# 04 Code Style

## Formatter and Linter

Ruff is authoritative.

Required checks:

```sh
uv run ruff format --check .
uv run ruff check .
```

Use `uv run ruff format .` to apply formatting.

## Imports

Import order:

1. Standard library.
2. Third-party packages.
3. Internal `agent_remote_server` imports.

Rules:

- No wildcard imports.
- Prefer specific imports over importing a whole internal module.
- Keep imports absolute for package modules.

## Typing

- Public functions and methods must be typed.
- Avoid boundary-level `Any`.
- Use Python 3.13 type syntax consistently.
- Pydantic models own API data shapes.
- Prefer explicit return types, including `-> None`.

## Naming

- Modules use lowercase snake_case.
- Classes use PascalCase.
- Functions and variables use snake_case.
- Constants use uppercase snake_case.
- Private helpers start with `_`.

## Error Handling

- Expected dependency failures should become structured status responses.
- Unexpected programmer errors may raise exceptions.
- Do not silently suppress exceptions unless the code explains why the operation is best-effort.

