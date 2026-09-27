# 受管账户的会话准入

启动保持原有用户、账户、工作区、后端和节点权限。用户内容写锁从运行模式判定一直持有到
账户准备、session/task 创建和精确 snapshot 引用完成。任何异常整体回滚保存点；正常的
迁移冲突先提交独立 preparation 回执，再返回 `STATE_MIGRATION_REQUIRED`，不创建 session、
任务或有效使用记录。错误详情列出保留的迁移 ID，重试不能悄悄应用旧选择。

账户无需受管内容且仍处于 legacy 时保留旧路径。启用了库内容的 legacy 账户必须先完成
目录接管。原节点和固定 Native 后端就绪时，普通启动原子预约接管并返回 `MIGRATION_PENDING`；
已有 migrating 账户复用原预约。managed_v1 即使库暂空也必须走受管路径。
关闭功能开关或所选后端能力不完整时明确拒绝，不能静默降级。

## 能力和任务契约

Node 的 `runtime_capabilities.skill_manager` 按 `native` / `docker_sandbox` 分别报告对象：
`protocol_version=1`、`manifest_version=1`、`writable_copies=true`、`finalization=true`、
`recovery=true`。版本必须为严格整数，能力必须为严格布尔值。Server 同时检查已有后端
allowlist、工具与区域匹配，以及未过期的最后心跳。缺失、错误或过期能力不会用于受管启动。
已绑定活动会话不能借此迁往其他节点；没有活动会话时可从兼容候选选择。

创建任务在同一事务追加 `skill_manager` 对象，包含协议/清单版本、snapshot UUID 与准备
任务的数据库 UUID。任务正文不含 manifest 或文件字节。Node 必须通过既有精确任务租约
下载接口读取内容；下载端也严格校验任务指针中的整数版本，布尔值不得冒充版本。快照另保存 Server 选定的系统 release 引用；这些引用不改变系统组件
已有 release/capability 闸门，也不允许普通用户树覆盖系统路径。Node 仍须完成传输和运行
验收后才能报告支持；本次 Server 接线本身不增加 Node 的能力广告。

## 全账户准备

按当前字段继承选择所有启用来源。仅尚未初始化的用户库目标调用原有首次准备服务，
已发布分支不被重置，过期分支仍要求显式恢复。准备记录使用完整前置条件派生内部重试键，
初次创建空目标身份不导致重复冲突。一次账户准备可以保存多个冲突并返回全部 ID；只有
所有来源都就绪，才能组合完整目录、验证链接/格式/额度并预约快照。

成功 preparation 不更新有效使用账本；只有成功快照预约更新。独立的准备成功与冲突可以
一同保存，以便下次继续；无效来源、额度错误等异常不能留下本次部分准备。最后的快照、
任务或审计失败也回滚本次准备、所有 session/task 引用和有效使用账本。

启动错误沿用现有 session API 的错误封套，内容额度超限返回 413，其余状态拒绝返回 409。
准备结束后重新读取节点报告，不能因 ORM 缓存继续使用已撤销的能力；复核失败回滚全部准备。

Native session admission now reserves initial account takeover under the existing user content
lock when enabled library content first requires a legacy account. The original affinity Node and
pinned Native backend must match the selected healthy compatible Node; takeover never selects an
empty source on a replacement Node. The session transaction commits only the takeover reservation
and its task before returning MIGRATION_PENDING with an operation ID and explicit no-session
evidence. Repeated attempts reuse the original receipt; no legacy writer is stopped. Existing
migrating state without consistent original reservation/task evidence fails closed. Owner/device
GET /sessions/skill-takeovers/{operation_id} reads bounded metadata without renewing or creating work.
Only committed authority permits a fresh session-creation attempt. Docker takeover admission remains
unsupported until its independently verified writer adapter is integrated.

## 首次接管与启动恢复

普通启动在同一用户锁下固定原账户节点和 Native 后端。备用节点不能代替保存旧目录的原节点；
原节点不兼容、未知绑定或 Docker/sbx 尚无接管适配器时返回 `SKILL_MANAGER_UNSUPPORTED`，
不创建预约。预约后的能力复核失败回滚全部本次目录模式、收据和任务。

成功预约只提交 migrating 模式、不可变历史写入者清单与唯一接管任务。HTTP 409 的
`MIGRATION_PENDING` 详情返回 `account_id`、`takeover_id`、`takeover_status`、
`reservation_committed=true` 和 `session_created=false`。这些字段不是快照或启动成功凭据。
原会话、工作区与替代会话授权仍先验证；原 legacy 会话继续运行，不能为新启动强停。

再次启动复用原预约。目录纪元、原 Node/backend、任务身份、载荷或可重试状态不一致时，
返回 `TAKEOVER_RECOVERY_REQUIRED`，不能删除原记录或创建替代任务。缺少接管收据的旧
migrating 状态也需要恢复。完整初始目录提交后，普通启动才执行全账户准备和快照预约；
手工内容与二进制学习状态保留为账户本地来源，不提升到用户库。

`GET /api/v1/sessions/skill-takeovers/{operation_id}` 使用既有会话用户／设备凭据，Node
凭据被拒绝。它只读原所有者的接管阶段、原账户、任务状态、初始 checkpoint 和
`recovery_required`，不返回写入者清单、文件、宿主路径，也不续租或创建任务。关闭新准入
开关后仍可读取。reserved 不能推断 Node 已捕获；只有 committed 证明初始目录权威提交。

`fclaude` 在收到上述精确“未创建 session”凭据后打印操作 ID，并在同一个 60 秒期限内
只读轮询接管。committed 后用完全相同的启动输入再提交一次；该次 POST 的不确定响应
绝不自动重发。期限包括 HTTP 请求；超时退出 3、Ctrl+C 退出 130、需恢复或协议错误退出 1。
已存在会话的 attach 流程保持原行为。
