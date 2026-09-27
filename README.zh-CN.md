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

只有所选 Node 为 session backend 明确上报 capability 时，控制面才允许创建端口转发。当前发布同时支持 Native Runtime 与 Docker Sandbox session；backend capability 或受信 runtime state 校验失败时仍会 fail closed。应用数据直接在设备与 Node 之间传输，不经过控制面。

管理前端可以删除失败或已暂停的同步会话。活跃的本地 Mutagen 会话必须先在所属设备上暂停，
控制面不会静默遗留运行中的同步进程。

受管 Native 启动已有认证的结果确认接口，绑定原始快照、任务记录和领取轮次。已提交结果的
精确重试只返回历史收据，不会复活已停止会话或重新引用已退役技能内容。通用任务结果接口也
执行相同的受管授权校验。只读查询可区分已提交收据和旧轮次未提交结果，不续期授权。
Native worker 已接入此契约；收尾传输、重启恢复和运行时验收完成前仍不广告受管能力。

受管 Native 终止现有独立精确快照接口和不可变收据（迁移 0048）。它取消尚未确认的原始启动任务、
保留已确认启动历史，并在同一事务撤销设备和浏览器绑定。Node 首次上传冻结内容前先确认终止；
stopped、persisted 和 published 仍是不同结果。详见[终止契约](docs/skill-session-termination.md)，
此接口本身不启用 backend 能力。

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

设备控制默认关闭。`AGENT_REMOTE_ENV=production` 时，当前版本启用该功能必须提供随根版本一起发布的 schema 9
证据清单，并绑定精确的根分发版本、Server/组件/制品组合且由固定 Ed25519 公钥验证通过；schema 9
没有 `expires_at` 字段，对同一签名组合永久有效。已签发的 schema 8 清单对其精确签名组合仍永久可验证，
但只能授权 legacy `per_application_approval` 策略；生产 `session_full_trust` 未提供 schema 9 时会 fail closed。
公钥使用 Base64 编码。开发环境只能为
不含敏感数据的测试显式启用该能力。生产部署还必须显式选择非零的终态 session 和设备 session
审计保留天数，且审计保留期不得短于 session 保留期。

Computer Use v2 对新 generation 默认启用。只有 Node 广告完整必需的
`observation_mode_v2`、`ax_state_v2`、`adaptive_settle_v2` 基础集合时，Server 才会协商
v2，并带上双方支持的 `clipboard_payload_v2` 等扩展。缺失、部分、未知或畸形集合会原子回退
到 v1。设置 `DEVICE_CONTROL_V2_ENABLED=false` 可在紧急情况下强制新 generation 使用 v1；
活跃 generation 不会原地改变 capability 集合。

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

受管会话停止响应提供 `skill_finalization_operation_id`（原始快照 UUID）。原用户可使用既有用户或
设备会话令牌查询 `GET /api/v1/sessions/skill-finalizations/{operation_id}`，删除会话后仍可查询。
接口分别报告进程终止确认、完整内容保存、最新发布状态和内容保留状态，详见
[停止与保存状态](docs/skill-stop-status.md)。

Native 账户接管任务已用精确领取轮次租约覆盖 Helper 捕获与冻结内容上传。通用完成接口
必须验证原始初始 checkpoint 已提交及精确结果；失败回报不能消耗待重试预约。Server 与
Helper 持久记录保证重试不重复导入后来的原目录改动。普通 Native 会话启动已接通预约，
混合后端验收仍待完成，
本次集成不启用受管能力广告。

首次受管 Native 会话启动会在原账户节点预约目录接管，返回包含持久操作 ID 和“未创建会话”
证据的 MIGRATION_PENDING。重试复用同一预约，已有 legacy 会话继续运行。
GET /api/v1/sessions/skill-takeovers/{operation_id} 允许原用户／设备只读查询进度，关闭新准入
开关后仍可读取。fclaude 最多等待 60 秒，只有初始 checkpoint 提交后才再次创建会话。
原目录绑定未知、任务证据改变或后端不兼容时，不会改用其他机器的空目录。

部署受理现为每个原账户目标保存独立尝试。内部重试事务固定原始计划，只为明确选定的临时失败
追加后继，保持已成功目标不变。历史不一致时，状态查询及保留分析会拒绝继续。公共重试提交
及回执查询现已通过原操作的 `/retries` 子资源提供，普通轮询现会调度兼容的待执行目标。详见[尝试协议](docs/skill-deployment-attempts.md)。

技能配置变更现会比较新旧保存计划：只有未完成的受影响目标选择实际改变，才将旧操作标记为
`superseded` 并展示第一次替代它的操作 ID。迟到的目标成功不会把旧操作重新变为 ready。
账户 pin 未变、无关配置、暂存候选及已完成目标不会仅因代数变化触发替代。仍执行或有冲突的
目标保留原内容引用，直到独立结束；该配置边界不代表 Node 已取消任务或 Helper 已永久排空。

内部部署预约现保存完整账户目录输入，并绑定原始尝试和精确 Node 任务；重试复用原输入，
授权时重新检查租约、有效配置及状态纪元。即使尝试被标为失败或配置被替代，仍活动的任务
继续保活内容。迁移 0050 新增该绑定，不伪造会话使用或 Node 就绪。普通轮询现调用原预约服务，
执行终态须经过专用撤权和排空确认；尚未声明部署能力。详见[部署预约边界](docs/skill-deployment-dispatch.md)。

专用 Node 部署清单、文件和续租接口现按精确原任务提供完整输入。每次请求绑定当前领取轮次；
文件复制释放数据库锁，完整校验后重验权限才发送字节。对应 Go 客户端校验原计划和目录摘要、
全部归属以及短期租约。下载可用状态为 `prepared_input`，不代表 Node 执行就绪。

专用部署结果确认现原子保存原始 Helper 准备回执、任务成功和目标就绪。即使输入已按规则
退役，精确重放及只读观察仍可恢复已提交结果。Worker 在确认前持久保存提案；通用任务完成
仍不可用于部署。详见[结果协议](docs/skill-deployment-results.md)。

部署终止现先持久撤销原尝试执行权，再接受 Helper 的独立排空回执。确认事务只结束原任务和
原目标；精确历史重放可跨越输入退役，且不覆盖后继尝试。迁移 0051 新增不可变撤权意图，
重试调度须核验已接受的排空结果。Worker 编排已持久恢复撤权、排空与确认各阶段，详见[终止协议](docs/skill-deployment-termination.md)。

首次账户接管现通过独立的部署发现记录补充手工来源，保留原已接受配置计划，并固定原账户、
接管回执、初始版本及目录纪元。实际任务输入、重试、配置替代与内容保留使用同一保存解析。
详见[部署发现](docs/skill-deployment-discovery.md)。已知兼容的 Native 目标（包括离线节点）以 pending 受理；
普通认证轮询有界调度原目标，并在冲突解决后继续准备。已有任务绑定仍由专用执行和排空
协议管理，详见[调度边界](docs/skill-deployment-scheduling.md)。完整运行时验收仍待完成，未启用 Node 能力。

Native 冻结快照导出使用用户授权接口
`/api/v1/skills/state/node-exports/{snapshot_id}/authorize` 和原 Node 重验接口
`/api/v1/node/skill-state-exports/{snapshot_id}/verify`。配套 CLI 经受限 SSH 直接读取已有冻结数据，
Server 状态配额耗尽不阻断恢复。原用户、设备和公钥须持续有效；授权不证明本地内容存在或已上传。
详见[导出协议](docs/skill-node-export.md)。
