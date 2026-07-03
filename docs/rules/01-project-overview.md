# 01 Project Overview

`agent-remote-server` is the Python control-plane API for agent-remote.

## Current Scope

The current repository foundation provides:

- FastAPI application factory.
- Settings loaded from environment and `.env`.
- Structured JSON logging.
- Request ID middleware.
- `/healthz` process health check.
- `/readyz` PostgreSQL and Redis readiness check.
- SQLAlchemy async engine helpers.
- Redis async readiness helper.
- Alembic initialization.
- Dockerfile and local Compose development stack.
- Basic tests and CI.

## Current Non-Goals

Do not implement these without first updating the protocol and architecture documents:

- Business tables.
- User authentication.
- Device registration.
- Node task polling behavior.
- Tool account binding.
- Tool sessions.
- Browser sessions.
- Authorization policy.

## Protocol Relationship

API and payload contract changes must start in `agent-remote-protocol`. This repository implements the server side only after the contract is updated.
