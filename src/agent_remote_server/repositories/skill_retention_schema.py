"""
显式记录各引用表的退役角色，新增跨域外键不能静默绕过保活分析。
"""

from sqlalchemy import MetaData

REFERENCE_POLICIES = {
    "skill_deployment_discoveries": "原受理发现边界及接管身份，待处理操作保活初始目录",
    "skill_deployment_discovered_sources": "原接管首次本地版本的规范引用，终态元数据不永久保活",
    "skill_deployment_terminations": "永久撤权元数据，待排空输入由原非终态任务保活",
    "skill_deployment_tasks": "活动或可重试部署的完整目录输入，终态绑定不是永久内容根",
    "skill_prune_operations": "不可变原受理，不保活已退役内容",
    "skill_prune_operation_entries": "原完整披露元数据，没有原内容外键",
    "skill_prune_operation_deletions": "原删除进度关联，不授予新的内容权限",
    "skill_prune_content_claims": "随实际内容行失效的派生续扫资格，不是保护或保留历史引用",
    "skill_content_deletions": "持久化物理删除屏障，终态任务身份不是内容根",
    "skill_libraries": "用户配置代数，不是内容根",
    "skill_installations": "活动默认版本与当前安装纪元",
    "skill_installation_epochs": "归档时钟元数据，不是永久根",
    "skill_revisions": "原始包内容与墓碑",
    "skill_tool_overrides": "工具 pin",
    "skill_account_overrides": "账户 pin",
    "skill_activations": "激活审计，不追溯保活历史包",
    "skill_source_observations": "来源审计，不保活完整历史",
    "skill_operations": "精确待处理版本，终态回执不是根",
    "skill_deployment_targets": "原始账户配置计划，待处理或可重试操作保活精确版本",
    "skill_deployment_entries": "计划的复合归属版本引用，终态元数据不永久保活内容",
    "skill_deployment_attempts": "完整目标尝试链，当前投影须与原操作保活阶段一致",
    "skill_deployment_retries": "不可变重试受理身份，不独立保活内容",
    "account_local_skills": "当前本地来源及 reset 初始目录",
    "account_local_skill_revisions": "本地初始内容与墓碑",
    "account_skill_directory_states": "当前目录内容和独立物化成员义务",
    "account_skill_states": "当前或 pin 分支，历史 head 不是根",
    "skill_checkpoints": "保护内容及 backing，parent 仅为审计",
    "skill_directory_members": "活动上下文成员或当前目录整理义务",
    "session_skill_snapshots": "活动会话和待收尾完整基线",
    "session_skill_snapshot_items": "精确暴露的原始分支与 checkpoint",
    "skill_finalizations": "未上传或未发布输入，以及冲突间接引用",
    "skill_finalization_transfers": "上传身份，租约与 finalization 已覆盖内容",
    "skill_snapshot_terminations": "原始停止观察，无内容外键；待保存输入由 finalizing 快照保活",
    "skill_publications": "未解决完整发布比较",
    "skill_publication_branches": "比较所观察的全部分支",
    "skill_resolution_plans": "计划身份，内容来自 choices",
    "skill_resolution_choices": "原冲突保活的人工树",
    "skill_resolution_operations": "不可变回执，不作为永久内容根",
    "skill_effective_branches": "有效使用历史，不能永久保活旧分支",
    "skill_branch_preparations": "未解决三侧与精确最新成功增量基线",
    "skill_migration_resolution_uploads": "冲突上传绑定，上传租约覆盖暂存对象",
    "skill_migration_resolution_content": "未解决迁移保活的全部授权人工内容",
    "skill_migration_resolution_plans": "计划身份，授权内容另行保活",
    "skill_migration_resolution_choices": "所有人工树均有完整授权内容外键",
    "skill_migration_resolution_operations": "原始回执，不作为永久内容根",
    "skill_state_operations": "原始 reset/restore 回执，不追溯永久保活",
    "skill_storage_usage": "引用写入与回收的同用户串行锁",
    "skill_content_objects": "分类计量，对象保护来自树或活动租约",
    "skill_stored_trees": "完整树，保护由引用闭包决定",
    "skill_content_uploads": "仅有效暂存租约是硬根",
    "skill_upload_objects": "原始上传声明的派生索引，租约已覆盖内容，不独立保活",
    "skill_tree_object_references": "分类对象依赖及共享底层文件",
    "skill_account_takeovers": "待完成接管，提交后由目录和本地来源接替",
}
_PREFIXES = ("skill_", "account_skill_", "account_local_skill", "session_skill_")


def require_classified_schema(metadata: MetaData) -> None:
    """
    新领域表或从其他领域指向 skill 内容的外键必须先明确其退役角色。

    :param metadata (MetaData): 本进程使用的完整 ORM 元数据
    """
    for table in metadata.tables.values():
        participates = table.name.startswith(_PREFIXES) or any(
            key.target_fullname.split(".")[-2].startswith(_PREFIXES) for key in table.foreign_keys
        )
        if participates and table.name not in REFERENCE_POLICIES:
            raise ValueError("unclassified skill retention reference table: " + table.name)


TREE_CONTENT_COLUMNS = frozenset(
    {
        ("skill_revisions", "tree_digest"),
        ("account_local_skill_revisions", "tree_digest"),
        ("skill_checkpoints", "tree_digest"),
        ("session_skill_snapshots", "retained_tree_digest"),
        ("skill_finalizations", "retained_tree_digest"),
        ("skill_publications", "retained_current_tree_digest"),
        ("skill_branch_preparations", "retained_base_digest"),
        ("skill_branch_preparations", "retained_current_digest"),
        ("skill_branch_preparations", "retained_incoming_digest"),
        ("skill_resolution_choices", "retained_tree_digest"),
        ("skill_migration_resolution_content", "retained_tree_digest"),
    }
)


def require_classified_tree_references(metadata: MetaData) -> None:
    """
    核对全部实际树入向外键，新增或变更引用不能绕过删除前的完整历史库存。

    :param metadata (MetaData): 本进程 ORM 的完整元数据
    """
    actual = {
        (table.name, key.parent.name)
        for table in metadata.tables.values()
        for key in table.foreign_keys
        if key.column.table.name == "skill_stored_trees" and key.column.name == "digest"
    }
    expected = TREE_CONTENT_COLUMNS | {
        ("skill_tree_object_references", "tree_digest"),
        ("skill_prune_content_claims", "tree_digest"),
    }
    if actual != expected:
        raise ValueError("unclassified skill tree content reference")
