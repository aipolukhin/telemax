# Contributing to Telemax

Thanks for helping. Keep changes focused, add tests for behaviour, and explain
user-visible trade-offs in the pull request.

## Development setup

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Run the same checks as CI:

```bash
ruff check bridge tests
mypy bridge
pytest -q
python -m build
python -m pip_audit . --strict
```

## Privacy rules

Tests and documentation must use synthetic identifiers and generated fixtures.
Never commit `.env`, `config.yaml`, databases, sessions, logs, network dumps,
account exports, private media, phone numbers, access tokens, or real message
content. Sanitising a filename is not enough: inspect metadata and file contents.

If a test needs media, generate a deterministic minimal fixture in the test or
provide a clearly licensed synthetic asset.

## Pull requests

Before submitting:

- run all checks above;
- update public documentation for user-visible changes;
- keep compatibility constants inside the MAX client boundary;
- state whether configuration or database migration is required;
- confirm that the diff contains no private data or credentials.

By contributing, you agree that your contribution is licensed under Apache-2.0.
