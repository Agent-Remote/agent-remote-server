# 会话固定集合删除原键

`POST /api/v1/sessions/delete-requests` 接受用户已经审阅的准确会话集合。请求必须带
`schema_version=1`、调用方生成的 `request_id`、`tool_type`、按完整 UUID 排序且不重复的
`session_ids`、`selection`、`selection_digest`、`intent_fingerprint` 和 `reason`。

`selection` 只允许 `stopped`、`interrupted`、`failed` 三种可删除状态，并可包含
`project_key`。服务端按用户、工具类型、状态和项目筛选重新读取候选集，在同一事务锁定全部
候选行；候选集与调用方提交的完整 ID 集合不相等时返回
`SESSION_DELETE_COLLECTION_MISMATCH`，不会扩大或缩小删除范围。会话行的用户、账户、工作区、
项目、节点、运行时后端和资源身份在行锁内冻结到回执上下文；用户身份由 owner-scoped
请求绑定，不重复写入跨端上下文。停止任务、Skill retention、
设备控制和端口转发清理均先通过现有生命周期约束。

成功回执保存在 `session_delete_requests`，与被删的 `sessions` 没有外键关系。回执包含完整
集合、筛选摘要、两个摘要、每个会话的状态和运行时上下文、删除结果及 ledger record ID；删除
之后仍可使用 `GET /api/v1/sessions/delete-requests/{request_id}` 查询。相同用户和原键只能重放
相同意图，异意图固定返回 `SESSION_DELETE_REQUEST_CONFLICT`。其他用户和未知原键统一返回
不存在错误。旧的 `DELETE /api/v1/sessions` 保留用于兼容旧客户端；新 GUI/CLI 必须使用上述
固定集合合同，避免动态重新选择当前用户会话。
