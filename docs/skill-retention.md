# Skill 历史保留与回收

本域落实根设计 §4.5、§8.2。prune 必须同时提供候选预览、一次确认、原请求恢复、
精确引用复核、历史退役、配额结算和可恢复物理删除；只将 retained 改为 false 不构成完成。

## 保活分析

分析读取同一用户、同一事务中的完整引用索引。SQL 仅在仓储；服务取得既有用户存储读锁；
纯图算法计算保护闭包及理由，不创建记录或改变保留时钟。明确区分以下引用：

- 活动安装的默认包、工具及账户 pin；当前账户解析版本的分支，即使规则停用也保活。
- 活动账户本地来源的初始版本及初始目录，包括停用条目，供 reset 使用。
- 已预约、运行或收尾中的精确 session snapshot，包括未上传、已持久化待发布输入。
- 未解决发布/版本迁移冲突的完整三侧、关联分支及人工解决内容。
- 仍匹配双方纪元及目录纪元的最新成功增量基线，随需要它的目标分支保活。
- 未完成接管和有效上传租约；上传按实际对象摘要保活，不把所有已完成上传永久视作根。
- 当前账户目录的完整内容。目录成员另列为必须处理的物化引用，不能把其中历史版本的
  head 永久提升为当前分支，也不能在目录内容仍可见时直接删除它们。

checkpoint 的 parent 是审计关系，不传播保活；有效使用顺序记录也是身份历史，不能永久
保活早已离开当前集合的全部分支。终态回执保持原始响应，内容的保留资格独立裁定。
逻辑对象按 package/state 分类，底层文件按用户/摘要共享；另一分类或活动上传引用相同
文件时，释放某分类额度不授权物理删除该文件。

内部分析输出的是保护理由和目录物化义务，不是可立即删除清单。目录成员、旧 snapshot、
finalization、迁移树和人工授权仍存在强外键；后续显式退役必须按依赖顺序解除内容引用，
保留身份、摘要和原始回执。尚未经过退役、保留期和新引用复核的对象不得进入删除阶段。

## 保留时钟与退役规则

普通历史从最后解除保活引用时计 30 天，卸载归档计 90 天；再次建立保护后取消时钟，
下一次解除重新计时。不能使用 checkpoint 创建时间，也不能让 preview 推进或重置时钟。
这些时钟必须与改变配置/head/session/conflict 引用的事务一起更新；单次 GC 扫描观察时间
不能替代真实解除引用的时间。schema 变更与各写入域的时钟接入应一起验证。

prune 只处理明确账户、skill 或完整目录中的运行状态。默认候选必须到期；
all-unreferenced 只提前终止普通历史等待，不能越过保护闭包。若当前目录仍物化历史成员，
候选预览须包括目录整理的完整影响，校验关联链接与所有单项根后原子发布整理后的目录，
再退役原目录历史；不修改技能包、pin 或启用规则。

两阶段删除先在用户锁内复核和提交 deleting 标记及精确配额变更；物理删除使用持久化
任务与重试状态，不能在数据库回滚后留下被删除的有效文件。新上传/引用遇到 deleting
必须等待/重新完整验证，不能复用即将消失的文件。普通用户读取不能把失去内容的墓碑
称为可恢复对象。历史分支退役留下 expired，后续选择要求显式 reset/restore。

公开 prune、内容引用退役与物理 GC 尚须在这些规则之上完成；内部引用分析与下文的
历史时钟通过不代表删除流程已可交付。

## 当前实现与验证边界

`repositories/skill_retention.py` 在用户锁内读取统一索引，显式忽略不参与引用判断的
大型 provenance、原始请求、回执、配置和冲突 JSON。总行数上限 1,000,000；必需上传清单和
库操作结果合计最多 16 Mi 字符，解码前由数据库检查（UTF-8 至多 64 MiB）。图上限为
1,000,000 节点、4,000,000 边；超限整体失败，受损图不能再输出闭包。当前实现不能把
这些上限当作性能或容量验收结论。

`skill_retention_schema.py` 明确列出当前 38 个 skill 引用表的角色；新 skill 表或其他领域
新增的 skill 外键引用表必须先分类，否则分析拒绝继续。此检查保护表级新增边界，不能
代替对已有表中新增字段和状态的逐项评审。未知配置操作状态或缺失精确版本的待处理操作
同样拒绝返回貌似完整的分析。已知待处理操作仅按保存版本保活，不把历史回执永久视作根。

当前目录 head 只保护自身内容；其物化成员作为独立整理义务返回。snapshot、finalization、
冲突比较或本地初始状态引用的完整目录则使用 directory_context，继续保留成员检查点。
因此不能把当前目录的历史成员误当成永久当前分支，也不能忽略活动流程的完整上下文。

`SkillRetentionInspector` 是内部只读入口，不暴露公开 HTTP 操作，不退役引用、不更新时钟、
不删除对象。新用户没有存储行时直接返回空视图；已有用户使用与所有写入相同的存储用户锁。
实际 PostgreSQL 17、迁移创建至 0039 的结构上，23 个引用分析用例通过，包括独立连接
证明分析锁阻塞新引用写入。临时数据库容器与匿名卷已删除。SQLite 常规门禁中的对应
PostgreSQL 锁用例显式跳过，不将 SQLite 结果当成生产行锁证据。

引用分析阶段的完整 Server 门禁通过：1114 tests passed、18 skipped、79.80% coverage；Ruff、Mypy（389 文件）、
中文注释与空白检查通过。新增跳过项为已经在独立 PostgreSQL 运行中通过的行锁用例。
日志 `/tmp/skill-retention-server-quality.log`；数据库专项日志 `/tmp/skill-retention-postgres.log`。

## 0040 历史释放时钟 schema 与事务边界

七类保留身份（包版本、本地初始版本、checkpoint、session snapshot、finalization、
publication、branch preparation）增加可空 `retention_released_at` UTC 时间戳；其余内容、
身份外键和回执不变。字段只记录最后一次解除有效保护的事务时间。保护期间为空；旧数据
无法证明释放时间也为空。迁移不按 created_at 或部署时间补写，降级在任一时钟非空时拒绝。

显式异步 `retention_mutation` 在同一用户存储锁和保存点内，先冻结完整身份/保护集合，
执行业务变更，再读取最终保护集合并更新时钟。新建且无保护的候选从该写事务进入历史；
既有未保护身份不会因扫描启动或重置计时。再次保护清空字段，下一次解除重新计时。
同事务嵌套业务调用合并为最外层边界，内层失败仍回滚自己的保存点；外层回滚同时撤销
引用和时钟。dry-run 不执行时钟分析和写入。禁止在提交钩子或同步 ORM 事件中访问数据库。

这些身份时钟不是物理对象时钟，也不授予删除权限。上传租约只指向对象而不反向保护
上述身份，因此自然到期不改变这些身份的保护状态。暂存对象继续由租约到期流程处理；
完整对象 GC 仍需独立精确租约截止、引用退役和持久化删除任务。历史等待期限由当前部署
策略与卸载纪元判定；未知释放时间不进入默认到期候选。当前目录整理义务仍须先完成。

内部 history 视图在同一只读用户锁下返回保护理由、原始释放时间、归档分类和当前策略的
等待截止时间。包按安装移除状态、本地版本按来源移除状态、分支按来源/安装纪元判断归档；
完整目录与会话只要包含归档成员便保守使用归档等待期，比较身份继承其关联来源分类。
重新安装后的旧纪元分支仍属于归档。日期只描述历史等待；即使到期，仍必须处理完整目录
物化义务、强引用退役和新引用复核。未知时钟、仍受保护内容不提供可用截止时间。


## 0040 当前接入与验证

真实历史写入已接入显式时钟边界：库配置与纪元、reset/restore、准备/迁移、两类解决、
预约、收尾接收与完整持久化、发布及重算、本地候选、接管完成、账户覆盖清理与会话关系
释放。Node 任务成功/失败和对账终态、interrupted 会话停止时重建保护也纳入同一边界。
这些会话入口在任务/会话变更前取锁；多用户对账按用户 ID 固定顺序取得已有存储行，
不为未使用技能的用户创建计量记录。上下文退出后才允许外层提交和外部撤销通知。

新增 13 个时钟专项用例，其中两个独立连接 pin 竞争仅在 PostgreSQL 运行。合并原引用
分析测试与捕获错误回归，实际 PostgreSQL 17 上 37 例通过，schema 由 Alembic 创建至 0040；不是用
create_all 替代迁移。独立数据库副本逐表验证七个降级保护，任何非空时钟都在删除列前拒绝；
全空时钟的 0040→0039→0040 保持七表全部身份/内容/回执指纹且不填补旧释放时间。
日志 `/tmp/skill-clocks-postgres.log`、`/tmp/skill-clocks-postgres-migration.log`、
`/tmp/skill-clocks-migration-roundtrip.log`。临时数据库容器和匿名卷已删除。

尚未实现的运行 snapshot started/cancelled 写入必须在新增时接入上述边界。内部截止诊断
还没有暴露到 CLI/API info/status。树/对象租约、强外键退役、目录整理和物理删除需继续实现；
不允许仅凭这些时钟启用公开 prune、接管 dispatch 或 runtime capability。


保存点入口先更新并锁定既有计量行；没有行时执行空更新，确保 SQLite 也建立真实外层
写事务，再在保存点内创建新计量行。这样业务错误被捕获后提交外层不会留下失败请求的
新行，服务成功后外层回滚也不会被 SQLite 的顶层 SAVEPOINT 释放语义提前提交。
完整门禁曾发现前一种回归；已有跨用户回执测试与时钟回滚测试联合覆盖修复。

## 0040 阶段的内容退役 schema 清单

按 0040 ORM 外键实查，九张历史表仍有十一条直接 `skill_stored_trees` 外键，另有
`skill_tree_object_references` 的树/对象两条依赖外键。下一阶段必须处理以下差异，不能
仅清空 checkpoint 的 tree_digest 后就尝试删树：

| 历史身份 | 当前内容引用 | 退役必须保留的证据与约束 |
| --- | --- | --- |
| skill_revisions | tree_digest 可空 | content_digest、稳定版本号、来源与 retained 语义 |
| account_local_skill_revisions | tree_digest 可空 | content_digest、来源、子树前缀与 retained 检查 |
| skill_checkpoints | tree_digest 可空 | content_digest、parent/backing 身份及独立账户归属；还须解除含 tree_digest 的入向强 FK |
| session_skill_snapshots | tree_digest 非空 | 完整树审计摘要、原 session/task、实际 snapshot items；需独立可退役内容引用 |
| skill_finalizations | tree_digest 可空但终态检查要求非空 | incoming_digest、checkpoint 身份、原回执；调整约束时不能丢掉独立 owner/account 绑定 |
| skill_publications | current_tree_digest 可空 | 原比较摘要、完整冲突/发布回执及关联分支；未解决仍禁止退役 |
| skill_branch_preparations | base/current/incoming_digest 非空 | 三侧审计摘要、原响应、成功基线身份；可退役完整比较不能破坏仍有效的精确增量基线 |
| skill_resolution_choices | tree_digest 可空但人工选择要求非空 | 原选择与摘要；需要将身份和可退役内容引用分开 |
| skill_migration_resolution_content | tree_digest 同时为主键与非空 FK | 原授权身份和选择 FK 不可清空/删除，需另设可退役内容引用 |

当前 upload 的 tree_digest 是清单/上传身份，并不直接引用 stored tree；物理保活仍依据
活动租约内的实际对象摘要。历史上传/transfer 审计如何缩减必须另行保持重试与原始响应
契约，不能把清理旧 manifest JSON 当作内容退役的替代步骤。此清单记录 0040 阶段的待办；下文 0041 已实现比较历史引用的退役，
checkpoint/原始版本退役、目录整理和物理删除仍待完成。

后续启用 GC 前还必须完成部署切换约束：未接入时钟的旧 Server 写进程不能与回收器混用。
旧进程可能在两次新进程观察之间重新建立并解除保护，现有字段无法证明那段释放历史。
因此当前时钟 schema 的兼容升级不等于允许混合版本启用删除；切换/恢复方案需明确处理
无法证明完整写入覆盖的旧时钟。当前尚无公开 GC，此项列入发行与备份恢复验收。


0040 最终完整 Server 门禁通过：**1125 passed、20 skipped、80.42% coverage**；Ruff 格式与
检查、Mypy（394 文件）、中文注释及空白检查均通过。新增两个跳过项是已在 PostgreSQL
37 例复验中通过的独立连接时钟竞争。最终日志 `/tmp/skill-clocks-server-quality-verified.log`；
首轮失败日志 `/tmp/skill-clocks-server-quality.log` 保留，不能当作最终通过证据。

## 0041 比较历史内容退役 schema

snapshot、finalization、publication、branch preparation、publication resolution choice 和
migration resolution content 增加可空 `content_retired_at`。原摘要、主键、来源、checkpoint
身份、选择和原响应保持原样；直接树外键改为持久化生成列 `retained_*`，其值为未退役时
的原摘要，退役后为 NULL。生成列避免每个写入者重复维护摘要和引用。旧行默认未退役，
升级不释放内容。PostgreSQL 生产迁移与 SQLite 测试 ORM 都使用 stored generated column。

snapshot 仅 retained/cancelled、finalization 仅 published/conflicted/detached、publication 仅非
conflicted、preparation 仅非 conflicted 才允许退役。finalization 的 conflicted 是原收尾分类，是否仍有未解决 publication 由完整硬根复核。
选择与人工授权随所属比较一起退役，
不能因共享摘要仍可读取就把已退役的历史比较称为可恢复。finalization 的 checkpoint
复合 FK 改为绑定 checkpoint.content_digest，继续严格约束 owner/account/scope/identity/
原内容身份，同时允许该 checkpoint 日后显式释放自己的 tree_digest。

内部退役入口仅处理明确账户和精确历史身份，先检查当前完整硬保护、真实截止时间与
父历史依赖：snapshot 不能先于仍保留的 finalization，finalization 不能先于仍保留的
publication 退役。普通请求必须到期；显式 all-unreferenced 仅越过历史等待，不越过硬根。
整份选择先验证后修改；受影响的选择/授权、历史退役时间同事务提交和回滚。此阶段不
退役 checkpoint 或技能包、不删除树/对象、不结算额度，不提供公开 prune。完整 prune
还需当前目录整理和 checkpoint 退役事务以及持久化回收任务。

历史查询保持原身份和原始响应，已退役比较的可用 tree_digest 返回 NULL，内容 diff/export
明确 STATE_EXPIRED。保活图不再从已退役比较传播完整内容，但仍独立保留有效的精确成功
增量基线。新建 active snapshot/finalization/conflict 不能使用退役标记绕过内容外键。
降级先检查全部六表，只要存在退役证据便拒绝；全未退役时才可恢复原内容外键。


## 0041 当前实现与验证边界

内部 `SkillHistoryRetirementService` 一次最多处理明确账户中的 1000 个不同历史身份；
默认要求可证明的等待期已到，显式提前只越过等待。选择全部验证后才写入退役标记；
所属 publication choices 和 migration custom grants 同时退役，完整父历史未退役时不
允许先退役其输入。有效未完成的迁移人工上传也是退役阻断条件；上传状态与原键仍可
查询，已退役输入不能通过旧 complete 请求重建内容授权。对象租约的截止与物理 GC
仍独立处理，这个额外传输阻断条件不重写七类历史的释放时钟。

读取端把已退役比较的可用侧设为 NULL，diff/export、新解决或重算返回 STATE_EXPIRED。
原选择与原始受理重放保留完整审计信息。增量基线继续通过独立 migration_baseline 指向
精确 source checkpoint，退役完整比较不会切断下一次增量迁移。迟到运行回报若试图
重新保护已退役 snapshot，保活分析拒绝并回滚任务结果和会话状态。

11 个新用例覆盖真实截止、硬保护、父历史依赖、整份选择及外层回滚、原回执重放、
同摘要内容仍存在时的过期读取、有效上传、人工授权主键、数据库活跃状态限制、退役后
同摘要跨账户 checkpoint 替换拒绝、真实生成外键释放、连续增量迁移和迟到回报回滚。
生产 service 不删除树/对象；删除测试只在私有 fixture 数据库证明内容 FK 已解除，
不把此测试称为物理 GC 或配额结算实现。

实际 PostgreSQL 17 上新旧保留用例共 **47 例通过**，结构由 Alembic 创建至 0041。
独立数据库的六表逐项降级保护、八个生成列与完整 0041→0040→0041 历史指纹往返也通过。
日志 `/tmp/skill-retirement-postgres-verified.log`、`/tmp/skill-retirement-postgres-migration.log`、
`/tmp/skill-retirement-migration-roundtrip.log`。临时数据库容器和匿名卷已删除。

当前仍不是公开 prune：checkpoint、分支 expired、完整目录整理、精确预览/原请求恢复、
额度结算与持久化两阶段删除尚待实现。六表退役字段也不能授权旧 Server 写进程与回收器
混用，发行/备份恢复必须完成上节列出的完整写入覆盖约束。

## 下一阶段 checkpoint 与目录整理约束

0041 后 ORM 中已没有指向 checkpoint.tree_digest 的入向 FK；身份/内容证据 FK 均可在
保留 tombstone 时继续成立。但这并不自动授权清空 checkpoint 内容。尚未退役的 snapshot、
finalization 和完整比较仍承诺完整输入；其历史等待与引用依赖也要纳入候选计划。

目录整理也不能只删除 `SkillDirectoryMember`：当前分支 checkpoint 自身的 tree_digest
及 backing_directory 仍可能保活包含其他历史根的完整旧树。后续整理必须分析完整树、
各受保护单项视图与跨根链接，明确是否需要生成等价的新物化视图，并验证单项文件、
权限和链接语义不变后原子发布。不能为了释放额度改写仍有效的精确增量基线或活动流程
输入，也不能以“有共享引用”为由假报已释放空间。预览应覆盖整理带来的全部 head/
checkpoint 变化及真实仍保留引用，最终配额只能按提交后的可达性结算。

## checkpoint 退役事务设计（0041 既有 schema）

内部精确历史选择扩展到 checkpoint，与比较历史共享账户授权、保留截止、用户锁和保存点。
复用既有 retained/tree_digest 与分支 expired 字段，不新增 schema。待退役 checkpoint 若仍被
未退役 snapshot 成员/起始目录、finalization、publication 或 migration 的精确输入/结果引用，
必须将相应历史一起选入并分别通过期限与保护校验。保留目录的成员和保留 item 的 backing
目录同样构成退役依赖；普通 parent 与终态回执只是身份引用。目录成员记录不删除。

因此当前目录仍物化的历史成员暂时返回 HISTORY_REFERENCED，不能绕过未来目录整理。
选中非当前分支的 head 时，同事务标记该分支 expired 并保留原 head 身份；其他历史 checkpoint
退役不使仍有有效 head 的分支过期。重新选择 expired 分支仍可查看状态，但预约/迁移必须
STATE_EXPIRED；只有显式 reset/restore 可建立新 head。过期分支的 head 不再是内容根，
精确 snapshot、基线或当前目录引用仍独立检查，不得重新保护已退役 checkpoint。

本步骤只解除 checkpoint 的树引用，不删除树/文件、结算配额或实现目录整理，也没有公开
prune 入口。服务调用成功仍由外层决定提交；一次选择失败不能留下部分退役或 expired 标记。

0041 比较历史退役最终完整 Server 门禁：**1136 passed、20 skipped、80.54% coverage**；
Ruff 格式/检查、Mypy（399 文件）、中文注释和空白检查通过。日志
`/tmp/skill-retirement-server-quality-verified.log`。此结果先于随后新增的 checkpoint 退役实现，
后者需要自己的专项与完整门禁证据。

checkpoint 阶段专项结果：7 个 SQLite 用例通过，2 个独立连接竞争用例只在 PostgreSQL
运行。合并引用、时钟和退役测试，真实 PostgreSQL 17 上 **52 例通过**，数据库由 Alembic
初始化至 0041。覆盖整组历史依赖、真实截止、错误账户和混合身份、外层回滚、墓碑查询与
成员审计、共享树仍存在时的过期导出、重新 pin 后准备/预约拒绝、显式 reset 建立新 head、
精确增量基线保护、迟到目录指针回滚，以及 pin/退役两种顺序的真实锁等待。
日志 `/tmp/skill-checkpoint-retirement-tests.log`、`/tmp/skill-checkpoint-retirement-postgres-verified.log`、
`/tmp/skill-checkpoint-retirement-postgres-migration-verified.log`。临时 PostgreSQL 容器和匿名卷已删除。
最终完整 Server 门禁已单独通过，不能把此前比较退役的 1136 例结果当作本轮最终门禁。

下一步实现前的整理约束细化见 `skill-directory-compaction.md`：目录树与各当前单项的
实际 backing 树必须分别验证，未授权根级辅助数据保留；新近解除保护的等价旧视图仍需
遵守真实等待期。默认整理不能默认为 all-unreferenced，也不能提前宣称已释放配额。

补充本地候选依赖：未激活或已移除来源仍可能保留 number=1 的本地初始版本，该版本的
后续物化使用 AccountLocalSkill.source_checkpoint_id 作为确切 backing。只要这个初始版本
仍 retained，来源 checkpoint 不能先退役；它是历史内容依赖，不是新增活动保活根。
状态 prune 不顺带退役本地初始版本，后续原始版本回收需要独立实现其期限与范围契约。

首次 checkpoint 完整门禁通过 1142 passed、22 skipped、80.70% coverage；
`/tmp/skill-checkpoint-retirement-server-quality.log`。随后补入本地初始版本来源依赖回归，
最终包含该用例的完整门禁通过 **1143 passed、22 skipped、80.70% coverage**；Ruff、
Mypy（402 文件）、中文注释和空白检查全部通过。日志
`/tmp/skill-checkpoint-retirement-server-quality-verified.log`。PostgreSQL 52 例结果也包含这项补充。

## 内部目录整理后续实现

`skill-directory-compaction.md` 所述内部 preview/apply 已实现：按精确历史身份分析当前
目录和受保护 head 的 backing 树，原子创建等价视图并更新必要成员引用，保留原始快照与
增量基线。旧历史不自动退役，释放时钟只在保护真正解除时启动。完整 Server 门禁
1160 passed、24 skipped、80.90% coverage，PostgreSQL 67 例通过；具体证据见整理文档。
整理后的只读保护投影现已实现，保留原始快照和精确基线并预计真实等待期。公开 prune、
原请求恢复、历史/对象引用释放、额度结算和持久化 GC 仍须按 `skill-state-prune.md` 继续实现。


## 完整历史依赖计划与执行

`SkillHistoryRetirementPlanner.preview` 从明确账户内的初始历史选择反向展开全部保留
消费者，返回 `HistoryRetirementPlan`。条目包含精确身份、原内容摘要、保留状态、保护与
截止信息、全部阻断及将过期的分支；依赖边还保留每个连带消费者的完整输入，供原计划
重验。其他输入仅展示，不会因正向依赖被自动选为删除目标。普通 parent 和回执不参与
内容消费者闭包。本地初始版本与假设新目录的引用是阻断项，永远不是可偷偷删除的目标。

`apply` 在用户锁和保存点内重建完整计划，变化后返回 HEAD_CHANGED；新 pin、新消费者、
新上传或保留资格变化不能继续使用旧确认。整份计划有任何阻断时拒绝执行。原内部退役
入口与新计划共用 `history_dependencies` 定义，所有校验先于修改。完整选择最多一百万
身份、四百万依赖边，超限整体失败，不再按旧 1000 条边界截断。迁移活动上传查询按
500 个 ID 分块读取完整结果，两个所有者条件和真实租约截止始终参与查询。

整理服务的 `retirement_preview` 返回保护投影及其完整退役计划。新增虚拟目录仍引用旧
成员时显示 replacement_reference；刚解除保护的旧等价视图仍等待普通/归档期限。
明确提前只越过等待，不能越过快照、精确增量基线、本地原始版本或活动上传。这个
只读组合本身不写任何历史。已验证的整理与退役可以在同一个 retention_mutation 保存点
执行，调用方捕获后提交和外层回滚都不能遗留半次状态。

没有新 schema、公共操作回执、配额结算或文件回收。计划 apply 的重试会重新检查内容
保留状态，不能当作公共幂等接口；公开 prune 必须先查持久化原请求回执，再触碰可能已
退役的内容。账户范围的完整候选、用户确认、批量范围策略及安全删除任务仍待接入。

本轮新增十三例（纯图三例、历史计划六例、整理组合四例），加上活动迁移上传与精确
基线的增强回归，专项组合十五例通过：`/tmp/skill-history-plan-final-tests.log`。
PostgreSQL 17 组合三十九例通过，含新增完整计划/pin 两种顺序的真锁竞争、1001 个额外
依赖视图的整批回滚/提交、活动上传跨 500 ID 查询块、错误所有者及固定租约截止：
`/tmp/skill-history-plan-postgres.log`。数据库由 Alembic 升级至 0041：
`/tmp/skill-history-plan-postgres-migration.log`。临时容器和匿名卷已清理。

本轮最终完整 Server 门禁：**1182 passed、28 skipped、81.14% coverage**，Ruff format/lint、
Mypy（421 文件）、中文文档串和 whitespace 均通过：
`/tmp/skill-history-plan-server-quality.log`。新增两个 PostgreSQL-only 跳过项已在上述三十九例
真实数据库测试中通过。没有遗留测试进程、临时数据库或卷；没有提交、发布或能力开启。

## 完整树时钟与实际内容外键库存

0042 为已完成树增加可空释放时钟，复用现有用户锁和保留事务记录真实交付/保护变化。
原 complete 重放不延长等待，旧未知时钟不补造，已经无保护的历史退役不重启树等待。
内部 `SkillRetentionInspector.trees` 另列所有实际历史外键，包括生成保留列和复合授权主键。
树普通期限届满不能覆盖归档历史的期限或 retained 外键。所有内容准入检查同用户所有分类
的 deleting 标记；原 begin/get 和操作回执元数据仍可恢复。

完整契约和本轮 PostgreSQL 68 例、0042 升降级保护证据见 `skill-tree-retention.md`。
此步骤不创建删除任务、不解除树/对象引用、不结算额度，也不开放公开 prune。

## 已接入内部实际内容回收

0043 的 `SkillContentReclamationService.preview/apply` 在同一用户保存点中重验精确树/对象、
解除无 retained 外键的树、保留活动上传计量、结算实际分类额度，并只为失去所有物理保留
理由的文件写入持久化删除任务。`SkillContentDeletionWorker` 独立事务消费已提交任务，
原任务 UUID、SQL 用户锁、卷内 flock 和完成回执共同阻止迟到执行影响新内容。
历史退役与本层已证明可在同一外层保存点一起回滚/提交，之后才运行实际磁盘删除。
具体状态、重试、升级保护及 PostgreSQL 76 例证据见 `skill-content-gc.md`。公开 prune 的
完整范围、账户整理和原请求回执仍未接入，不能把内部 apply 当作公共幂等受理。
