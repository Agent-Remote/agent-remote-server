# 首次账户目录接管事务

设计第 7 节要求原目录保留、旧写入者排空、失败重试不重复导入，以及单一目录权威提交点。
Native Node 已接通清点、静止证明、稳定捕获与传输消费；普通 session 启动可预约原节点接管，
详见 `skill-session-admission.md`。混合后端验收和可验证回滚仍待完成，现有 Node 不广告受管能力。

迁移 `0039_skill_account_takeover` 增加一张接管收据表，固定 user/account/node/backend、
用户幂等键与请求摘要、目录 epoch、精确 capture task、不可变旧资源清单及其摘要。
每账户只有一次权威接管；阶段为 reserved、uploading、committed。当前没有释放围栏或将模式
退回 legacy 的通用接口。显式可验证回滚将另行接入 Helper 原目录清单，不能通过删除收据实现。

预约持用户内容锁并使用保存点：验证账户归属、当前 legacy epoch/head 及节点完整新鲜能力后，
将目录置为 migrating，同时保存操作和 Node 任务。原有会话不被停止。清单保留历史绑定、
会话、技能导入和后端迁移身份；取消、失败、过期任务仍在其中。会话表和历史任务共同清点，
不依赖 profile 中最后一次绑定 ID。清单不含配置、文件清单、宿主路径或凭据。

Node 捕获输入必须绑定精确有效租约、Server 清单摘要、目录 epoch 和 Helper 持久收据身份。
Server 独立检查没有活动会话及未终结的旧技能写入任务，但这些状态不是进程静止证明。
Helper 仍须在本地关闭围栏、检查所有已知及本地额外资源、核实整个进程组和导入收据静止，
保留稳定原目录后才提交捕获声明。其他节点遗留资源需要先明确处置，不能忽略。

捕获阶段固定完整清单摘要和 Helper 收据身份，使用独立 account_directory 上传配额。租约重试
只可续传同一输入，不能换树；上传身份通过同用户/摘要/范围复合外键绑定当前接管。
每个文件和完成请求重新核对精确 Node 任务；摘要相同不授予其他账户上传权限。

完成时持同一用户锁和保存点，验证全部实际字节、系统路径排除、单技能与根级总额度，
保存初始完整目录 checkpoint，再为有效手工技能创建账户本地身份、初始版本、运行分支和
成员视图。根级辅助数据、链接与尚非有效技能的目录仍保存在完整树中。同名用户库来源不会
覆盖手工来源，后续精确会话准备仍明确报告来源冲突。所有引用和 managed_v1/head 交换、
committed 回执在同一保存点内；异常即使被调用方捕获并提交，也不能留下半次接管。

已提交重试返回原始收据，不重新导入、不回退后续 head/epoch。元数据与原内容需共同保留，
只要存在接管收据就拒绝 schema 降级。库操作的后台部署调度、混合后端 Node 执行和回滚仍是
独立集成门槛，不能用内部事务测试代替真实 Native/Docker Sandbox 验收。

## 验证边界

`test_skill_takeover*.py` 的 72 个用例已在 SQLite 外键开启和 PostgreSQL 17 上通过。
覆盖独立连接并发预约/上传绑定/提交、预约重放、旧资源清点、原写入者自然结束、预约后新增写入者拒绝、精确任务及
活动用户授权、实际布尔协议类型、不可变捕获、过期上传替换、实际格式及根级额度，
以及最后目录 CAS 失败后外层捕获异常并提交仍不保留部分引用。混合树用例保留
二进制状态、根级文件、空目录和跨入口链接；同名用户库安装不替代手工来源。

数据库测试绕过服务验证阶段组合、Node/任务绑定、用户/上传/摘要/范围及账户/
checkpoint 复合约束，以及账户、幂等键、任务唯一性。任务的用户身份仍由精确 payload
授权检查负责，数据库没有为 NodeTask 虚构用户列约束。

独立 PostgreSQL 实例已从头升级到 0039，再两次降级到 0038 并重升；已有 legacy
账户的 mode、epoch 和 head 均保持原值。有预约收据时降级被拒绝，迁移版本、目录、
任务、收据及全部 13 个约束保留。该证据不包含 Helper 实际排空或 Native/Docker
Sandbox 的真实工具接管；这些仍须接入并单独验收。

## Node 专用传输入口

功能开关开启后，现有 Node 令牌可使用 `/api/v1/node/skill-takeovers/{takeover_id}`。
所有请求必须提供精确数据库 `task_id` 查询参数，Node、用户、账户、后端和纪元均从原预约派生。
这些路由不能创建预约。Native Helper/Worker 已接通既有预约任务，普通 session 启动现在负责首次预约；Node 路由本身不接受预约写入。

| 方法及后缀 | 行为 |
| --- | --- |
| `GET` | 取得固定身份、最多 10,000 项旧写入者清单及当前原始收据；允许旧资源尚忙时先取得围栏依据 |
| `POST /capture` | 先核对任务，再有界读取最多 64 MiB 捕获声明；服务再次授权并检查排空后绑定上传 |
| `PUT /files/{digest}?upload_id=...` | 接收清单内文件的原始字节；检查完整大小、摘要和文本分类，接收结束后再次授权 |
| `POST /complete?upload_id=...` | 全部内容和目录权威提交后返回原始检查点；缺失文件返回 `CONTENT_INCOMPLETE` |

响应使用版本 1 的 `SkillResult[SkillTakeoverView]`，不返回完整文件清单、文件字节、用户幂等键或
宿主路径。`reserved`、`uploading` 的 `committed=false`；只有 `committed` 携带原始检查点并确认
权威提交。原始捕获摘要、Helper 身份、当前上传 ID/次数可用于断线重试；GET 不续期上传。
已提交的原任务即使终态也可读取原收据，但每次仍检查活动所有者、Node 和精确任务正文。
文件写入不可复用已提交收据；没有通用围栏释放、源目录删除或清理授权。

HTTP 测试使用真实令牌认证与独立请求事务，覆盖四个入口各自的跨节点/任务/租约/用户撤销、
不可变正文、大小限制、文件校验与原始提交重放。流式测试在接收正文后推进时间，确认过期租约
在写入内容卷前被拒绝。额外回环网络联调使用实际 Go 客户端调用 Python HTTP 路由，保留混合树
并验证单文件和提交的幂等重放；此证据不包含 Helper 进程静止证明或真实工具后端验收。

任务轮询信封新增 `task_record_id` UUID，直接返回数据库主键。原有 `task_id` 仍为逻辑任务名，
继续用于启动、完成、失败和 Node ledger 重放；不能把它当作接管传输的查询 UUID。旧 Node 可以
忽略新字段，受管任务消费者必须验证该 UUID。HTTP 测试从真实轮询结果取得新字段后授权成功，
并验证逻辑任务名不能替代它。

Native takeover dispatch uses the exact polled task record and attempt. The worker renews the
lease through Helper capture and upload, and never caches transient errors as task failure.
Generic completion accepts only the exact six-field committed takeover result after the initial
checkpoint transaction has committed; generic failure cannot consume a takeover reservation.
Exact replay has no directory lifecycle side effects. The original Helper capture and Server
reservation provide durable recovery even when the worker restarts before reporting completion.
