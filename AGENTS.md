# AGENTS.md

This document is the primary instruction set for AI agents and automated coding tools working in this repository. Repository-local rules take precedence over general assumptions.

## Task-To-Documentation Mapping

Before making changes, identify the task domain and read the matching rule document.

| Task Domain | Primary Reference |
| --- | --- |
| Project purpose and repository boundary | `docs/rules/01-project-overview.md` |
| Runtime architecture and module boundaries | `docs/rules/02-architecture.md` |
| Technology stack and dependencies | `docs/rules/03-tech-stack.md` |
| Python style, typing, formatting | `docs/rules/04-code-style.md` |
| Comments, docstrings, internal documentation | `docs/rules/05-comment-style.md` |
| Local commands and developer workflow | `docs/rules/06-commands.md` |
| Quality and security gates | `docs/rules/07-quality-security.md` |
| Git, commits, hooks, pull requests | `docs/rules/08-collaboration.md` |
| Data, persistence, migrations | `docs/rules/09-data-persistence.md` |
| Identity, authentication, devices, tokens | `docs/rules/10-identity-auth.md` |
| Node control, heartbeats, task leases | `docs/rules/11-node-control.md` |

## Mandatory Gates

- Public classes, methods, and functions in `src/` must have Chinese docstrings.
- Pydantic model and settings fields must use `Field(..., description="中文描述")`.
- Inline comments must explain why a trade-off exists, not restate the code.
- `ruff format --check`, `ruff check`, `mypy`, `pytest`, and docstring checks must pass before commit.
- Commit messages must follow Conventional Commits.
- Secrets, tokens, cookies, private keys, and login state must never be committed or logged.

## Implementation Rules

- Keep persistence changes tied to approved schema documents and Alembic migrations.
- Do not add authentication behavior, node task execution, tool sessions, or browser sessions without updating architecture notes, rule documents, and relevant tests first.
- Update server schemas, tests, and affected callers when API contracts change.
- Prefer explicit, boring code over speculative abstractions.
- Use async database and Redis clients in server runtime code.
- Preserve structured JSON logging and request ID propagation.
- Never add broad `Any` at API or service boundaries without a narrow explanation.

## Hook Setup

Install repository hooks after cloning:

```sh
scripts/install-githooks.sh
```

Run the full local quality gate:

```sh
scripts/run-quality-checks.sh
```

## Conflict Resolution

If existing code conflicts with these rules:

1. Stop before editing the conflicting area.
2. Identify the file and rule that disagree.
3. Ask for the intended current standard.
