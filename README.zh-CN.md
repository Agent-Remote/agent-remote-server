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

`agent-remote-server` 是 Agent Remote 的 FastAPI 控制面，保存用户、设备、工具账户、工作区、会话、同步元数据、端口转发记录和审计数据，并提供 CLI、Node 与管理前端使用的 API。

## 当前边界

Server 负责控制面操作的授权与记录。Node 执行获准的运行时工作；CLI 负责本地凭据、WireGuard、SSH、Mutagen 和本地操作 journal。Server 不接收私钥，也不替代本地 CLI。

当前 API 包括认证与设备登记、账户绑定、工作区/同步准备、准确会话接管/创建/停止/删除请求、端口转发、运行时恢复请求、附件传输元数据和设备控制 API。每个有后果的操作都绑定用户、设备、会话、节点和代次等上下文，并返回结构化结果或明确的未知结果。

## 要求

- Python 3.13
- `uv`
- 用于本地服务的 Docker 和 Docker Compose

## 本地开发

```sh
uv sync
cp .env.example .env
uv run pytest
uv run uvicorn agent_remote_server.main:app --reload
```

启动本地栈：

```sh
docker compose up --build
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
```

仓库质量门禁是 `scripts/run-quality-checks.sh`，会运行格式、lint、类型、docstring 检查和测试。

## 容器安装

Docker 镜像默认先运行 Alembic 迁移，再启动 Uvicorn：

```sh
docker build -t agent-remote-server .
docker run --rm -p 8000:8000 \
  -e AGENT_REMOTE_SECRET_KEY=change-me \
  -e DATABASE_URL=postgresql+asyncpg://agent_remote:agent_remote@postgres:5432/agent_remote \
  -e REDIS_URL=redis://redis:6379/0 \
  agent-remote-server
```

一次性命令可设置 `AGENT_REMOTE_RUN_MIGRATIONS=0` 跳过迁移。

## 配置

运行配置来自 `.env`/环境变量，主要分组如下：

- `AGENT_REMOTE_ENV`、`AGENT_REMOTE_SECRET_KEY`、`PUBLIC_BASE_URL`
- `DATABASE_URL`、`REDIS_URL`、`LOG_LEVEL`
- 端口转发策略和限流：`PORT_FORWARD_*`
- Device 控制策略和证据：`DEVICE_CONTROL_*`
- 会话保留与审计保留：`DEVICE_SESSION_*`

修改生产设置前查看 `.env.example` 和当前规则文档。生产环境中的 Device 控制在缺少准确签名发布证据及策略前置条件时会 fail-closed。

## 部署

除非设置 `AGENT_REMOTE_RUN_MIGRATIONS=0`，容器入口会先运行迁移再启动 Uvicorn。生产部署必须使用持久化 PostgreSQL/Redis 数据、由部署方管理的 secret，以及运维策略要求的非零保留期。发布镜像由 `v*` tag 和仓库 workflow 构建发布。

## 脚本入口

仓库保留以下发布和验证入口：

```sh
# 运行格式、lint、类型、docstring 检查和测试。
scripts/install-githooks.sh
scripts/check_docstrings.py
scripts/run-quality-checks.sh
# 准备版本化 Server 发布和 changelog 元数据。
scripts/prepare-release.sh
# 从一次性夹具生成经过审阅的 Device 控制发布证据。
uv run python scripts/create_device_control_release_evidence.py --help
```

Docker 镜像使用 `scripts/docker-entrypoint.sh` 在启动 Uvicorn 前执行迁移。仓库没有独立的生产
安装器；请使用上文的 Docker Compose 或容器命令。发布和证据脚本只能使用一次性数据运行，发布前
必须审阅输出。

## 文档

- 架构与边界：`docs/rules/01-project-overview.md`、`docs/rules/02-architecture.md`
- 数据与身份：`docs/rules/09-data-persistence.md`、`docs/rules/10-identity-auth.md`
- Node 控制：`docs/rules/11-node-control.md`
- 当前会话/账户合同：`docs/session-lifecycle.md`、`docs/session-exact-attach.md`、`docs/session-creation-caller-key.md`、`docs/session-deletion-caller-key.md`
- 同步、导入与恢复：`docs/workspace-sync-safety.md`、`docs/account-config-import-requests.md`、`docs/runtime-recovery-requests.md`
- 端口转发与设备登记：`docs/port-forward-caller-key.md`、`docs/device-registration-idempotency.md`
- Skills 与 Device 控制运维：`docs/skill-manager.md`、`docs/device-control-release-evidence.md`

这些文档描述当前合同；迁移、schema 和测试是最终兼容性事实来源。

## 许可证

GPL-3.0-only。详见 `LICENSE` 和 `THIRD_PARTY_NOTICES.md`。
