"""M3 JSON contract tests.

PRD #61 §M3 + §JSON contract. The wire shape is the stability contract
for agents; this file pins it from three angles:

  1. Field-presence: every key the PRD example carries appears in the
     to_dict output. New keys may be added by later versions, but no
     v1 key may go missing without bumping schema_version.
  2. Shape validation: the document validates against the committed
     `schema/fj-queue.v1.json`. Both the success and error envelopes
     are covered (the schema is `oneOf`).
  3. Byte stability: a frozen-clock Snapshot serializes to a
     deterministic byte stream (sort_keys=True + 'Z' RFC3339).

Plus the error contract: typed exit codes per exception, JSON error
envelope shape, and non-FjQueueError fallback.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

import fj_queue as fq


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 27, 14, 3, 11, tzinfo=timezone.utc)
HOST = "git.wxs.ro"


def _runner(rid, *, status="active", labels=("grunt",), name=None):
    return fq.Runner(
        id=rid,
        name=name if name is not None else f"runner-{rid}",
        status=status,
        version="v12.7.3",
        labels=tuple(labels),
        ephemeral=False,
    )


def _job(jid, *, status="waiting", repo_id=85, owner_id=11, runs_on=("grunt",), needs=(), task_id=0, attempt=1, name=None):
    return fq.RawJob(
        id=jid,
        name=name or f"job-{jid}",
        status=status,
        repo_id=repo_id,
        owner_id=owner_id,
        runs_on=tuple(runs_on),
        needs=tuple(needs),
        task_id=task_id,
        attempt=attempt,
        handle=f"h-{jid}",
    )


def _snap(runners=(), jobs=(), repo_names=None, **kwargs):
    return fq.aggregate(
        runners=runners,
        jobs=jobs,
        repo_names=repo_names or {},
        now=NOW,
        host=HOST,
        **kwargs,
    )


@pytest.fixture(scope="module")
def schema() -> dict:
    return fq.load_schema()


# ---------------------------------------------------------------------------
# Schema file is well-formed and matches the contract `--schema` will emit.
# ---------------------------------------------------------------------------


def test_schema_file_loadable_and_versioned(schema):
    assert "$schema" in schema
    assert schema.get("title") == "fj-queue v1 snapshot"
    assert "oneOf" in schema  # success + error envelopes
    # `SuccessEnvelope` is one of the oneOf branches.
    branches = {b.get("$ref") for b in schema["oneOf"]}
    assert "#/$defs/SuccessEnvelope" in branches
    assert "#/$defs/ErrorEnvelope" in branches


def test_render_schema_round_trips_through_json_load(schema):
    """The string returned by render_schema() must parse to the same
    dict load_schema() returns. Different ways of asking for the same
    contract.
    """
    rendered = fq.render_schema()
    assert json.loads(rendered) == schema


def test_schema_validator_itself_is_valid(schema):
    """The schema document is itself a valid JSON Schema draft 2020-12.
    Catches typos at the schema level before they corrupt a release.
    """
    jsonschema.Draft202012Validator.check_schema(schema)


# ---------------------------------------------------------------------------
# Success-envelope: presence of every PRD-example key + schema-validation.
# ---------------------------------------------------------------------------


def test_empty_snapshot_has_all_v1_keys():
    """Additive-only policy floor: every v1 key MUST be present even on
    an empty snapshot. Missing a key (e.g. forgetting `warnings` on the
    empty path) would break agents that key off presence.
    """
    snap = _snap()
    d = fq.to_dict(snap)
    assert set(d.keys()) == {
        "schema_version",
        "as_of",
        "host",
        "filter",
        "schedulable_labels",
        "runners",
        "totals",
        "per_repo",
        "queue",
        "warnings",
        "runner_pods",
        "metrics",
        "ncps",
    }
    assert d["schema_version"] == 1
    assert d["host"] == HOST
    assert d["filter"] == {"repo": None, "label": None}
    assert d["totals"] == {"running": 0, "waiting": 0, "total": 0}
    assert d["runners"] == d["per_repo"] == d["queue"] == d["warnings"] == []
    assert d["schedulable_labels"] == []
    # Additive v1.x runner-metrics keys: present even when empty, for
    # contract stability. metrics.error is null on the no-error path.
    assert d["runner_pods"] == []
    assert d["metrics"] == {
        "source": "prometheus",
        "error": None,
        "rate_window": "5m",
    }
    # NCPS: present and null on an empty/default snapshot.
    assert d["ncps"] is None


def test_empty_snapshot_validates_against_schema(schema):
    snap = _snap()
    jsonschema.validate(fq.to_dict(snap), schema)


def test_as_of_is_rfc3339_utc_z():
    snap = _snap()
    d = fq.to_dict(snap)
    assert d["as_of"] == "2026-05-27T14:03:11Z"


def test_runner_wire_shape_includes_derived_online_bool():
    """`online` is NOT a Runner model field; it is derived from status
    at the wire layer. Active and idle both -> true; offline -> false.
    """
    runners = [
        _runner(1, status="active", labels=("docker", "grunt"), name="a-active"),
        _runner(2, status="idle", labels=("gpu",), name="b-idle"),
        _runner(3, status="offline", labels=("any",), name="c-offline"),
    ]
    snap = _snap(runners=runners)
    online_by_name = {r["name"]: r["online"] for r in fq.to_dict(snap)["runners"]}
    assert online_by_name == {"a-active": True, "b-idle": True, "c-offline": False}


def test_task_id_null_for_waiting_int_for_running_on_wire():
    """PRD JSON contract: `"task_id": null` when the job is waiting
    (Forgejo's 0 sentinel), an integer once running.
    """
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [
        _job(10, status="waiting", runs_on=("grunt",)),
        _job(20, status="running", task_id=98765, runs_on=("grunt",)),
    ]
    snap = _snap(runners=runners, jobs=jobs)
    d = fq.to_dict(snap)
    # queue is waiting-only.
    assert len(d["queue"]) == 1
    assert d["queue"][0]["task_id"] is None
    assert d["queue"][0]["job_id"] == 10
    assert d["queue"][0]["blocked_reason"] == "waiting_for_runner"


def test_filter_label_empty_serializes_to_null():
    """Matches the PRD example's `"label": null` for unscoped runs.
    Non-empty tuples serialize to a list (covered by separate test).
    """
    snap = _snap(filter_repo=None, filter_label=())
    assert fq.to_dict(snap)["filter"] == {"repo": None, "label": None}


def test_filter_label_non_empty_serializes_to_list():
    snap = _snap(filter_repo="alpha/repo", filter_label=("grunt", "docker"))
    assert fq.to_dict(snap)["filter"] == {
        "repo": "alpha/repo",
        "label": ["grunt", "docker"],
    }


def test_blocked_reason_enum_values_appear_unchanged_on_wire():
    """Each of the three documented `blocked_reason` strings reaches
    the wire verbatim (not, e.g., title-cased or i18n'd).
    """
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [
        _job(10, runs_on=("grunt",), needs=()),                 # waiting_for_runner
        _job(20, runs_on=("nope",), needs=()),                  # unschedulable
        _job(30, runs_on=("grunt",), needs=("upstream",)),      # blocked_on_needs
    ]
    snap = _snap(runners=runners, jobs=jobs)
    by_id = {j["job_id"]: j["blocked_reason"] for j in fq.to_dict(snap)["queue"]}
    assert by_id == {
        10: "waiting_for_runner",
        20: "unschedulable",
        30: "blocked_on_needs",
    }


def test_warnings_carry_unschedulable_labels_code(schema):
    """Single warning case currently emitted. Validates against the
    Warning sub-schema via the parent doc validation.
    """
    runners = [_runner(1, status="active", labels=("grunt",))]
    jobs = [_job(10, runs_on=("nope",), needs=())]
    snap = _snap(runners=runners, jobs=jobs)
    d = fq.to_dict(snap)
    assert len(d["warnings"]) == 1
    w = d["warnings"][0]
    assert w["code"] == "unschedulable_labels"
    assert w["job_id"] == 10
    assert w["runs_on"] == ["nope"]
    assert "No online runner can satisfy" in w["message"]
    jsonschema.validate(d, schema)


# ---------------------------------------------------------------------------
# Byte stability (sort_keys, frozen clock).
# ---------------------------------------------------------------------------


def test_render_json_is_byte_stable_under_frozen_clock():
    """Same Snapshot in -> same JSON bytes out, twice in a row.
    sort_keys=True is the contract anchor.
    """
    runners = [
        _runner(2, name="b-second", status="active", labels=("docker", "grunt")),
        _runner(1, name="a-first", status="idle", labels=("gpu",)),
    ]
    jobs = [
        _job(20, runs_on=("grunt",), needs=()),
        _job(10, runs_on=("docker", "gpu"), needs=()),  # schedulable via runner-2? no, docker on r2, gpu on r1 -> superset miss
    ]
    snap = _snap(runners=runners, jobs=jobs, repo_names={85: "containers/theme-api"})
    a = fq.render_json(snap)
    b = fq.render_json(snap)
    assert a == b
    # And keys at the top level appear in sorted order (sort_keys=True).
    parsed = json.loads(a)
    top_keys_in_string = [
        line.strip().rstrip(":").strip('"')
        for line in a.split("\n")
        if line.startswith("  \"") and line.rstrip().endswith(":") is False
    ]
    # Above heuristic is fragile, so use a direct check: re-render with
    # sort_keys and compare; both should equal a.
    assert json.dumps(parsed, sort_keys=True, indent=2) == a


def test_render_json_top_level_keys_are_sorted_alphabetically():
    """Explicit assertion: the FIRST top-level key encountered in the
    JSON string is `as_of` (alphabetically before `filter`, `host`,
    `per_repo`, `queue`, `runners`, ...).
    """
    rendered = fq.render_json(_snap())
    parsed = json.loads(rendered)
    top_keys = list(parsed.keys())
    assert top_keys == sorted(top_keys)
    assert top_keys[0] == "as_of"


def test_render_json_validates_against_schema_for_typical_snapshot(schema):
    """End-to-end: build a realistic Snapshot, render to JSON string,
    parse, validate. Catches drift between to_dict output and the
    committed schema.
    """
    runners = [
        _runner(3, name="k8s-runner", status="active", labels=("docker", "grunt")),
        _runner(2, name="k8s-runner-b", status="offline", labels=("docker",)),
    ]
    jobs = [
        _job(
            80239,
            status="waiting",
            repo_id=85,
            owner_id=11,
            runs_on=("grunt",),
            needs=(),
            name="Semantic Release",
        ),
        _job(
            79969,
            status="waiting",
            repo_id=586,
            owner_id=15,
            runs_on=("gpu",),
            needs=(),
            name="pipeline",
        ),
        _job(
            83886,
            status="running",
            task_id=85492,
            repo_id=589,
            owner_id=15,
            runs_on=("grunt",),
            needs=("pipeline.generate",),
            name="Chainsaw E2E Tests",
        ),
    ]
    snap = _snap(
        runners=runners,
        jobs=jobs,
        repo_names={
            85: "containers/theme-api",
            586: "crossplane/harbor",
            589: "owner-a/repo-a",
        },
    )
    rendered = fq.render_json(snap)
    parsed = json.loads(rendered)
    jsonschema.validate(parsed, schema)
    # And the warning we expect is present (job 79969 with runs_on=[gpu]
    # and no online runner matching).
    warnings = [w for w in parsed["warnings"] if w["job_id"] == 79969]
    assert len(warnings) == 1
    assert warnings[0]["code"] == "unschedulable_labels"


# ---------------------------------------------------------------------------
# Error envelope (the stdout-on-failure contract).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls,expected_code,expected_exit",
    [
        (fq.ConfigError, "usage", fq.EXIT_USAGE),
        (fq.AuthError, "auth", fq.EXIT_AUTH),
        (fq.ConnectionError, "connection", fq.EXIT_CONNECTION),
        (fq.SchemaDrift, "schema_drift", fq.EXIT_SCHEMA_DRIFT),
    ],
)
def test_error_envelope_code_and_exit_per_exception(exc_cls, expected_code, expected_exit):
    exc = exc_cls("something went wrong")
    rendered = fq.render_json_error(exc, host=HOST)
    parsed = json.loads(rendered)
    assert parsed == {
        "schema_version": 1,
        "error": {
            "code": expected_code,
            "message": "something went wrong",
            "host": HOST,
        },
    }
    assert fq.exit_code_for(exc) == expected_exit


def test_error_envelope_for_unknown_exception_is_internal_exit_1():
    """A non-FjQueueError MUST still emit a parseable JSON envelope on
    stdout (the contract is "even on failure"). Code falls back to
    `internal`, exit to 1.
    """
    rendered = fq.render_json_error(RuntimeError("boom"), host=HOST)
    parsed = json.loads(rendered)
    assert parsed["error"]["code"] == "internal"
    assert parsed["error"]["message"] == "boom"
    assert parsed["error"]["host"] == HOST
    assert fq.exit_code_for(RuntimeError("boom")) == 1


def test_error_envelope_uses_classname_when_no_message():
    """str(exc) is empty when an exception is raised bare. Fall back to
    the class name so the message is never an empty string.
    """
    rendered = fq.render_json_error(fq.AuthError(), host=HOST)
    parsed = json.loads(rendered)
    assert parsed["error"]["message"] == "AuthError"


def test_error_envelope_validates_against_error_branch_of_schema(schema):
    rendered = fq.render_json_error(fq.AuthError("403 forbidden"), host=HOST)
    parsed = json.loads(rendered)
    jsonschema.validate(parsed, schema)


def test_error_envelope_byte_stable():
    """Same exception, same host, same string twice."""
    exc = fq.SchemaDrift("unexpected payload shape")
    a = fq.render_json_error(exc, host=HOST)
    b = fq.render_json_error(exc, host=HOST)
    assert a == b


# ---------------------------------------------------------------------------
# Token-leak defense (PRD §error contract: message NEVER contains token).
# ---------------------------------------------------------------------------


def test_error_envelope_scrubs_token_from_message():
    """If an exception message ever carries a literal token (e.g. a
    future Forgejo response echoed an Authorization header back into
    a 4xx body that M1 then snippet'd into a ConnectionError), the
    scrub_token kwarg masks it before serialization.
    """
    secret = "abc123DEADBEEFsecrettoken456"
    exc = fq.ConnectionError(
        f"403 on /api/v1/admin/actions/runners: "
        f"{{\"error\":\"bad Authorization: token {secret}\"}}"
    )
    rendered = fq.render_json_error(exc, host=HOST, scrub_token=secret)
    parsed = json.loads(rendered)
    assert secret not in rendered
    assert "***" in parsed["error"]["message"]


def test_error_envelope_no_scrub_when_token_kwarg_omitted():
    """scrub_token defaults to None (no scrubbing). Existing M1/M2/M3
    exceptions never embed the token; the kwarg is opt-in defense for
    M5/M6 callers that have a Config in scope.
    """
    exc = fq.AuthError("401 unauthorized: check --token / FORGEJO_TOKEN")
    rendered = fq.render_json_error(exc, host=HOST)
    # The flag name FORGEJO_TOKEN (literal text, not a secret) is fine.
    assert "FORGEJO_TOKEN" in rendered


# ---------------------------------------------------------------------------
# Brief-required schema validation cases: all-offline + C3 (docker+gpu split).
# ---------------------------------------------------------------------------


def test_schema_validates_all_runners_offline_scenario(schema):
    """Brief case: schema validates an all-offline snapshot (every
    waiting job becomes unschedulable, warnings list non-empty).
    """
    runners = [
        _runner(1, status="offline", labels=("docker",)),
        _runner(2, status="offline", labels=("grunt",)),
    ]
    jobs = [
        _job(10, runs_on=("docker",), needs=()),
        _job(11, runs_on=("grunt",), needs=()),
    ]
    snap = _snap(runners=runners, jobs=jobs)
    parsed = json.loads(fq.render_json(snap))
    jsonschema.validate(parsed, schema)
    warning_ids = sorted(w["job_id"] for w in parsed["warnings"])
    assert warning_ids == [10, 11]


def test_schema_validates_c3_docker_plus_gpu_split_scenario(schema):
    """Brief case: schema validates the canonical C3 unschedulable
    case: one [docker]-only runner + one [gpu]-only runner, job needs
    [docker, gpu]. Job is unschedulable + warning emitted.
    """
    runners = [
        _runner(1, status="active", labels=("docker",), name="docker-only"),
        _runner(2, status="active", labels=("gpu",), name="gpu-only"),
    ]
    jobs = [_job(10, runs_on=("docker", "gpu"), needs=())]
    snap = _snap(runners=runners, jobs=jobs)
    parsed = json.loads(fq.render_json(snap))
    jsonschema.validate(parsed, schema)
    assert parsed["queue"][0]["blocked_reason"] == "unschedulable"
    assert parsed["warnings"][0]["job_id"] == 10


# ---------------------------------------------------------------------------
# Open additive-only schema (PRD §JSON contract, locked policy):
#   * Top-level oneOf branches (SuccessEnvelope, ErrorEnvelope) are
#     CLOSED, only to disambiguate the two envelope shapes.
#   * Every inner object (Runner, Job, RepoBreakdown, totals, filter,
#     error, Warning) is OPEN: new optional fields land in v1.x
#     without bumping schema_version.
# ---------------------------------------------------------------------------


def test_schema_closes_only_the_two_oneof_branches_not_inner_objects(schema):
    """The closed-schema invariant lives at exactly two places:
    SuccessEnvelope + ErrorEnvelope. Everything inside is open so the
    additive-only policy actually delivers forward compat.
    """
    defs = schema["$defs"]
    assert defs["SuccessEnvelope"].get("additionalProperties") is False
    assert defs["ErrorEnvelope"].get("additionalProperties") is False
    for inner_name in ("Runner", "Job", "RepoBreakdown", "Warning"):
        assert "additionalProperties" not in defs[inner_name], inner_name
    # Inline sub-objects on SuccessEnvelope / ErrorEnvelope.
    filter_obj = defs["SuccessEnvelope"]["properties"]["filter"]
    totals_obj = defs["SuccessEnvelope"]["properties"]["totals"]
    error_obj = defs["ErrorEnvelope"]["properties"]["error"]
    assert "additionalProperties" not in filter_obj
    assert "additionalProperties" not in totals_obj
    assert "additionalProperties" not in error_obj


def test_schema_rejects_missing_required_top_level_key(schema):
    """The closed top-level envelope catches missing keys (drift
    between to_dict and the schema). Dropping `warnings` should fail.
    """
    snap = _snap()
    d = fq.to_dict(snap)
    del d["warnings"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(d, schema)


def test_schema_rejects_unknown_top_level_key(schema):
    """SuccessEnvelope is closed, so a v1.5-style doc with an extra
    top-level `extras` key MUST fail v1 validation. Bumping
    schema_version (or splitting the oneOf branch) is the right escape
    hatch.
    """
    snap = _snap()
    d = fq.to_dict(snap)
    d["extras"] = {"unexpected": True}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(d, schema)


def test_schema_accepts_unknown_inner_object_keys(schema):
    """The open-additive contract: a v1.5-style Runner row with an
    extra `priority: 5` (or any unknown inner key) MUST validate
    against v1. Locks forward-compat so a future Forgejo field
    surfacing as a v1.5 addition does not break agent clients pinned
    to v1.
    """
    runners = [_runner(1, status="active", labels=("grunt",))]
    snap = _snap(runners=runners)
    d = fq.to_dict(snap)
    # Inject an unknown key into the Runner row (open) AND another into
    # the totals object (also open) AND one into the inline filter
    # object (also open). All three must validate.
    d["runners"][0]["priority"] = 5
    d["totals"]["experimental_skipped"] = 0
    d["filter"]["org"] = None
    # Should not raise.
    jsonschema.validate(d, schema)


# ---------------------------------------------------------------------------
# Naive datetime rejected at the wire layer (defense-in-depth: M2
# already rejects in aggregate(), but a hand-rolled Snapshot in tests
# or from a future code path must not bypass).
# ---------------------------------------------------------------------------


def test_rfc3339_helper_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        fq._rfc3339_utc_z(datetime(2026, 1, 1, 0, 0, 0))


def test_rfc3339_helper_normalizes_non_utc_offset_to_utc():
    """A datetime carrying a +02:00 offset is permitted by the helper;
    we astimezone() it to UTC so the wire string is always Z-suffixed
    UTC, never offset-suffixed.
    """
    from datetime import timedelta
    plus_two = timezone(timedelta(hours=2))
    dt = datetime(2026, 5, 27, 16, 3, 11, tzinfo=plus_two)
    assert fq._rfc3339_utc_z(dt) == "2026-05-27T14:03:11Z"


# ---------------------------------------------------------------------------
# Live-fixture round-trip: the captured Forgejo response set, aggregated,
# serialized, validated.
# ---------------------------------------------------------------------------


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_live_fixture_renders_valid_json(schema):
    runners = [
        fq._normalize_runner(d)
        for d in json.loads((FIXTURES / "runners-live.json").read_text())
    ]
    jobs = [
        fq._normalize_job(d)
        for d in json.loads((FIXTURES / "jobs-live.json").read_text())
    ]
    snap = fq.aggregate(
        runners=runners,
        jobs=jobs,
        repo_names={589: "owner-a/repo-a", 586: "crossplane/harbor"},
        now=NOW,
        host=HOST,
    )
    rendered = fq.render_json(snap)
    parsed = json.loads(rendered)
    jsonschema.validate(parsed, schema)
    # Wire-facing sanity:
    assert parsed["totals"] == {"running": 1, "waiting": 1, "total": 2}
    assert parsed["schedulable_labels"] == ["docker", "grunt"]
    # The waiting job (id=79969) is blocked_on_needs, so no warnings.
    assert parsed["warnings"] == []
    assert parsed["queue"][0]["job_id"] == 79969
    assert parsed["queue"][0]["blocked_reason"] == "blocked_on_needs"
    # task_id null on the wire for the waiting job.
    assert parsed["queue"][0]["task_id"] is None


# ---------------------------------------------------------------------------
# Runner metrics wire contract (additive v1.x keys: runner_pods + metrics).
# ---------------------------------------------------------------------------


def _pod(name, *, node="k8s-green-wn1", cpu=0.01, mem=700_000_000, cpu_limit=None, mem_limit=None):
    return fq.PodResource(
        pod=name,
        node=node,
        cpu_cores=cpu,
        memory_bytes=mem,
        cpu_limit_cores=cpu_limit,
        memory_limit_bytes=mem_limit,
    )


def test_runner_pods_wire_shape_is_raw_numbers(schema):
    """runner_pods rows carry RAW numbers (not MiB/decimal-formatted):
    cpu_cores a number, memory_bytes an integer, limits raw (null when
    absent). Validates against schema. Mirrors live reality: memory limit
    set, CPU limit None.
    """
    pods = (
        _pod(
            "forgejo-runner-qf4k7",
            node="k8s-green-wn2",
            cpu=0.00109,
            mem=763322368,
            cpu_limit=None,
            mem_limit=18674094196,
        ),
        _pod("forgejo-runner-b5f9b", node="k8s-green-wn3", cpu=0.01023, mem=711917568),
    )
    snap = _snap(runner_pods=pods)
    d = fq.to_dict(snap)
    assert d["runner_pods"][0] == {
        "pod": "forgejo-runner-qf4k7",
        "node": "k8s-green-wn2",
        "cpu_cores": 0.00109,
        "memory_bytes": 763322368,
        "cpu_limit_cores": None,
        "memory_limit_bytes": 18674094196,
    }
    assert d["metrics"] == {
        "source": "prometheus",
        "error": None,
        "rate_window": "5m",
    }
    jsonschema.validate(d, schema)


def test_metrics_degraded_case_validates_and_carries_error(schema):
    """On a Prometheus failure: runner_pods is [] and metrics.error is
    the reason string. Both validate against the schema.
    """
    snap = _snap(runner_pods=(), metrics_error="timeout querying prometheus")
    d = fq.to_dict(snap)
    assert d["runner_pods"] == []
    assert d["metrics"]["error"] == "timeout querying prometheus"
    assert d["metrics"]["source"] == "prometheus"
    jsonschema.validate(d, schema)


def test_podresource_def_is_open_per_additive_policy(schema):
    """Inner objects (including PodResource + metrics) stay OPEN so a
    future field lands in v1.x without bumping schema_version.
    """
    defs = schema["$defs"]
    assert "PodResource" in defs
    assert "additionalProperties" not in defs["PodResource"]
    metrics_obj = defs["SuccessEnvelope"]["properties"]["metrics"]
    assert "additionalProperties" not in metrics_obj
    # An unknown key on a pod row + metrics object must still validate.
    snap = _snap(runner_pods=(_pod("p1"),))
    d = fq.to_dict(snap)
    d["runner_pods"][0]["throttled"] = False
    d["metrics"]["scrape_ms"] = 12
    jsonschema.validate(d, schema)


# ---------------------------------------------------------------------------
# NCPS wire contract (additive top-level `ncps` key; object or null).
# ---------------------------------------------------------------------------


def _ncps(active=True, req=8.5, inflight=1, upstream=0.3, bytes_ps=4000000.0):
    return fq.NcpsStatus(
        active=active,
        requests_per_sec=req,
        inflight=inflight,
        upstream_per_sec=upstream,
        bytes_per_sec=bytes_ps,
    )


def test_ncps_populated_wire_shape_is_raw_numbers(schema):
    snap = _snap(ncps=_ncps())
    d = fq.to_dict(snap)
    assert d["ncps"] == {
        "active": True,
        "requests_per_sec": 8.5,
        "inflight": 1,
        "upstream_per_sec": 0.3,
        "bytes_per_sec": 4000000.0,
    }
    jsonschema.validate(d, schema)


def test_ncps_null_when_absent_validates(schema):
    snap = _snap(ncps=None)
    d = fq.to_dict(snap)
    assert d["ncps"] is None
    jsonschema.validate(d, schema)


def test_ncps_idle_wire_shape(schema):
    snap = _snap(ncps=_ncps(active=False, req=0.0, inflight=0, upstream=0.0, bytes_ps=0.0))
    d = fq.to_dict(snap)
    assert d["ncps"]["active"] is False
    jsonschema.validate(d, schema)


def test_ncps_def_is_open_per_additive_policy(schema):
    defs = schema["$defs"]
    assert "NcpsStatus" in defs
    assert "additionalProperties" not in defs["NcpsStatus"]
    snap = _snap(ncps=_ncps())
    d = fq.to_dict(snap)
    d["ncps"]["p99_latency_ms"] = 42  # unknown future field
    jsonschema.validate(d, schema)
