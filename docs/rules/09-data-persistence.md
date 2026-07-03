# 09 Data And Persistence

## Current Persistence Boundary

The current skeleton initializes SQLAlchemy and Alembic only. Business tables require an explicit schema design and migration plan.

## Database

- PostgreSQL is the control-plane database.
- SQLAlchemy async engine is required.
- Alembic owns schema migrations.
- Migrations must be reversible unless explicitly documented.

## Redis

Redis is required by the broader project. The current skeleton only checks connectivity.

Future feature work uses Redis for:

- Task lease coordination.
- Distributed locks.
- Polling throttles.
- Short-lived task state.

## Secrets

Secrets are supplied through environment variables or deployment secret stores.

Do not persist:

- Raw tokens.
- Private keys.
- Tool account login state.
- Browser cookies or profiles.
