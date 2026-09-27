# 账户版本状态迁移

本域落实跨仓库设计第 6 节。迁移不是会话收尾，不创建虚构 session、snapshot 或
finalization。全部写入持有用户内容锁；完整输入、目标分支、目录 head 和幂等回执
共享事务。迁移冲突作为正常受理结果提交，不能在抛出会话准入异常时丢失。

## 持久化边界

`0034_skill_branch_preparation` 增加两类记录：

- `skill_effective_branches`：账户、稳定安装身份、安装纪元对应的最后一次成功快照预约。
  引用实际 snapshot member 和同源分支；只在整个快照预约成功后写入。
  查询、预览、失败预约、迁移失败和旧会话晚到收尾均不得更新它。
- `skill_branch_preparations`：首次进入版本的独立受理记录。保存用户幂等键、请求摘要、
  目标分支、可选来源分支和来源 checkpoint、双方 epoch、目录 epoch、配置代数、
  三份完整比较树及冲突元数据。成功时保存目标 checkpoint 和目录 checkpoint。
  复合外键保护账户、稳定安装身份、安装纪元和内容所有者；存在记录时禁止降级丢失历史。

自动来源为最后成功预约分支的**当前已发布 head**，不是最后登记版本或最后写入分支。
目标已有 head 时直接继续该 head，不重放旧变化。无来源历史时使用原始包；有历史快照成员
却缺少可信使用顺序时拒绝猜测；仅 reset 初始化而从未实际预约的分支不算使用历史。
目标登记编号早于来源时从目标原始包开始，返回
`newer_state_not_migrated`。编号只表示登记顺序，不表示上游兼容性。

首次向前迁移三侧为 `old_original`、`new_original`、`old_published`。独立成员的三侧
均为带原始前缀的完整成员树；来源 checkpoint 另行保留完整目录。
只有明确目标成员可以自动变更；跨成员或辅助根的关联单元保守保留完整来源为冲突，
不把旧输入中的其他来源复制到当前目录。完整结果校验相对链接、实际内容、单项与用户配额。
发布使用目标 head/epoch 和目录 head/epoch CAS，失败回滚完整事务。

reset/restore 将账户内待解决迁移标为 superseded，保留原始输入；重置后的目标已有 head，
后续准备直接复用。旧幂等请求返回原始受理结果，不重新运行迁移。

## 接口和调用顺序

`POST /api/v1/skills/state/prepare` 接受单项 selector、`expected` 完整当前状态、
`idempotency_key` 和 `dry_run`。只处理当前有效且启用的用户库来源。
`GET /api/v1/skills/state/preparations?key=…` 查询不可变原始回执和当前失效状态。
预览不创建分支、内容上传、预约历史或回执。正式请求返回 ready/conflicted 并提交；
公开会话入口在同一用户锁及外层保存点内编排全部有效目标，再固定快照；正常冲突
则提交准备回执后拒绝启动。该编排与能力检查见 `skill-session-admission.md`。

迁移不复用普通 finalization 的冲突解决计划。显式增量迁移及 last-migrated 基线见下节；
迁移专用三侧导出、用户解决和关联单元选择见 `skill-migration-conflicts.md`、
`skill-migration-resolution.md` 和 `skill-migration-recomputation.md`。

## 验证记录

完整 Server 门禁通过：652 tests passed、15 skipped、76.81% coverage，Ruff、Mypy、
中文注释和空白检查通过。52 个准备、认证、约束、快照与状态命令用例在 PostgreSQL 17
迁移创建的结构上通过。0034 升降级回环和 ORM 约束对比通过；有数据降级被拒绝，
83 份准备回执和 139 条有效分支记录原样保留。临时数据库和匿名卷已删除。

## 显式增量迁移事务

`0035_skill_incremental_migration` 扩展独立准备记录，加入 `mode=incremental`、
`base_checkpoint_id`、`current_checkpoint_id` 和成功迁移序号 `migration_sequence`。
基线引用只能属于来源分支，当前侧引用只能属于目标分支。序号在来源/目标分支、
双方 state epoch 和 directory epoch 的组合内唯一递增；只有完整成功的 forward 或
incremental 记录拥有序号。最新成功记录的 source checkpoint 就是 last-migrated
checkpoint，不另建可提前更新的游标。失败、冲突、预览和重放不推进序号。

迁移 0035 为已有 successful forward 记录补序号 1，不改写其原始 JSON 回执。
若旧数据违反同一首次目标只能成功初始化一次的约束，升级失败并回滚，不能按时间猜测。
存在 incremental 记录时禁止降级；仅兼容的 forward 序号可以由 0034 的原始成功记录重建。

显式 from/to 可指定同一活动安装当前纪元内的任意两个不同登记版本，不必改变 pin 或
启用状态。来源必须有完整已发布 head；目标缺失时用其原始包作为 current。
第一次迁移使用来源原始包作为 base；已有成功同纪元记录时使用其来源 checkpoint。
current 始终是目标自己的 head，不能用当前目录展示的其他版本代替。输入来自来源当前
已发布 head；晚到旧会话写入因而只在显式请求时合入目标。

`GET /skills/state/migration/current` 查询双方 head/epoch、目录 head/epoch、配置代数和
精确上次成功迁移记录，显示是否有尚未迁移来源 checkpoint。`POST /skills/state/migrate`
必须回传完整 expected、持久化键及 from/to。预览不写入；正式请求保存完整三侧，
并原子交换目标和目录 head、保存成功序号与回执。规则和有效使用顺序保持不变。
reset/restore 的纪元变化形成新的明确迁移范围，旧迁移基线不能隐式跨纪元复用；
用户新请求可明确从来源当前状态重新迁移。旧冲突仍保留而不推进 last-migrated。

内容和链接处理继续使用完整目录校验与不透明保护。关联单元的迁移专用用户解决流程
仍需单独实现；显式 migrate 不是绕过 source-conflict 的强制覆盖选项。

增量接口响应分别标记 `old_original/last_migrated`、`target_original/target_published` 和
`source_published`。`changes` 比较目标自己的原 head，`directory_changes` 比较当前完整
目录，不能把两个基线混为一谈。来源 checkpoint 没有新增时，正式命令保存一份成功回执
但保持双方 head 不变，目标自己的修改和删除不会被重复迁入的旧数据覆盖。

`GET /skills/state/migration/operations?key=…` 返回原始增量结果及当前失效状态；
首次准备和显式迁移共用用户幂等键空间，但回执接口各自使用准确的类型。错误入口返回
`OPERATION_KIND_MISMATCH`。不存在可读取的 last-migrated 内容时明确失败，不回退原始包。
升级 0035 必须排空旧 Server 写请求，因为旧代码不能写入新成功序号约束。


0035 验证：完整 Server 门禁 672 passed、15 skipped、77.15% coverage，格式、静态检查、
类型、中文注释与空白检查通过。72 个迁移及关联用例在 PostgreSQL 17 上通过。带历史
升降级回环保留 7 份首次成功迁移的原始 JSON，补序号 1；故意重复的旧历史使升级整体
回滚。新列、空值约束、外键列映射、唯一约束列和检查名与 ORM 一致。有 incremental
数据降级被拒绝，16 份回执、状态、成功序号、精确引用及 JSON 原样保留。临时数据库
和匿名卷已清理。

迁移 `0036_skill_migration_resolution` 在此独立迁移身份上增加人工上传绑定、完成内容授权、
版本化计划、选择和不可变回执；不会改写本文件所述原始准备/迁移响应或提前推进成功序号。
详细外键、恢复与降级约束见 `docs/skill-migration-resolution.md`。完整解决发布编排仍需接入。

## CLI 增量迁移和操作身份查询

CLI `skill state migrate SKILL --account-id UUID --from-revision rN --to-revision rM`
已接入只读选择、完整预览、一次确认、元数据请求日志、断线恢复和状态查询。
预览固定双方稳定版本 ID、完整前置条件与三侧摘要；目标分支 diff 和目录 diff 分开显示。
正式请求必须与已确认预览一致；只有原键明确未受理才允许原样重放。
冲突预览不写入，正式冲突保留全部输入；两者都返回 CLI 退出码 1。完整成功为 0，
当前 superseded 为 1，参数错误为 2，Ctrl-C 为 130。同步发布没有额外部署等待队列。

新增 `GET /skills/state/migration/operations/{operation_id}` 与原键查询使用相同所有者约束、
类型检查和回执构造。外层 status 表示当前状态；`data.result` 永远是原始受理结果。
解决完成后外层可为 ready 而原结果仍为 conflicted；reset 后可为 superseded，均不改写历史。
错误身份返回 OPERATION_NOT_FOUND；首次准备身份返回 OPERATION_KIND_MISMATCH。
不增加 schema、运行时能力声明或公开接管调度。

本增量验证：完整 Server 门禁 1092 passed、17 skipped、79.45% coverage；Ruff、Mypy（374 文件）、
中文注释与空白检查通过。CLI 完整门禁 476 Rust tests；真实 CLI 到用户认证 Server 的 26 条命令
覆盖晚到增量、重复无变化、反向迁移、冲突/reset 与断线原键恢复。运行时后端验收仍单独待完成。
