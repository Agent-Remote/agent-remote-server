# 版本迁移冲突查看与原始内容导出

迁移冲突使用 `skill_branch_preparations` 的真实受理身份，不属于 finalization 冲突。
用户身份、账户、稳定安装和安装纪元均来自保存记录；同摘要不扩大授权范围。
查询持有既有内容读锁，不初始化用户库、分支、上传或计划，不执行迁移或自动重算。

## 接口

- `GET /api/v1/skills/state/migration/conflicts?account_id=…&skill=…`：skill 可省略，
  列出同账户未解决或 superseded 的记录；每页最多 200 条。游标必须属于同账户及
  所选稳定来源。停用或归档来源仍可按稳定身份查询。
- `GET /migration/conflicts/{id}`：原始模式、来源/目标版本和纪元、三侧、原目录上下文、
  原始冲突单元、当前 head/epoch、配置漂移和需要重新计算的原因。原始受理状态与
  当前记录状态分开。来源仅有新 checkpoint 不会改写保存的 incoming，也不冒充 reset。
- `GET /migration/conflicts/{id}/diff`：最多 500 路径的已保存三侧元数据差异。它解释原始
  输入，不代表已批准的迁移结果；不读取正文。游标绑定此记录和固定三侧摘要。
- `GET /migration/conflicts/{id}/trees/{side}`：side 为 base/current/incoming/directory。
  输出保存的精确完整树，不裁剪路径、不改写链接。返回目标根及额外根清单，明确原始
  比较树可能包含关联或独立的上下文成员；directory 是当时完整账户目录，不是 current。
- `GET /migration/conflicts/{id}/trees/{side}/files/{digest}`：只允许该侧声明的文件，
  发送前核验实际字节并释放请求事务。当前账户其他文件或同用户其他树均不能冒用此入口。

首次向前迁移标签为 old_original/new_original/old_published；增量迁移为
old_original 或 last_migrated / target_original 或 target_published / source_published。
每侧同时给出相关版本、可选 checkpoint 和精确树摘要。原始目录使用 account_directory
标签，单独给出其 checkpoint。删除表现为真实空树/缺失条目，不合成文件。

列表和详情保留未发布输入；原始目录或其他引用变更不会令导出切换到今天的内容。
缺失、过期或损坏内容明确失败，不能返回不完整文件流。用户态以外的 Node/device 凭据
不可读取这些接口。此域为迁移专用解决流程提供可审阅输入，尚不复用会话的解决计划。
