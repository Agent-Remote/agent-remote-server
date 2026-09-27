"""
定义技能管理的稳定结果、版本详情和可解释规则视图。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_remote_server.schemas.skill_diagnostics import SkillHistoryDiagnostic, SkillStorageView
from agent_remote_server.schemas.skill_effective import AccountSkillView, SystemSkillView
from agent_remote_server.schemas.skill_library import SkillProvenance, SkillSource
from agent_remote_server.schemas.skill_rules import ResolvedSkillRule, SkillRuleOverride


class SkillRevisionView(BaseModel):
    """
    清晰区分库内版本编号、内容身份和上游来源。
    """

    id: UUID = Field(description="稳定版本标识")
    number: int = Field(description="用户库内容版本编号")
    content_digest: str = Field(description="不可变原始树摘要")
    provenance: SkillProvenance = Field(description="首次登记的不可变来源观测")
    retained: bool = Field(description="原始内容是否仍可恢复")
    metadata: dict[str, object] = Field(description="工具格式元数据")
    retention: SkillHistoryDiagnostic | None = Field(
        default=None, description="详情查询的精确保留诊断"
    )


class SkillAccountRuleView(SkillRuleOverride):
    """
    显示账户覆盖所属工具，不根据调用方输入推断账户身份。
    """

    tool_type: str = Field(description="账户真实工具类型")


class SkillInstallationView(BaseModel):
    """
    用户库完整详情，账户有效字段可选且不宣称模型实际加载。
    """

    id: UUID = Field(description="稳定技能标识")
    name: str = Field(description="当前目录名称")
    epoch: int = Field(description="当前安装纪元")
    removed: bool = Field(description="是否已卸载归档")
    source: SkillSource = Field(description="稳定来源身份")
    tracking: dict[str, object] = Field(description="用户默认上游跟踪策略")
    default_enabled: bool = Field(description="用户默认启用")
    default_revision_id: UUID = Field(description="用户默认版本")
    revisions: list[SkillRevisionView] = Field(description="保留版本及过期墓碑")
    tool_overrides: dict[str, SkillRuleOverride] = Field(description="工具逐字段覆盖")
    account_overrides: dict[str, SkillAccountRuleView] = Field(description="账户逐字段覆盖")
    effective: ResolvedSkillRule | None = Field(
        default=None, description="指定账户或工具的规则解析"
    )
    project_discovery: Literal["not_inspected"] = Field(
        default="not_inspected", description="项目原生发现尚未检查"
    )
    model_loaded: Literal[False] = Field(default=False, description="此视图不证明模型已经加载")
    account_state: AccountSkillView | None = Field(
        default=None, description="账户查询的精确状态与同步说明"
    )
    storage: SkillStorageView | None = Field(
        default=None, description="详情查询的当前用户总存储观察"
    )


class SkillLocalRevisionView(BaseModel):
    """
    本地初始状态版本及内容保留信息。
    """

    id: UUID = Field(description="稳定版本标识")
    number: int = Field(description="本地版本编号")
    content_digest: str = Field(description="原始内容摘要")
    retained: bool = Field(description="原始内容是否保留")
    subtree_prefix: str = Field(description="完整目录内的来源路径")
    metadata: dict[str, object] = Field(description="工具格式元数据")
    retention: SkillHistoryDiagnostic | None = Field(
        default=None, description="详情查询的精确保留诊断"
    )


class SkillLocalView(BaseModel):
    """
    仅在明确账户范围展示的本地来源，不伪造库或上游身份。
    """

    origin: Literal["account_local"] = Field(default="account_local", description="账户本地来源")
    id: UUID = Field(description="稳定本地技能标识")
    account_id: UUID = Field(description="唯一所属账户")
    name: str = Field(description="当前目录名称")
    status: Literal["active", "removed"] = Field(description="来源发布状态")
    enabled: bool = Field(description="账户本地启用标志")
    default_revision_id: UUID = Field(description="本地初始状态版本")
    source_checkpoint_id: UUID = Field(description="首次完整目录检查点")
    revisions: list[SkillLocalRevisionView] = Field(description="本地版本及过期墓碑")
    effective: ResolvedSkillRule = Field(description="该账户的有效启用规则")
    model_loaded: Literal[False] = Field(default=False, description="此视图不证明模型已经加载")
    account_state: AccountSkillView | None = Field(
        default=None, description="账户查询的精确状态与同步说明"
    )
    storage: SkillStorageView | None = Field(
        default=None, description="详情查询的当前用户总存储观察"
    )


class SkillLibraryView(BaseModel):
    """
    当前用户库及可用于下一次修改的代数。
    """

    system_items: list[SystemSkillView] = Field(
        default_factory=list, description="独立系统目录或有效系统选择"
    )
    generation: int = Field(description="配置代数")
    items: list[SkillInstallationView] = Field(description="当前用户条目")
    local_items: list[SkillLocalView] = Field(
        default_factory=list, description="明确账户范围的本地条目"
    )


class SkillOperationTarget(BaseModel):
    """
    每个受理目标的存储或部署就绪状态。
    """

    account_id: UUID = Field(description="受影响账户")
    node_id: UUID | None = Field(description="受理时的绑定节点")
    readiness: Literal[
        "stored", "pending", "unsupported", "ready", "needs_resolution", "failed"
    ] = Field(description="目标就绪状态")
    deploy_on_first_use: bool = Field(description="是否等待首次使用时部署")
    error_code: str | None = Field(default=None, description="稳定目标错误码")
    plan_digest: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", description="原始配置计划摘要，历史操作可为空"
    )
    attempt_id: UUID | None = Field(default=None, description="当前持久化目标尝试身份")
    attempt_number: int | None = Field(default=None, ge=1, description="当前目标尝试序号")
    retryable: bool = Field(default=False, description="此目标当前失败是否允许原计划重试")


class SkillMutationData(BaseModel):
    """
    配置命令的持久化受理结果。
    """

    generation: int = Field(description="提交后的用户库代数")
    skill_ids: list[UUID] = Field(description="本次全部选定技能")
    revision_ids: list[UUID] = Field(description="本次选定或登记版本")
    changed: bool = Field(description="是否改变配置或版本集合")
    warnings: list[str] = Field(description="保留覆盖和归档状态说明")
    targets: list[SkillOperationTarget] = Field(description="各账户的独立就绪状态")
    replacement_id: UUID | None = Field(default=None, description="取代本操作的后续操作")


class SkillErrorView(BaseModel):
    """
    不携带私有文件内容的稳定结构化错误。
    """

    code: str = Field(description="稳定错误码")
    message: str = Field(description="用户可读错误说明")
    object_id: str | None = Field(default=None, description="相关对象或目标标识")
    details: dict[str, object] = Field(default_factory=dict, description="已授权对象的具体差异")


class SkillResult[PayloadT](BaseModel):
    """
    供 CLI 和 API 共用的版本化状态封套。
    """

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = Field(default=1, description="结果协议版本")
    operation_id: UUID | None = Field(default=None, description="持久化受理操作标识")
    status: str = Field(description="操作状态，不替代内容持久化或运行发布状态")
    committed: bool = Field(description="配置是否已持久化提交")
    retryable: bool = Field(default=False, description="是否可重试既定计划")
    data: PayloadT = Field(description="本次结果数据")
    errors: list[SkillErrorView] = Field(default_factory=list, description="全部已知失败目标")
