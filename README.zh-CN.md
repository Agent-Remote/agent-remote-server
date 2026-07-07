# agent-remote-server

[English](README.md) | 中文

agent-remote-server 是 agent-remote 的 Python 3.13 控制平面 API。它负责身份、设备、节点、workspace、同步会话、tool account、tool session、临时浏览器会话和审计数据。

## 能力范围

- FastAPI 应用和结构化 JSON 日志。
- 请求 ID 中间件。
- `/healthz` 进程健康检查。
- `/readyz` PostgreSQL 和 Redis 就绪检查。
- SQLAlchemy async 持久化模型。
- Alembic 数据库迁移。
- 用户、设备、节点、任务轮询、session、workspace 和 browser session API。
- Dockerfile 和本地 Compose 开发栈。

## 环境要求

- Python 3.13
- uv
- Docker 和 Docker Compose
- PostgreSQL
- Redis

## 本地开发

```sh
uv sync
cp .env.example .env
uv run pytest
uv run uvicorn agent_remote_server.main:app --reload
```

本地依赖栈：

```sh
docker compose up --build
```

健康检查：

```sh
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
```

## 配置

主要环境变量：

- `AGENT_REMOTE_ENV`
- `AGENT_REMOTE_SECRET_KEY`
- `PUBLIC_BASE_URL`
- `DATABASE_URL`
- `REDIS_URL`
- `LOG_LEVEL`

详见 `.env.example`。

## 容器

镜像默认运行 Alembic migration 后启动 Uvicorn。设置 `AGENT_REMOTE_RUN_MIGRATIONS=0` 可跳过迁移。

GitHub Actions 会为 release tag 构建并推送 GHCR 镜像，同时创建 GitHub Release 记录和 release notes。

## 许可证

agent-remote-server 使用 GPL-3.0-only 许可证。详见 `LICENSE`。

第三方依赖声明见 `THIRD_PARTY_NOTICES.md`。
