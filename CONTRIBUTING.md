# Contributing to Fangorn

Start proposed work in a GitHub Issue. Implementation begins only for an
accepted Issue; opening a pull request does not accept new scope.
Keep each pull request focused and link it with `Closes #<issue>`.

## Local checks

Fangorn supports Python 3.12–3.14 on Linux and macOS. Install the locked
development environment and run the deterministic checks:

```sh
uv sync --locked --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
uv run python scripts/check_publication.py --source . dist/*
```

Run installed-artifact smoke checks for both distributions:

```sh
uv run --isolated --no-project --with dist/*.whl fangorn --help
uv run --isolated --no-project --with dist/*.whl fangorn --version
uv run --isolated --no-project --with dist/*.tar.gz fangorn --help
uv run --isolated --no-project --with dist/*.tar.gz fangorn --version
```

Examples, diagnostics, fixtures, commit messages, and pull-request text must be
sanitized. Never include credentials, private paths, private infrastructure,
complete environment dumps, or raw log bundles.

By participating, you agree to follow [the Code of Conduct](CODE_OF_CONDUCT.md).
Report vulnerabilities or conduct incidents through
[GitHub Private Vulnerability Reporting](https://github.com/iurimadeira/fangorn/security/advisories/new),
as described in [the security policy](.github/SECURITY.md).
