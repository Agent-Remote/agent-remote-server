# 07 Quality And Security

## Required Quality Gates

Before committing:

- Ruff format check.
- Ruff lint check.
- Mypy.
- Pytest.
- Public docstring and Pydantic field-description check.
- Git diff whitespace check.

The hook scripts enforce these checks.

## Security Rules

- Never commit `.env` files.
- Never log tokens, cookies, private keys, passwords, login state, or browser session contents.
- Health checks may include dependency status and error class names, but must not expose secrets.
- `AGENT_REMOTE_SECRET_KEY` must be supplied by deployment configuration in real environments.
- Logs must remain structured JSON.

## Review Rules

Reject changes that:

- Add unaudited persistence behavior.
- Introduce broad `Any` at API boundaries.
- Add business tables without an approved persistence design and migration plan.
- Bypass hooks or CI.
- Store sensitive values in source code, examples, logs, or tests.
