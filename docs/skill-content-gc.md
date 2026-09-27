# 私有内容引用释放、配额结算与持久化物理删除

本实现连接历史退役与公开 prune。账户/单项选择见 `skill-prune-candidates.md`，公开确认
及原请求回执见 `skill-prune-api.md`；CLI 接入仍待完成。本层只接受调用方已经授权的
精确用户树/对象身份。没有扫描整个用户并隐式提前删除。

## 0043 删除任务

新增 `skill_content_deletions`：UUID 主键、不可级联的 user_id、digest、size、category_mask
（package=1/state=2）、status（pending/complete）、attempts、next_attempt_at、last_error_code、
completed_at 及审计时间。一个 user/digest 最多存在一个 pending 任务；完整任务保留原 UUID
以拒绝旧任务重放影响后来重新上传的文件。pending 无完成时间，complete 必须有完成时间。
category_mask 记录本次确切 deleting 分类，size 是去重后的物理文件长度。

升级前若存在旧 deleting 对象则拒绝：不能凭部署时间替未知旧标记制造删除授权。
降级遇到任何删除任务或 deleting 对象即拒绝，先检查后改 schema。旧 available 对象、树、
上传、计量和字节不回填或修改。ORM 与 Alembic 同步定义部分唯一索引及状态约束。

## 只读计划与原子应用

树选择逐项核对所有者、真实时钟/截止、硬保护和所有实际 retained 外键。普通模式遵守等待，
明确提前只越过等待；所有历史外键必须已经在同一外层业务事务中合法退役。范围包含无效身份、
树阻断或 deleting 内容时整份拒绝。完整计划记录树库存、受影响摘要的两分类对象、所有保留树
引用和有效上传身份/期限，以便在用户锁与保存点中精确重建比较；新引用/租约使旧确认失效。

只移除精确选定树的对象边和树行。某类别对象仅在移除后无树引用且无同类别有效上传时释放。
有效上传无论 reserved_bytes 是否为零都保护原对象计量。为处理上次因租约暂留的无树对象，
本层支持显式对象选择；不连带选择其他用户/类别对象。对象的物理文件仍有另一类别对象或
任何有效上传时，只删除本类别登记并减少该类别用量，不产生 deleting 标记。

最后物理保留理由消失时，释放类别用量并把本次对象置为 deleting，同时插入 pending 任务。
`*_bytes` 从此只计 available 对象，deleting 是已释放逻辑额度但尚未完成磁盘回收的屏障。
只有任务完成才移除标记行，不能二次扣减额度。计划/结果分别报告类别释放和待物理删除字节，
不会在受理时称磁盘已经释放。树、计量、标记、任务共享保存点，任何异常整体回滚；本层没有
公共幂等回执，调用方必须把原请求受理与这些变更放在同一外层提交中。

## worker 生命周期

worker 自己创建独立事务，从已提交 pending 任务读取确切 UUID，然后取得用户锁再刷新任务。
终态原任务不触碰文件。每次删除前重验两类别 marker 集合/大小、所有实际树边以及所有有效
上传；不一致保持屏障并记录固定错误码，不能猜测引用已无效。用户锁持有到磁盘线程结束及
结果提交，避免并发 worker、引用建立、过期收集器或新上传与 unlink 交叉。

私有字节层只按已授权 user/digest 用目录描述符、不跟随链接删除服务所有的普通文件并 fsync。
文件已不存在是幂等成功。取消使用既有 run_storage_io 等待线程退出后传播；若 unlink 后
数据库回滚或进程崩溃，原 pending 标记继续拒绝准入，重试确认不存在后才完成。I/O 错误保留
任务/标记并以有上限的退避重试，错误字段不能记录路径、异常正文或内容。

worker 不接受调用方未提交事务，避免消费自己的未提交标记后外层回滚。多个进程可重复投递
同一任务，用户锁和原任务终态重验负责排他。每次扫描有明确批量上限，超额留待后续轮次；
选定树/对象的计划本身则有整体资源预算，不能截断成部分成功。

应用生命周期由 `SKILL_MANAGER_ENABLED` 控制是否消费已有任务；默认开启，可显式设为 `false` 关闭。
`SKILL_DELETION_INTERVAL_SECONDS` 默认 30 秒，`SKILL_DELETION_BATCH_SIZE` 默认 100，分别有
1–3600 秒和 1–1000 项的明确边界。关闭时不访问存储，启用后只轮询已提交任务，不自动选择
任何树或历史。应用退出等待当前有界轮次，协程取消仍受磁盘线程收尾保证保护。

数据库连接失效可能先释放 SQL 锁而磁盘线程仍存活，因此用户 SQL 锁不是唯一的磁盘隔离。
字节层还在稳定用户目录描述符上持有跨进程 flock；每个原任务 UUID/摘要/长度在私有
`.deletions` 目录写入零内容的持久化完成回执。删除与 fsync 完成后才落回执并 fsync，最后
才允许 worker 提交 SQL 终态。迟到的同任务磁盘调用先看回执而不再触碰新文件。完成回执
不被暂存收集器清理，须和数据库任务一起协调备份恢复；不能单独删除这些小型元数据。

## 验证证据

新增专项集 **30 passed、3 PostgreSQL-only skipped**：
`/tmp/skill-content-gc-final-tests.log`，以及历史退役/内容结算组合 **1 passed**：
`/tmp/skill-content-gc-history.log`。覆盖独立类别计量、同分类剩余树、零预留和跨类别上传、
新消费者使计划失效、错误所有者/retained 外键、任务写入后保存点回滚、原任务重放、
先 unlink 后数据库回滚、I/O 退避、重复取消、并发磁盘线程、危险文件及真实消费者重验。

扩展 PostgreSQL 17 集 **76 passed**：`/tmp/skill-content-gc-postgres-final.log`。
数据库通过 Alembic 0043 创建：`/tmp/skill-content-gc-postgres-migration.log`。三个新独立连接
案例分别证明：未提交标记不授权 worker、同摘要上传等待实际删除与提交、真实终止 worker
数据库连接后另一 worker 完成并重新上传，迟到旧 I/O 不删除新文件。组合还包含原树时钟、
跨类别准入、目录投影、历史整理和所有新 worker/约束/生命周期测试。

独立升级库证据：`/tmp/skill-content-gc-migration-roundtrip.log`；验证脚本
`/tmp/verify_skill_gc_migration.py`。未知旧 deleting 标记在建新表之前阻断升级；恢复夹具为
available 后，0042→0043→0042→0043 保留对象/树完整指纹。即使只有已完成任务，也在任何
schema/版本/行变更前阻止降级。临时数据库容器和匿名卷已移除。

Root Compose 的默认与 device-test overlay 渲染均验证：技能开关仍 false，轮询 30 秒、
批量 100；未创建部署。协调备份文档要求保留 `.deletions` 与任务表，并先停止所有 worker
及磁盘线程。以上是 Server 存储层证据，不表示公开 prune、全量容量或真实 Node 后端验收。

另以两个独立 PostgreSQL 数据库和一次性私有内容目录执行真实 `pg_dump -Fc`、`pg_restore`
及完整 `tar` 归档/恢复。夹具同时包含 retained 文件、已 unlink 且磁盘回执落盘但 SQL 仍
pending 的任务，以及已完成任务后重新上传的同摘要内容。恢复后全部相关 SQL 完整行和
两个 0400 完成回执一致；pending 任务收敛而不重复扣减额度；原完成任务和迟到字节层重放
均保留新文件及其他 retained 内容。日志：`/tmp/skill-content-gc-restore.log`，脚本：
`/tmp/verify_skill_gc_restore.py`，0043 源库迁移：`/tmp/skill-content-gc-restore-migration.log`。
测试数据目录、数据库容器及匿名卷已清理。这是本删除协议的协调恢复证据，不替代全部
历史/运行时、Node pending 数据、混合版本升级和真实后端恢复验收。

最终完整 Server 门禁：**1240 passed、33 skipped、81.48% coverage**，Ruff format/lint、
Mypy（440 文件）、中文文档串与 whitespace 全部通过：
`/tmp/skill-content-gc-server-quality-final.log`。本轮 31 项普通新增案例及迁移头/表/索引
登记检查全部包含在最终收集中，三个 PostgreSQL-only 跳过项已在上述 76 例中通过。
没有遗留测试进程、临时数据库或夹具卷；完整 Skill 管理器目标仍在推进。

## 0044 账户续扫归属

`skill_prune_content_claims` 在明确的账户历史退役事务中记录实际 state 树/对象归属，
支持等待或零预留上传结束后的续扫。它不是保护根或 retained-history 外键；实际内容行
删除时只级联清除此派生资格，同摘要新上传不继承原资格。精确 GC 和 worker 的正常
保护、租约、配额及原任务校验不变。归属与内容必须协调备份；具体边界见
`skill-prune-candidates.md`。旧墓碑摘要和创建时间不能代替这份实际生命周期证据。
