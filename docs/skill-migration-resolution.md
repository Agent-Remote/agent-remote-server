# 迁移专用人工内容与解决计划

本域使用 `skill_branch_preparations` 的真实身份，与会话收尾解决计划分开。
完整解决服务最终必须在用户内容锁和单一保存点中完成计划、目录/分支 CAS、成功迁移
序号和不可变回执；输入查询与导出契约见 `skill-migration-conflicts.md`。

## 持久化基础

迁移 `0036_skill_migration_resolution` 给准备记录增加 `(user_id, account_id, id)` 唯一键，
并增加五张表，不改写任何既有迁移原始响应：

- `skill_migration_resolution_uploads`：上传租约通过实际外键绑定迁移、用户、账户、树和范围，
  不能仅依赖调用方可能自行构造的键前缀。未完成和过期上传均保留绑定，不能跨迁移替用。
- `skill_migration_resolution_content`：在明确迁移下完成验证的人工状态树授权和保活引用。
  主键 `(migration_id, tree_digest)`，复合外键保证相同用户/账户的迁移、同用户 state 树。
  相同用户的其他迁移、包、普通上传或会话解决内容不能仅凭摘要冒充这份授权。
- `skill_migration_resolution_plans`：一个迁移一份单调版本计划，`revision >= 0`。
  复合用户/账户/迁移外键防止跨账户关联。读取没有计划时返回版本零，不建立记录。
- `skill_migration_resolution_choices`：每个选择范围一行，范围摘要为主键的一部分。
  普通路径、完整关联单元或整体范围只可择一；方法为 current/incoming/file/directory。
  自定义树通过本迁移 content 复合外键保活。文件必须带路径，目录不能带路径。
  选择转换使用相同严格 schema，不借用最终发布身份。
- `skill_migration_resolution_operations`：用户幂等键唯一，保存请求摘要、迁移、计划版本
  与不可变响应。原始回执引用不能因后续计划修改而变为新的选择或重复执行。

仓储计划替换使用版本 CAS，不一致返回失败，完整编排层需映射为 `PLAN_REVISION_CONFLICT`。更新计划与替换选择
处于同一保存点，晚到失败不得留下已递增版本或丢失旧选择。上层在此之外仍须先取得
用户写锁、校验范围不重叠、实际内容、原始冲突与全部实时前置条件。数据库外键不是
这些业务检查的替代品。降级前检查全部五表；存在人工内容、计划或回执时拒绝降级，
不能先删表再报告无法降级。

## 人工上传接口

用户接口前缀 `/api/v1/skills/state/migration/conflicts/{id}`：

- `POST /uploads`：新受理要求原冲突仍为 conflicted；有界接收 `idempotency_key` 和完整
  `manifest`，使用独立 `migration-resolve:{id}:upload:{key-hash}` 命名空间。已经真实绑定的相同键先恢复原租约，即使冲突已失效；清单改变仍拒绝。
- `GET /uploads/{upload_id}`：只查询原迁移内同用户上传；先授权，再处理租约状态。
- `PUT /uploads/{upload_id}/files/{digest}`：只接收租约声明文件，校验大小、摘要和类型。
- `POST /uploads/{upload_id}/complete`：完整字节验证及内容授权引用在同一保存点提交。
  重复完成同树或不同键上传同树只保留一份迁移授权。已失效冲突的已有上传允许恢复和
  完成以保留用户编辑，但不允许建立新的上传，也不会恢复旧计划的发布资格。
- `GET /plan`：原始迁移的当前计划版本和选择；未创建时返回零与空选择。只读。

Node/device 凭据与其他用户不可使用这些接口。已有同用户内容不能绕过迁移范围；完成
人工上传不改变冲突状态、分支、目录、有效版本账本或最后成功迁移基线。

## 完整解决编排要求与公开接入边界

原子命令、用户入口与过期重算已接入。当前已支持非重叠逐路径选择、
完整不透明/关联单元、dry-run 零写入、完整覆盖预览、实际内容与额度验证、原子发布、
新成功序号和不可变幂等重放。目标或目录变化要求建立新的比较尝试，不转移旧选择；
reset/restore/重装不能被旧输入复活。来源同纪元的新 head 不替换保存 incoming；成功解决
旧输入只推进到保存来源 checkpoint，更新来源仍待下一次显式迁移。

关联原始树包含的其他根不自动获得写入授权。完整目录结果需要逐个核对稳定来源、
base revision、分支纪元和 head，特别是当前目录反向链接与旧来源内的其他 skill。
不能裁掉关联根假装完整，也不能把旧树其他来源的版本身份搬入当前目录。预览必须
展示相对目标原始包及目标当前状态的覆盖，结果仍为明确 base revision 的 modified 状态。

## 本增量验证与边界

用户接口及持久化测试覆盖上传并发/重放、伪造键前缀、同用户跨迁移内容、非用户凭据、
文件损坏、过期租约、reset 后原键恢复、授权失败保存点回滚、计划版本 CAS、旧选择保护、
复合归属/范围约束及原始操作回执不随计划改变。原子 resolve 用户入口现已接入，见下文；上传和计划查询本身仍不能作为迁移完成证明。

PostgreSQL 17 已验证 0036 升级、降到 0035、再升级，29 条既有准备/迁移记录和全部
原始响应保持相同。新增五表及准备表的列/可空性、主键、外键列映射、唯一列映射及检查
约束名称与 ORM 一致。含数据降级在变更前拒绝，保留 26 个上传绑定、10 个人工内容授权、
7 个计划、3 个选择、1 条操作回执和 74 条准备/迁移记录，head 保持 0036。
日志：`/tmp/skill-migration-resolution-pg-migration.log`、
`/tmp/skill-migration-resolution-pg-existing.log`、`/tmp/skill-migration-resolution-pg-tests.log`。

完整 Server 门禁通过：713 passed、15 skipped、覆盖率 77.39%；Ruff 格式/静态检查、Mypy、
中文文档字符串/字段描述与空白检查全部通过。日志：
`/tmp/skill-migration-resolution-quality-complete.log`。本增量未提交、发布、部署或宣告运行能力。

## 迁移候选结果计算

迁移专用纯计算器使用保存的 base/current/incoming 和另行保存的账户目录上下文，
不把任何历史树额外包含的根当成写入授权。关联单元由四份输入共同计算，因此仅存在于
当前目录的反向链接也不能被忽略。普通独立文本冲突复用路径级显式选择；不透明状态或
跨根关联必须选择完整单元，不能用文件级选择拆开数据库或链接组。

不带路径的选择指向目标所在完整关联单元，不默认替换账户内所有独立 skill。保留 current
时，目标根来自保存的目标当前侧，其他关联根来自当时账户目录；保留 incoming 时，整个
关联单元采用保存来源侧。所有不属于选择范围的根始终取账户目录，忽略历史导出树的独立
上下文。人工目录允许携带未修改的范围外上下文以使链接完整，但不能通过这些额外根
修改独立来源。新选择导致链接悬空或组合无效时，只返回完整冲突，不返回部分清单。

纯计算器返回的有效清单仅为候选；调用服务仍须验证全部涉及稳定来源、版本、纪元、head、
实际字节和额度，生成相对目标原始包及当前状态的覆盖预览，并通过完整目录事务发布。
算法成功不能代替关联来源授权，也不能提前推进成功迁移基线。

只读候选规划器按原迁移身份加载四份保存输入、本迁移已完成的人工树和目标原始包。
它在计算前拒绝已失效或实时前置条件变化的尝试，验证输入及结果实际字节和完整目录额度，
并分别给出相对目标当前侧、目标原始包和账户目录的差异，以及其他受影响根。目标结果
标记明确 revision 与是否 modified。范围与分项额度使用原目录真实成员；额外入口不能仅凭
路径获得稳定来源身份或独立额度，后续发布仍须检查来源及新增入口。
此规划器不保存计划、回执、上传或 checkpoint；返回候选不代替后续来源授权及发布编排。

## 内部计划编辑事务

`MigrationResolutionDraftService` 是不接入公开 resolve 路由的独立内部编辑事务。
请求沿用严格的单次选择、预期版本、幂等键和 dry-run；编辑从原迁移已保存选择出发，
移除与新选择范围相交的旧选择，保留独立选择，再完整验证剩余计划。被替换的人工
内容无需再次读取，但保留下来的人工内容仍须校验本迁移授权和真实字节。

预览只取得既有用户读锁，返回当前计划版本和完整候选/覆盖说明，不改变任何数据。
保存取得用户写锁，CAS 替换全部选择，递增版本，并在同一保存点保存不可变回执。
回执标记 `migration_resolution_draft` 类型，状态仅为 `planned`，不表示 published；
`candidate_complete` 只说明全部内容冲突已处理，不代替关联来源身份和发布权限检查。
完整候选仍不创建上传、checkpoint，不移动目录/分支 head，也不更新成功序号。

同用户原键重放先于活跃状态、版本及实时漂移检查；即使计划后来修改或 reset 失效，
重放仍返回原响应。不同请求或不同迁移不得复用该键。失败的版本 CAS 或晚到回执
保存错误回滚计划及其选择，即使外层继续提交。独立回执查询同时展示当前迁移状态，
避免把历史 planned 响应解释为当前有效计划。过期比较当前仍拒绝，完整 resolve 编排
必须另行重算，且不能转移这些旧选择。此内部能力没有新增公开路由。

### 关联发布的证据与剩余边界

迁移 `0037_skill_checkpoint_provenance` 已为新 item checkpoint 保存原始 state epoch
和确切 backing-directory ID，完整目录保存创建纪元；见 `skill-checkpoint-provenance.md`。
旧历史的缺失证据保持未知。完整源树中额外同名 skill 根仍不能仅凭内容摘要、目录名
或当前 state ID 推断历史来源，同一个 state ID 可跨 reset 保留。

单分支 `SkillBranchPublisher.publish` 保留其他成员引用，明确拒绝隐藏关联写入。
`publish_many` 接受精确关联分支集合，内部完整解决器在同一用户锁和保存点内推进
所有受影响分支、完整目录、计划及最终回执。上述内部 draft 回执仍不是最终发布回执；
过期重算和公开入口见下文及 `skill-migration-recomputation.md`。

### 完整候选的关联来源校验

完整候选规划现进一步检查将写入的其他稳定成员。每个成员必须来自保存目录的
真实 member 引用，历史 item.state_epoch 已知且等于当前分支 epoch，来源仍有效，
将修改的分支 head 仍等于保存成员 checkpoint。人工目录以这些保存身份作为目标，
不能仅凭新建 SKILL.md 获得新的稳定来源。

整体 incoming 还必须沿来源 item 的确切 backing-directory 检查所有关联稳定成员，
包括字节恰好未变化的成员。来源 item 必须对应保存 source checkpoint、state 和 epoch；
关联成员的 state ID（含固定原始版本/来源）及历史 epoch 必须匹配保存目录成员。
缺失 provenance 明确失败；同名、同摘要或同 state ID 的跨 reset 内容都不能冒充匹配。
即使相关字节不变，整体 incoming 也不能导入已移除或已跨安装纪元的来源。
这允许同纪元的历史关联 checkpoint 合入较新的同源 head，但不允许偷偷切换关联版本。
来源 backing 的目录创建纪元用于历史说明，不能当作今天的新显式迁移请求的目录纪元；
原计划是否过期仍依据其受理时保存的目录/分支前置条件判断。

内部完整解决器在此校验之后于写锁内重查，并对所有相关 head 做原子 CAS。

## 内部原子解决命令

`SkillMigrationResolutionService` 在同一用户写锁与保存点中完成选择替换、计划版本 CAS、
完整关联发布、迁移成功序号和类型为 `migration_resolution` 的不可变回执。
不完整计划仅返回 pending；完整候选同时发布目标和实际修改的其他稳定成员。
未修改成员保持原 checkpoint，删除成员保留同分支 item 视图但移出新目录成员集合。
根级辅助内容随完整目录一起发布；不允许只推进目标 head 而保留错误关联成员引用。

预览沿用真实内容、关联来源、额度和原始包验证，并说明每个将写入分支的精确版本、
原 head、state epoch、相对自身当前侧以及原始包的全部变化。预览不写入任何对象。
原子成功只把保存的 source checkpoint 记为迁移基线，来源之后同纪元新增的 head
仍待下一次显式迁移。原 preparation.response_json 始终保持首次受理结果；当前 ready、
结果检查点和成功序号单独更新。回执重放早于实时前置条件校验，不能重复执行旧选择。

最终目录、任一目标分支、迁移记录状态或回执写入失败，都回滚本次新内容引用、
所有 head、计划和成功基线。发布后冲突详情的实时比较以已发布结果为自身预期，
不能把自己的发布误报为 stale。完整命令在应用选择之前拦截过期比较，从保留输入创建替代尝试或终止旧计划；
详细纪元优先级见 `skill-migration-recomputation.md`。

initial 与 older 准备同样可能因替换目录成员造成悬空链接而冲突。完整人工目录可显式
修复这些冲突；发布保留原模式，成功序号为空，不建立增量迁移基线。存在来源的 older
仍检查来源纪元；无来源的 initial 不虚构来源 checkpoint，整体 incoming 涉及其他
稳定成员但没有来源身份时明确拒绝。ready 诊断仅对有成功序号的记录比较自身迁移基线。

## 用户原子解决与操作回执入口

`POST /api/v1/skills/state/migration/conflicts/{migration_id}/resolve` 接受严格的
`SkillResolutionRequest`，通过用户令牌、功能开关和精确迁移归属后调用完整原子服务。
`GET /api/v1/skills/state/migration/resolution-operations?key=...` 读取类型为
`migration_resolution` 的原始回执及迁移当前状态；draft、其他操作或用户的键不能替用。
resolve 封套 status 为 preview、pending 或 published，只有实际保存的结果 committed=true；
操作查询 committed=true 描述历史受理，不表示该迁移今天仍活跃。外层提交失败不返回成功。

人工文件/目录通过既有迁移范围上传建立内容授权，再以摘要提交明确选择。每次选择必须
提供最新计划版本；预览不保存选择或回执。查询计划可恢复下一次选择所需版本。原键重放
保留原响应，包含先前 pending 回执，即使后续已经发布或 reset。非用户凭据、其他用户、
跨迁移人工摘要、错误版本和不同请求复用键均明确失败。

过期比较在原计划版本核对后返回 preview 或 superseded，不应用本次选择。新比较从原始
保留输入与今天的 current/目录建立，关联冲突仍要求完整明确选择；无冲突结果可自动发布。
旧计划不搬到新尝试。源/目标与实际改动关联成员的纪元失效不当作普通竞争重试；
原样保留的关联项可以在新比较中沿用今天目录，细节见重算协议。库变更和成功迁移会在
各自保存点主动取消精确范围的旧冲突。replacement_id 指向新比较或取代旧输入的成功迁移，没有可继续
尝试时为空；stale_reasons 和 recomputation_possible 明确说明本次处理。原始已成功
且未被取代的记录仍拒绝新的 resolve，不能重复发布。

当前原子解决与用户 API 验证：全量 Server 门禁 822 passed、15 skipped、覆盖率 78.18%；
Ruff、Mypy（327 个源文件）、中文文档字符串/字段描述和空白检查全部通过。PostgreSQL 17
通过 108 项服务回归与 11 项真实 HTTP 测试；此次没有 schema 变更，head 保持 0037。
临时数据库容器和匿名卷均已移除。日志：`/tmp/skill-atomic-resolution-api-quality-final.log`、
`/tmp/skill-atomic-resolution-pg-final.log`、`/tmp/skill-migration-resolution-api-pg.log`。

## 上传前纯清单预览

`POST /api/v1/skills/state/migration/conflicts/{migration_id}/content-preview` 接受
`expected_revision`、人工文件/目录 `choice` 与摘要一致的完整 `manifest`，不接收文件字节，
不建立上传、人工内容授权、计划或回执。原冲突授权在有界读取正文前执行；读取完毕后重新核对
计划版本及固定四侧的失效条件。已有选择仍使用原迁移的内容授权，新选择仅用于本次纯算法。

预览的 `candidate_complete` 只表示清单层面完整；`metadata_only=true`、
`content_verified=false`、`ready_to_publish=false` 明确尚不能发布。返回完整候选摘要、
相对保存账户目录的 `changes`、相对目标自身当前侧的 `target_changes`，以及相对目标原始包
的 `original_changes`、确切 `target_revision_id` 与 `target_modified`。候选不完整时这些
差异和摘要为空，保留 `remaining`。普通文件、关联单元、结构及独立上下文的规则复用正式解决算法。

真实 resolve 仍要求完成本迁移范围上传，并重新验证全部内容、来源身份、额度和 head。
清单预览不授予任何可持久化内容引用，不能通过附带 manifest 字段绕过正式命令。正文限制
64 MiB，声明树受已有完整目录限制；实际用户额度和新来源识别仍属于上传及发布检查。

CLI `skill state resolve ID` uses the migration-specific plan, metadata preview, scoped upload,
verified resolve and receipt endpoints. Its preview preserves exact target revision and every
current/original/directory override; a modified candidate cannot be reported as an untouched new
version. Local custom files are fixed before confirmation. A verified candidate must agree with the
reviewed custom metadata before an actual request can enter the durable CLI journal. Recovery queries
the original key before touching a changed or missing local source; only explicit key absence permits
identical replay. Pending plans and superseded attempts retain distinct exit-2 results.

GET `/api/v1/skills/state/migration/resolution-operations/{operation_id}` returns the same
`SkillMigrationResolutionReceipt` as original-key lookup. The repository filters by owner and operation
ID, the service preserves immutable original result JSON, and live supersession diagnostics remain
separate. An internal draft operation still fails `OPERATION_KIND_MISMATCH`; ID lookup never promotes
it into a full resolution receipt, creates a new plan or executes a choice.
