"""
区分当前账户选择、原会话快照与系统版本目录，均不宣称模型加载。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from agent_remote_server.schemas.skill_rules import ResolvedSkillRule


class SystemSkillView(BaseModel):
    """
    系统项只读且独立于用户库，条件选择不冒充已安装。
    """

    name: Literal["ego-browser", "agent-remote-device"] = Field(description="系统保留目录名称")
    origin: Literal["system"] = Field(default="system", description="独立系统来源")
    read_only: Literal[True] = Field(default=True, description="不能通过用户库命令修改")
    selected: bool | None = Field(description="配置选择或会话原选择，运行条件未知时为空")
    selection_reason: str = Field(description="选择依据或待启动时确定的条件")
    release: dict[str, str | int] = Field(max_length=8, description="配置或原快照中的版本引用")


class AccountSkillView(BaseModel):
    """
    当前选择的精确分支与账户同步证据，不触发初始化或迁移。
    """

    account_id: UUID = Field(description="明确查询账户")
    revision_selection_reason: Literal[
        "account_pin", "tool_pin", "user_default", "account_local_revision"
    ] = Field(description="当前版本固定来源或默认跟随原因")
    directory_mode: Literal["legacy", "migrating", "managed_v1"] = Field(description="当前管理模式")
    directory_epoch: int | None = Field(description="当前目录纪元")
    directory_checkpoint_id: UUID | None = Field(description="当前完整目录头")
    state_id: UUID | None = Field(description="当前选中版本的分支身份")
    state_epoch: int | None = Field(description="当前分支纪元")
    checkpoint_id: UUID | None = Field(description="当前选中分支的头，不替用其他版本")
    state_expired: bool = Field(description="选中分支是否已过期")
    preparation: Literal["initialized", "uninitialized", "migration_required", "state_expired"] = (
        Field(description="已存状态准备情况，不表示部署就绪")
    )
    publication_conflicts: int = Field(ge=0, description="该来源各版本纪元的未解决发布冲突数")
    migration_conflicts: int = Field(ge=0, description="该来源各版本纪元的未解决迁移冲突数")
    latest_publication_conflict_id: UUID | None = Field(description="最新发布冲突查询身份")
    latest_migration_conflict_id: UUID | None = Field(description="最新迁移冲突查询身份")
    last_recorded_sync_at: datetime | None = Field(description="已有证据的最近完整内容同步时间")
    unknown_sync_times: bool = Field(description="是否还有已持久化但缺失完成时间的旧记录")


class SessionSkillItemView(BaseModel):
    """
    固定会话成员只使用原快照和不可变分支来源，不读取当前规则。
    """

    name: str = Field(description="原快照目录名称")
    skill_id: UUID = Field(description="原稳定来源身份")
    origin: Literal["user_library", "account_local"] = Field(description="原来源种类")
    revision_id: UUID = Field(description="原始固定版本")
    installation_epoch: int = Field(ge=1, description="原始安装纪元")
    state_id: UUID = Field(description="原始精确运行分支")
    state_epoch: int = Field(ge=1, description="启动固定的分支纪元")
    checkpoint_id: UUID = Field(description="启动固定检查点")
    checkpoint_retained: bool = Field(description="该原检查点内容当前是否仍保留")
    resolution: ResolvedSkillRule = Field(description="原快照保存的逐字段解析结果")


class SessionSkillView(BaseModel):
    """
    有界查询原会话选择；旧会话没有记录时明确未知，不从当前配置重建。
    """

    session_id: UUID = Field(description="原始会话身份，删除后仍按快照所有者授权")
    account_id: UUID = Field(description="原账户身份")
    basis: Literal["session_snapshot", "legacy_unrecorded"] = Field(description="选择证据来源")
    snapshot_id: UUID | None = Field(description="原精确快照身份")
    snapshot_status: str | None = Field(description="快照生命周期，不等于模型已加载")
    content_retained: bool | None = Field(description="快照完整内容是否仍保留，旧会话未知")
    runtime_backend: Literal["native", "docker_sandbox"] = Field(description="固定后端")
    library_generation: int | None = Field(description="原快照固定的配置代数")
    directory_epoch: int | None = Field(description="原快照固定的目录纪元")
    starting_checkpoint_id: UUID | None = Field(description="原起始完整目录头")
    tree_digest: str | None = Field(description="原物化树摘要，退役后仍保留身份")
    system_items: list[SystemSkillView] = Field(description="原系统引用，不取当前发布配置")
    items: list[SessionSkillItemView] = Field(description="当前页原快照成员")
    next_cursor: str | None = Field(description="同一快照下一页名称游标")
    project_discovery: Literal["not_inspected"] = Field(
        default="not_inspected", description="不推断项目原生发现"
    )
    model_loaded: Literal[False] = Field(default=False, description="本查询不证明模型已加载")
