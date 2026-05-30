# Contributing to fj-queue

Thank you for your interest in contributing to fj-queue! We welcome contributions from the community.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Ways to Contribute](#ways-to-contribute)
- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Pull Request Process](#pull-request-process)
- [Coding Standards](#coding-standards)
- [Communication](#communication)

## Code of Conduct

This project adheres to a [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code.

## Ways to Contribute

There are many ways to contribute:

- **Report bugs** - File detailed bug reports with reproduction steps
- **Suggest features** - Propose new features or improvements
- **Write code** - Submit bug fixes or new features
- **Improve documentation** - Fix typos, clarify content, add examples
- **Review pull requests** - Provide feedback on proposed changes
- **Answer questions** - Help other users in issues or discussions
- **Write tests** - Improve test coverage

## Getting Started

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/YOUR_USERNAME/fj-queue.git
   cd fj-queue
   ```
3. **Add upstream remote**:
   ```bash
   git remote add upstream https://github.com/vtmocanu/fj-queue
   ```
4. **Install dependencies**:
   ```bash
   uv sync --group dev
   ```

## Development Workflow

1. **Create a branch** for your changes:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** - Write code, add tests, update docs

3. **Test your changes** - Run `uv run --group dev pytest tests/`

4. **Commit your changes**:
   ```bash
   git commit -m "Description of your changes"
   ```

5. **Push to your fork**:
   ```bash
   git push origin feature/your-feature-name
   ```

6. **Open a Pull Request** on GitHub

## Pull Request Process

### Before Submitting

- Ensure your code follows the project's coding standards
- Add or update tests for your changes
- Verify all tests pass: `uv run --group dev pytest tests/`
- Update documentation as needed
- Keep pull requests focused on a single concern

### PR Description

Include in your pull request description:

- **What** - Summary of changes
- **Why** - Motivation and context
- **How** - Implementation approach
- **Testing** - How you tested the changes
- **Related Issues** - Link any related issues (e.g., "Fixes #123")

### Review Process

1. Automated checks will run on your PR
2. Maintainers will review your changes
3. Address any feedback or requested changes
4. Once approved, a maintainer will merge your PR

## Coding Standards

- Write clear, readable code
- Use meaningful names for variables and functions
- Add comments for complex logic
- Follow language-specific best practices

## Communication

- **Issues** - Report bugs and request features via [GitHub Issues](https://github.com/vtmocanu/fj-queue/issues)

---

Thank you for contributing to fj-queue!
