# 配置导入与技能目录所有权

账户目录模式不受功能开关回退影响。规划 `account import-config` 时持用户内容锁，
检查请求的 include 和所有 file 路径；managed_v1 或 migrating 请求中的 skills 路径
使整批请求返回 `SKILL_MANAGER_OWNS_PATH`（409），不保存 profile、任务或审计。
CLI 的 `--exclude-skills` 在打包前移除根路径。插件 skills 和项目历史不属于账户发现根。

Node 开始旧排队导入任务时，Server 在原 start 接口再次检查所有权。Node 写入前还调用
`GET /api/v1/node-api/tasks/{task_id}/config-import-authorization`，只接受当前节点的
有效 leased/running 导入任务、活动所有者、同一账户和精确任务身份。响应含 task_id、
node_id、user_id、account_id、directory_mode、directory_epoch，不包含文件字节。
Node 对响应身份及目录模式再次检查；无响应、过期租约、未知模式或旧 Server 都拒绝写入。
任务正文中的自报模式不是授权来源。Node 在任何文件写入前检查整批路径和内容编码。

部署先升级 Server，再升级 Node。该协议不会让旧 Node 获得受管能力，且不启用接管。
完整接管还必须持同一用户锁阻止新 skills 导入、排空已入队和正在执行的旧任务，并取得
Node 本地排他边界后才捕获稳定旧目录。单次授权响应不允许越过此排空过程；仅凭租约到期
或任务被取消不能证明旧写入者已退出。此处的双次检查不能代替接管流程和本地排他锁。

Node 底层写入原语在 Linux/macOS 上以配置根为锚，使用 no-follow 目录句柄逐级打开账户路径；
实际特权 Helper 导入仅在 Linux 上启用。
写入前预检全部已有祖先及目标，拒绝符号链接、特殊文件、重复目标和文件/目录冲突，
不能借 agents 等非技能路径的链接间接写入 skills。每次真正写入再次从锚点打开，
通过临时文件 fsync、原子 rename 和父目录 fsync 发布；硬链接目标以新文件替换。
其他平台明确拒绝该安全写入流程。此路径安全边界仍不代替接管时的旧写入者排空。

导入传输同时限制原始内容和编码列表：单文件 1 MiB、原始合计 8 MiB、按紧凑 JSON
计算的 files 列表最多 12 MiB（非 ASCII 和 HTML 敏感字符按转义计量）。这为包含授权
和身份封套的 16 MiB Node 轮询/Helper 导入消息留下明确余量，超出在派发前拒绝。
Node 导入写入由串行 Linux Helper 执行，并使用其私有账户屏障和精确任务收据；接管排空必须检查
这些本地收据，不能把 worker/HTTP 失败当作 Helper 已停止写入。完整接管仍未启用。
