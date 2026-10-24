# 工具会话创建调用方原键合同

Server 为 CLI 和 desktop 提供按用户隔离的会话创建幂等受理与只读观察。

## API 和身份

创建请求携带规范非零 UUID `request_id`、schema 版本、工具/账户/workspace/project 身份、replacement、sync target 摘要和 argv/intent digest。原始 argv 仍只存在执行任务已有载荷；caller-key 记录、回执和观察不保存 raw 参数、环境或凭据。

Server 通过 `(user_id, request_id)` 唯一约束保存 `session_create_requests`。同一用户和 key 的意图完全一致时返回原受理结果；任何冻结字段不同都返回固定冲突。跨用户、未知 key、上下文漂移和缺少能力均拒绝或返回安全的 pending/unknown。

## 事务和观察

受理事务重新校验用户、账户、workspace、replacement、sync target、Node/backend 和准备证据，再写入 Session、NodeTask、审计和 caller-key 回执。Node 后续只更新可变观察，不能改写原始受理身份，也不能从旧回执派生新的任务或权限。

`GET /api/v1/sessions/requests/{request_id}` 是 owner-scoped 只读观察入口。它返回 request phase、session/task 公开 ID、当前状态、错误码和删除观察；不会刷新设备 last-seen、续租、重派任务或泄露其他用户数据。会话删除后原回执仍可查询。

## 当前状态

创建原键、冲突、重放、丢响应恢复、replacement、生命周期结果保护和 migration 0065 已由 Server 测试覆盖；完整质量门禁通过。正式发布仍需一次性真实 Server/Node/SSH/tmux/fclaude 联调，验证 Node 执行、exact attach、附件传输和清理回执，并绑定准确组件版本。