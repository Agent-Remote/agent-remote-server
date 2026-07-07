# agent-remote-server

[English](README.md) | 中文

agent-remote 的 Python 控制平面 API。

该仓库当前提供控制平面服务基础：

- FastAPI application factory。
- 从环境变量和 `.env` 加载设置。
- 结构化 JSON 日志。
- Request ID middleware。
- `/healthz` 进程健康检查。
- `/readyz` PostgreSQL 和 Redis readiness 检查。
- SQLAlchemy async engine helpers。
- Alembic 初始化。
- Dockerfile 和本地 Compose 开发栈。
- 基础测试。

## 要求

- Python 3.13
- uv
- 用于本地依赖服务的 Docker 和 Docker Compose

## 本地设置

```sh
uv sync
cp .env.example .env
```

运行测试：

```sh
uv run pytest
```

本地运行 API：

```sh
uv run uvicorn agent_remote_server.main:app --reload
```

运行本地 Compose 栈：

```sh
docker compose up --build
```

健康检查：

```sh
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
```

## 配置

环境变量：

- `AGENT_REMOTE_ENV`
- `AGENT_REMOTE_SECRET_KEY`
- `PUBLIC_BASE_URL`
- `DATABASE_URL`
- `REDIS_URL`
- `LOG_LEVEL`

见 `.env.example`。

## 容器

Docker 镜像默认会运行 Alembic migrations，然后启动 Uvicorn：

```sh
docker build -t agent-remote-server .
docker run --rm -p 8000:8000 \
  -e AGENT_REMOTE_SECRET_KEY=change-me \
  -e DATABASE_URL=postgresql+asyncpg://agent_remote:agent_remote@postgres:5432/agent_remote \
  -e REDIS_URL=redis://redis:6379/0 \
  agent-remote-server
```

设置 `AGENT_REMOTE_RUN_MIGRATIONS=0` 可在一次性命令中跳过 migrations。

GitHub Actions 会在 `v*` tag 上构建生产镜像并推送到 GHCR，同时创建带生成 release notes 的 GitHub Release 记录。

## 当前边界

该仓库包含控制平面 API 基础、持久化模型、身份和设备 API、节点控制 API，以及节点任务轮询 API。需要本地设备网络、workspace 同步、工具账户绑定和交互式工具 session 的运行时功能由 CLI 和 node 仓库实现。

## 许可证

agent-remote-server 使用 GPL-3.0-only 许可证。详见 `LICENSE`。

第三方依赖声明见 `THIRD_PARTY_NOTICES.md`。
