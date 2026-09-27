"""
定义账户状态历史、明确原始基线和完整依赖导出的只读响应。
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from agent_remote_server.schemas.skill_diagnostics import SkillHistoryDiagnostic, SkillStorageView
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest


class SkillCheckpointView(BaseModel):
    """
    检查点自身的保留状态与来源会话收尾状态分开表达。
    """

    id: UUID = Field(description="不可变检查点身份")
    account_id: UUID = Field(description="所属账户")
    scope: Literal["item", "account-directory"] = Field(description="明确对象范围")
    state_id: UUID | None = Field(description="单项运行分支身份")
    skill_id: UUID | None = Field(description="稳定库或账户本地来源身份")
    origin: Literal["user_library", "account_local"] | None = Field(description="来源种类")
    revision_id: UUID | None = Field(description="该分支固定原始版本")
    installation_epoch: int | None = Field(description="该分支固定安装纪元")
    state_epoch: int | None = Field(description="单项创建时的分支纪元，旧记录未知时为空")
    directory_epoch: int | None = Field(description="目录创建时的纪元，旧记录未知时为空")
    backing_directory_id: UUID | None = Field(
        description="单项确切完整目录来源，未知或独立初始化时为空"
    )
    current_state_epoch: int | None = Field(description="查询时分支纪元，不冒充历史检查点纪元")
    current_directory_epoch: int | None = Field(description="查询时账户目录纪元")
    subtree_prefix: str = Field(description="单项在完整保存树中的前缀")
    parent_id: UUID | None = Field(description="历史父检查点")
    content_digest: str = Field(description="原始完整保存树摘要，退役后仍保留")
    retained: bool = Field(description="内容是否仍被完整保留")
    is_head: bool = Field(description="是否仍为该目录或精确分支的当前头")
    invalid_skill_format: bool = Field(description="是否包含已知无效技能格式")
    source_session_reference_id: UUID | None = Field(description="产生该记录的原始会话身份")
    finalization_id: UUID | None = Field(description="来源会话的独立收尾身份")
    finalization_status: str | None = Field(description="来源会话收尾结果，不等价于当前头")
    storage_location: Literal["server", "expired"] = Field(description="完整内容持久化位置")
    created_at: datetime = Field(description="检查点创建时间")
    retention: SkillHistoryDiagnostic | None = Field(
        default=None, description="详情查询的精确保留诊断"
    )
    storage: SkillStorageView | None = Field(
        default=None, description="详情查询的当前用户总存储观察"
    )

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        """
        数据库无时区时间按约定解释为 UTC。

        :param value (datetime): 保存时间
        :return datetime: 带明确时区的时间
        """
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SkillCheckpointPage(BaseModel):
    """
    同一账户与来源的所有版本纪元历史使用独立有界页。
    """

    items: list[SkillCheckpointView] = Field(description="检查点历史")
    next_cursor: UUID | None = Field(description="同范围下一页游标")


class SkillCheckpointMember(BaseModel):
    """
    完整目录保存的成员引用固定到精确来源、安装纪元和原始版本。
    """

    entry_name: str = Field(description="完整目录中的成员名称")
    state_id: UUID = Field(description="精确运行分支身份")
    checkpoint_id: UUID = Field(description="该目录保留的成员检查点")
    skill_id: UUID = Field(description="稳定来源身份")
    origin: Literal["user_library", "account_local"] = Field(description="来源种类")
    revision_id: UUID = Field(description="固定原始版本身份")
    installation_epoch: int = Field(description="固定安装纪元")
    state_epoch: int | None = Field(description="该成员检查点创建时的纪元，旧记录未知时为空")


class SkillCheckpointMemberPage(BaseModel):
    """
    大目录的完整成员引用独立分页，不能用当前配置替换历史成员。
    """

    checkpoint_id: UUID = Field(description="所属完整目录检查点")
    items: list[SkillCheckpointMember] = Field(description="原始固定成员")
    next_cursor: str | None = Field(description="下一页成员名称游标")


class SkillStatePendingView(BaseModel):
    """
    待上传收尾只返回身份和状态，不能作为已保存快照导出。
    """

    id: UUID = Field(description="收尾身份")
    snapshot_id: UUID = Field(description="原始精确会话快照")
    session_reference_id: UUID = Field(description="原始会话身份")
    node_id: UUID = Field(description="唯一持有待上传内容的来源节点")
    incoming_digest: str = Field(description="待完整上传目录摘要")
    status: Literal["upload_pending"] = Field(default="upload_pending", description="尚待上传")
    storage_location: Literal["source_node"] = Field(
        default="source_node", description="来源节点持有"
    )
    exportable_from_server: Literal[False] = Field(default=False, description="服务端尚无完整内容")


class SkillStatePendingPage(BaseModel):
    """
    待上传记录独立分页，不能伪造一个空检查点。
    """

    items: list[SkillStatePendingView] = Field(description="待上传收尾")
    next_cursor: UUID | None = Field(description="同范围下一页游标")


class SkillCheckpointTree(BaseModel):
    """
    导出保留原路径的完整依赖单元，可独立校验而不会生成悬空链接。
    """

    checkpoint_id: UUID = Field(description="授权检查点身份")
    source_tree_digest: str = Field(description="服务端原始完整树摘要")
    tree_digest: str = Field(description="本次完整导出清单摘要")
    subtree_prefix: str = Field(description="用户选中项的原始前缀，目录范围为空")
    dependency_roots: tuple[str, ...] = Field(description="为保持相对链接而一并导出的其他顶层范围")
    locally_removed: bool = Field(description="完整记录确认选中技能已被会话删除")
    manifest: SkillTreeManifest = Field(description="保留原相对路径的完整可验证清单")


class SkillStatePathDiff(BaseModel):
    """
    差异只返回清单元数据，二进制和大文件正文通过导出检查。
    """

    path: str = Field(description="相对账户发现根的原始路径，单项保留其前缀")
    base: SkillTreeEntry | None = Field(description="原始基线元数据或缺失")
    current: SkillTreeEntry | None = Field(description="选中检查点元数据或缺失")


class SkillCheckpointDiff(BaseModel):
    """
    明确单项原始版本或目录父检查点基线，不把某次会话快照替换为基线。
    """

    checkpoint_id: UUID = Field(description="正在比较的检查点")
    base_kind: Literal["package_revision", "local_initial_revision", "directory_checkpoint"] = (
        Field(description="明确基线来源")
    )
    base_reference_id: UUID = Field(description="基线版本或检查点身份")
    base_tree_digest: str = Field(description="基线完整存储树摘要")
    current_tree_digest: str = Field(description="当前完整存储树摘要")
    items: list[SkillStatePathDiff] = Field(description="有界路径差异")
    next_cursor: str | None = Field(description="下一页相对路径游标")
