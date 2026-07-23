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

Runtime 控制平面还提供：

- 每节点 runtime backend 允许列表、默认值、策略、能力上报和 backend 感知调度。
- 每账户 runtime backend 固定，以及 Native Runtime 与 Docker Sandbox 之间的显式迁移。
- Session runtime 标识、中断 session 对账和不重放命令的 replacement session 继承关系。
- 非特权 node worker 与特权 Native Runtime helper 之间的窄任务契约。

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

该仓库包含控制平面 API、持久化模型、身份和设备 API、节点/runtime 策略、工具账户绑定与迁移状态机、session 对账，以及节点任务轮询 API。特权隔离和进程执行由 node 仓库实现；本地设备网络和 workspace 同步由 CLI 仓库实现。

## 许可证

agent-remote-server 使用 GPL-3.0-only 许可证。详见 `LICENSE`。

第三方依赖声明见 `THIRD_PARTY_NOTICES.md`。
