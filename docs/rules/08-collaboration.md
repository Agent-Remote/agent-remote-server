# 08 Collaboration

## Branches

Use short-lived branches:

- `feature/<topic>`
- `fix/<topic>`
- `refactor/<topic>`
- `chore/<topic>`
- `docs/<topic>`

Keep branch topics lowercase and descriptive.

## Commit Messages

Use Conventional Commits:

```text
type(scope): subject
type: subject
```

Allowed types:

- `feat`
- `fix`
- `refactor`
- `chore`
- `docs`
- `perf`
- `test`
- `build`
- `ci`
- `style`

Rules:

- Subject is English.
- Subject is imperative and concise.
- No trailing period.
- Keep the first line under 120 characters.

## Hooks

Repository hooks live in `.githooks/`.

Install:

```sh
scripts/install-githooks.sh
```

Hook behavior:

- `pre-commit` runs the quality gate.
- `commit-msg` validates Conventional Commit format.
- `pre-push` runs the quality gate again.

