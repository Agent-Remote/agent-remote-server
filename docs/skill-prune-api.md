# 公开 Skill state prune：确认与不可变回执

## 协议与授权

新增接口均沿用活跃用户 token 和 SKILL_MANAGER_ENABLED；owner 只来自认证上下文。
`POST /skills/state/prune/preview` 接受 selector、all_unreferenced、可选 cursor 和 limit（1–100）。
首请求只读解析规范稳定来源与当前 cutoff，返回 summary、连续 offset 的披露 rows、total、
next_cursor 和 confirmation。后续请求必须回显规范 selector 与模式。每页重建同一 cutoff
的完整计划并比对摘要；事实变化拒绝旧分页，不展示混合计划。所有预览都不写 ORM、
操作、归属、上传或回执。最后一页且内容计划可执行时才返回 confirmation。

每个 history 行明确原始身份/种类、是否仍可恢复、是否选中、完整组、直接/传播阻断、
保护理由和等待截止；全部候选历史和完整依赖边均可遍历。整理披露包含所有变化目录、
等价 head 以及移除/阻断成员，不以摘要替代恢复损失列表。每页最多 100 个有界记录；
客户端必须验证 offset/total/摘要一致并完整遍历后再确认，不能只显示第一页。

计划摘要使用版本化、类型标记、长度定界的 SHA-256 流式编码，覆盖 PrunePlan 全部字段，
包括完整清单、scope/cutoff、头/纪元、完整图、资格、分组、对象边/租约/额度和 claim UUID。
字典和集合按规范编码排序；顺序集合保留顺序；datetime 规范为 UTC 微秒；未知类型拒绝。
不使用 Python 默认 JSON/repr 或把整份大计划塞入客户端日志。此摘要为 Server 协议身份，
CLI 不重建内部图。修改编码需要升级域版本。

游标与最终确认是 HMAC-SHA256 签名的、严格有界的元数据包，使用独立协议域和部署
secret_key。包绑定用户、规范 selector、cutoff、模式、完整计划摘要、offset 及 page/confirm
阶段。游标签名不是用户认证；不能跨用户、跨阶段或修改 offset/范围。最后页才签发
confirm。凭据不独立授予其他账户权限。凭据不基于等待超时自动执行，旧计划事实变化
始终拒绝。密钥轮换使尚未受理的旧凭据失效；已提交回执不依赖旧签名或输入仍存在。

`POST /skills/state/prune` 只接受 idempotency_key 和 confirmation，正文远低于 CLI 4 MiB
原请求日志上限。先锁定已有用户存储、查原键并比较精确请求摘要；命中立即返回原回执，
不解析旧凭据、不读计划、不访问已经回收的输入或文件。新请求才验证签名与用户、重建
原计划、比较摘要与完整披露数量，随后原子执行。签名本身不绕过任何真实保护与 CAS。

## 0045 持久化

`skill_prune_operations`：UUID 主键、user/account、用户唯一原键、精确请求 SHA-256、
计划摘要、不可变 receipt JSON 和创建/更新时间。user/account 外键绑定真实账户目录；
(user_id,id) 唯一供子记录约束。receipt 保存原规范 summary、原键、confirmation 的散列、
实际退役/整理数量、分类释放与原待物理删除字节、披露条数；status 永远是 accepted。
数据库不存储原签名确认凭据。回执和元数据不是内容保活根。

`skill_prune_operation_entries`：(operation_id,ordinal) 主键、user_id、完整披露行 JSON；
(user_id,operation_id) 外键绑定原操作。保存原披露顺序与全部损失/阻断/整理信息；实际
等价替换行补上真实结果 UUID。没有指向已退役内容的外键，读取不需要重新构造旧计划。

`skill_prune_operation_deletions`：(operation_id,deletion_id) 主键及 user_id，分别以
(user_id,operation_id) 和 (user_id,deletion_id) 外键绑定原操作和其真实删除任务。
0045 在既有删除任务上补充 (user_id,id) 唯一约束，不改写旧任务或进度。
不对回执/明细/任务关系设置内容级联删除。降级在任何操作历史存在时先拒绝，再删除表
和辅助唯一约束；升级只增加空回执表，不推造以前的原请求。

原操作、全部明细、任务关联、head、历史退役、claims 和额度变化共享同一用户锁与外层
retention_mutation 保存点。明细可分块 flush，但不能分批提交。故障或调用方捕获异常后
外层提交都不能留下半份回执或清理。独立 worker 仍只消费最终提交任务。

## 恢复与进度

`GET /skills/state/prune/operations?key=...` 与 `/operations/{id}` 仅按认证用户查询原回执，
返回同一个 accepted 内容。`/operations/{id}/entries?offset=...&limit=...` 分页返回原持久化
披露，不访问旧内容。`/operations/{id}/progress` 单独返回关联任务的 pending/complete
数量、待删除/已删除字节和等待重试数量；不改写原 accepted 回执，也不声称受理即磁盘释放。

CLI 接入必须复用按 Server/用户绑定的原请求日志、一次确认和原键/ID 恢复。断线或 Ctrl-C
后先查原受理，未知结果时不得重新预览后生成新键。新增 Server 路由是加法接口；CLI
命令与 status 分派接入及其实际验收仍须随后完成，不能把仅 Server 通过称为完整交付。
