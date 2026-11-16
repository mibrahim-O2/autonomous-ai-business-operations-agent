# Contributing

Thank you for your interest. This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## Development setup

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev]"
```

## Before opening a pull request

```bash
.venv/bin/ruff format src tests
```

```bash
.venv/bin/ruff check src tests
```

```bash
.venv/bin/pytest
```

All three must pass. Continuous integration runs the linter and the tests on Python 3.11 and 3.13.

## Guidelines

- One topic per pull request, on its own branch from the latest `main`.
- Add or update tests for every behaviour change. Tests must run offline with the scripted model.
- New tools declare a risk level and a typed input model. Tools with side effects must be safe to replay.
- New agents get the smallest tool allow-list that their role needs.
- Database changes are appended as new migrations; existing migrations are never edited.
- Never commit secrets, real customer data, or real credentials. Use the mock integrations.
- Document what you built, and state plainly what is not implemented or not tested.

## Reporting bugs

Open an issue with the steps to reproduce, what you expected, what happened, and the output of `bizops trace RUN_ID` if a run was involved. For security problems, follow [SECURITY.md](SECURITY.md) instead.
