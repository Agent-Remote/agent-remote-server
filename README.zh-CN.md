# agent-remote-server

<p align="center"><img src="assets/agent-remote-icon.svg" alt="Agent Remote 图标" width="80" height="80"></p>

<p align="center">
  <a href="https://github.com/Agent-Remote/agent-remote-server/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Agent-Remote/agent-remote-server/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://codecov.io/gh/Agent-Remote/agent-remote-server"><img alt="Codecov" src="https://codecov.io/gh/Agent-Remote/agent-remote-server/graph/badge.svg"></a>
  <a href="https://github.com/Agent-Remote/agent-remote-server/stargazers"><img alt="GitHub Stars" src="https://img.shields.io/github/stars/Agent-Remote/agent-remote-server?style=flat&logo=github"></a>
  <img alt="Python 3.13" src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white">
  <a href="LICENSE"><img alt="License: GPL-3.0" src="https://img.shields.io/github/license/Agent-Remote/agent-remote-server"></a>
</p>

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
- 带修订版本的设备级 SSH key 同步，以及 attach 就绪状态上报。
- 为退役资源提供受保护的删除能力，删除前校验生命周期状态和关联记录。
- Session 级端口转发授权，包含 Redis 一次性 token、可续租 lease、配额、资源即时撤销、生命周期清理和仅元数据审计事件。

只有所选 Node 为 session backend 明确上报 capability 时，控制面才允许创建端口转发。当前发布仅支持 Native Runtime session；Docker Sandbox 请求会 fail closed。应用数据直接在设备与 Node 之间传输，不经过控制面。

管理前端只能删除失败的同步会话。活跃的本地 Mutagen 会话必须在所属设备上终止，控制面不会静默遗留运行中的同步进程。

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
- `PORT_FORWARDING_ENABLED`
- `PORT_FORWARD_MIN_PORT` / `PORT_FORWARD_MAX_PORT`
- `PORT_FORWARD_MAX_PER_USER` / `PORT_FORWARD_MAX_PER_DEVICE` / `PORT_FORWARD_MAX_PER_SESSION`
- `PORT_FORWARD_MAX_STREAMS`
- `PORT_FORWARD_DEFAULT_TTL_SECONDS` / `PORT_FORWARD_MAX_TTL_SECONDS`
- `PORT_FORWARD_CONNECTION_TOKEN_TTL_SECONDS` / `PORT_FORWARD_LEASE_SECONDS`
- `PORT_FORWARD_CONTROL_PLANE_GRACE_SECONDS`
- `PORT_FORWARD_BYTES_PER_SECOND`
- `PORT_FORWARD_CLEANUP_INTERVAL_SECONDS`
- `PORT_FORWARD_CREATE_RATE_LIMIT_PER_MINUTE` / `PORT_FORWARD_REDEEM_RATE_LIMIT_PER_MINUTE`
- `DEVICE_CONTROL_ENABLED`
- `DEVICE_CONTROL_RELEASE_EVIDENCE_PATH`
- `DEVICE_CONTROL_RELEASE_PUBLIC_KEY`
- `DEVICE_SESSION_RETENTION_DAYS`
- `DEVICE_SESSION_AUDIT_RETENTION_DAYS`

见 `.env.example`。

清单 schema 和 Ed25519 规范签名载荷见 `docs/device-control-release-evidence.md`。

设备控制默认关闭。`AGENT_REMOTE_ENV=production` 时，启用该功能必须提供未过期、绑定当前服务端
精确版本并由固定 Ed25519 公钥验证通过的发布证据清单；公钥使用 Base64 编码。开发环境只能为
不含敏感数据的测试显式启用该能力。生产部署还必须显式选择非零的终态 session 和设备 session
审计保留天数，且审计保留期不得短于 session 保留期。

用户 API 在 `/api/v1/port-forwards` 下提供创建、列表、详情、重连和停止操作；Node API 提供 redeem、renew 和 release。Connection token 只返回一次并带 `Cache-Control: no-store`，仅作为短期 Redis 值保存，客户端不得记录日志或持久化。

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
