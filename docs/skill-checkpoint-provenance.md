# Checkpoint 来源与纪元证据

迁移解决必须证明关联内容的来源、版本和原纪元，不能由相同树摘要、同名根或当前
分支纪元推断。迁移 `0037_skill_checkpoint_provenance` 扩展既有 `skill_checkpoints`：

- `state_epoch`：item 创建时对应的分支纪元，正整数；directory 必须为空。
- `directory_epoch`：directory 创建时的目录纪元，正整数；item 必须为空。
- `backing_directory_id`：item 实际共同创建或明确取作原始内容的完整目录身份。
  独立库原始包初始化不虚构目录引用；不引用后来恰好具有同摘要的目录。
- `backing_scope`：固定为 directory。复合外键绑定相同 user/account、目录范围、
  backing ID 和不可变 `content_digest`，禁止跨用户、账户或不同完整内容替用。
  外键引用审计摘要，不使用可随内容退役变空的 `tree_digest`。

旧记录新增列为空，不把缺失历史证据填成当前 epoch。独立内容仍可查询和导出；
后续关联发布验证若需要缺失证据则明确返回 `STATE_PROVENANCE_UNAVAILABLE`。
新创建路径保存真实证据：原始库初始化保存 branch epoch；本地原始状态视图保存
branch epoch 和本地来源明确登记的初始 source checkpoint；收尾原始输入保存
snapshot 中的 state/directory epoch；合并发布使用实际通过 CAS 的目标纪元；
reset/restore 保存推进后的纪元，并保持旧 checkpoint 的证据不变。

共同生成目录与 item 时先保存目录，再让全部新 item 指向它。删除的 item 仍保留
backing 引用，即使最终目录成员中不再出现该名称。未改动成员继续保留其原 item
和原 backing，不把新目录上下文覆写进旧 checkpoint。人工新增来源的临时原始目录
保存当前目录纪元；本地原始来源的 backing 引用使用明确来源 checkpoint ID。

此变更只增加证据，不自动授权任何迁移。原身份仍由 item.state_id 对应的稳定来源/
revision 确定；关联发布必须读确切 backing 成员并逐项核对历史和当前 epoch、head、
身份及实际子树。缺少成员也不能把同名目录升级为稳定来源。

升级保留所有既有记录及 JSON。尚无新证据时可降级；只要存在任意非空 provenance
字段就拒绝降级，避免无声删除未来发布校验依赖的证据。内容退役保留审计引用，
这类元数据引用不等于自动永久保活 backing 字节，后续 GC 必须分别计算内容根。
