"""
定义会话接口模型。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class SessionData(BaseModel):
    """
    工具运行 session 响应数据
    """

    id: UUID = Field(..., description="会话标识")
    tool_type: str = Field(..., description="工具类型")
    user_id: UUID = Field(..., description="用户 ID")
    tool_account_id: UUID = Field(..., description="工具账户 ID")
    workspace_id: UUID = Field(..., description="工作区标识")
    workspace_local_path: str | None = Field(default=None, description="工作区本地起始目录")
    workspace_remote_path: str | None = Field(default=None, description="工作区远端工作目录")
    node_id: UUID = Field(..., description="节点 ID")
    project_key: str = Field(..., description="项目键")
    status: str = Field(..., description="工具会话状态")
    tmux_session_name: str | None = Field(default=None, description="tmux 会话名称")
    container_id: str | None = Field(default=None, description="容器或 sandbox 标识")
    runtime_backend: str = Field(..., description="会话运行时")
    runtime_resource_id: str | None = Field(default=None, description="运行时资源标识")
    replaces_session_id: UUID | None = Field(default=None, description="被替代的会话标识")
    create_task_id: str | None = Field(default=None, description="创建任务 ID")
    stop_task_id: str | None = Field(default=None, description="停止任务 ID")
    skill_finalization_operation_id: UUID | None = Field(
        default=None, description="受管会话收尾操作标识，即原始快照标识"
    )
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class CreateSessionRequest(BaseModel):
    """
    创建工具运行 session 请求
    """

    tool_type: str = Field(..., description="工具类型")
    tool_account_id: UUID = Field(..., description="工具账户 ID")
    workspace_id: UUID = Field(..., description="工作区标识")
    project_key: str = Field(..., description="项目键")
    argv: list[str] = Field(default_factory=list, description="透传给工具 CLI 的参数")
    replaces_session_id: UUID | None = Field(default=None, description="被替代的中断会话标识")
    request_id: UUID | None = Field(default=None, description="调用方生成的幂等请求标识")
    schema_version: Literal[1] | None = Field(default=None, description="调用方请求协议版本")
    argv_digest: str | None = Field(default=None, description="命令参数摘要，不保存原始参数")
    intent_fingerprint: str | None = Field(
        default=None, description="完整创建意图摘要，用于重放冲突检测"
    )
    sync_target: "SessionCreateSyncTarget | None" = Field(
        default=None, description="调用方确认的同步目标"
    )
    sync_target_digest: str | None = Field(default=None, description="同步目标摘要")
    prepare_task_id: str | None = Field(default=None, description="准备任务身份")
    prepare_task_record_id: UUID | None = Field(default=None, description="准备任务记录身份")


class SessionResponse(BaseModel):
    """
    工具运行 session 响应
    """

    data: SessionData = Field(..., description="工具会话数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class SessionListData(BaseModel):
    """
    工具运行 session 列表数据
    """

    items: list[SessionData] = Field(default_factory=list, description="工具会话列表")
    next_cursor: str | None = Field(default=None, description="下一页游标")


class SessionListResponse(BaseModel):
    """
    工具运行 session 列表响应
    """

    data: SessionListData = Field(..., description="工具会话列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class SessionCreateSyncTarget(BaseModel):
    """
    创建请求提交前确认的同步目标身份。
    """

    id: UUID = Field(..., description="同步会话 ID")
    workspace_id: UUID = Field(..., description="同步工作区 ID")
    node_id: UUID | None = Field(default=None, description="同步节点 ID")
    local_path: str = Field(..., description="本地目录")
    remote_path: str = Field(..., description="远端目录")
    mutagen_session_id: str | None = Field(default=None, description="Mutagen 会话标识")
    target_digest: str | None = Field(default=None, description="同步目标摘要")


class SessionCreateRequestReceipt(BaseModel):
    """
    创建受理时冻结的无内容凭据。
    """

    schema_version: Literal[1] = Field(..., description="受理协议版本")
    request_id: UUID = Field(..., description="调用方原始请求标识")
    tool_type: str = Field(..., description="工具类型")
    tool_account_id: UUID = Field(..., description="工具账户标识")
    workspace_id: UUID = Field(..., description="工作区标识")
    project_key: str = Field(..., description="项目身份")
    argv_digest: str = Field(..., description="命令参数摘要")
    intent_fingerprint: str = Field(..., description="完整创建意图摘要")
    replacement_session_id: UUID | None = Field(default=None, description="替代会话标识")
    sync_target: SessionCreateSyncTarget | None = Field(default=None, description="同步目标")
    node_id: UUID | None = Field(default=None, description="服务端实际调度节点")
    session_id: UUID | None = Field(default=None, description="已创建会话标识")
    create_task_id: str | None = Field(default=None, description="创建任务标识")
    task_record_id: UUID | None = Field(default=None, description="创建任务记录标识")
    phase: str = Field(..., description="受理阶段")
    acceptance_code: str | None = Field(default=None, description="等待或拒绝错误码")


class SessionCreateRequestObservation(BaseModel):
    """
    原键当前只读执行观察，可随任务推进变化。
    """

    task_status: str | None = Field(default=None, description="创建任务状态")
    session_status: str | None = Field(default=None, description="会话当前状态")
    result_status: str | None = Field(default=None, description="节点结果状态")
    observed_at: datetime | None = Field(default=None, description="观察时间")
    session_deleted: bool = Field(default=False, description="会话是否已被删除")


class SessionCreateRequestData(BaseModel):
    """
    原键查询响应。
    """

    schema_version: Literal[1] = Field(..., description="受理协议版本")
    request_id: UUID = Field(..., description="调用方原始请求标识")
    tool_type: str = Field(..., description="工具类型")
    tool_account_id: UUID = Field(..., description="工具账户标识")
    workspace_id: UUID = Field(..., description="工作区标识")
    project_key: str = Field(..., description="项目身份")
    argv_digest: str = Field(..., description="命令参数摘要")
    replacement_session_id: UUID | None = Field(default=None, description="替代会话标识")
    sync_target_digest: str | None = Field(default=None, description="同步目标摘要")
    phase: str = Field(..., description="受理阶段")
    session_id: UUID | None = Field(default=None, description="会话标识")
    create_task_id: str | None = Field(default=None, description="创建任务标识")
    task_record_id: UUID | None = Field(default=None, description="创建任务记录标识")
    task_status: str | None = Field(default=None, description="创建任务状态")
    session_status: str | None = Field(default=None, description="会话状态")
    result_status: str | None = Field(default=None, description="节点结果状态")
    session_deleted: bool = Field(default=False, description="会话是否已删除")
    observed_at: datetime | None = Field(default=None, description="观察时间")
    error_code: str | None = Field(default=None, description="受理错误码")


class SessionCreateRequestResponse(BaseModel):
    """
    原键查询响应包装。
    """

    data: SessionCreateRequestData = Field(..., description="不可变受理凭据及当前观察")
    request_id: str | None = Field(default=None, description="HTTP 请求跟踪身份")


class StopSessionRequest(BaseModel):
    """
    工具会话停止调用方原键请求。
    """

    request_id: UUID | None = Field(default=None, description="调用方生成的停止请求标识")
    schema_version: Literal[1] | None = Field(default=None, description="停止请求协议版本")
    expected_session_id: UUID | None = Field(default=None, description="调用方确认的会话身份")
    expected_node_id: UUID | None = Field(default=None, description="调用方确认的节点身份")
    expected_runtime_backend: str | None = Field(
        default=None, max_length=64, description="调用方确认的运行时后端"
    )
    expected_stop_task_id: str | None = Field(
        default=None, max_length=256, description="调用方确认的已有停止任务"
    )
    reason: str = Field(
        default="user_requested", min_length=1, max_length=64, description="停止原因"
    )
    intent_fingerprint: str | None = Field(default=None, description="停止目标摘要")


class SessionStopRequestData(BaseModel):
    """
    停止原键不可变受理凭据及当前只读观察。
    """

    schema_version: Literal[1] = Field(..., description="受理协议版本")
    request_id: UUID = Field(..., description="调用方原始停止请求标识")
    session_id: UUID = Field(..., description="原始会话标识")
    node_id: UUID = Field(..., description="受理时实际节点标识")
    runtime_backend: str = Field(..., description="受理时运行时后端")
    runtime_resource_id: str | None = Field(default=None, description="受理时运行时资源标识")
    stop_task_id: str | None = Field(default=None, description="原始停止任务标识")
    stop_task_record_id: UUID | None = Field(default=None, description="原始停止任务记录标识")
    stop_task_status: str | None = Field(default=None, description="停止任务当前状态")
    session_status: str | None = Field(default=None, description="会话当前状态")
    skill_finalization_operation_id: UUID | None = Field(
        default=None, description="受管会话保存操作标识"
    )
    session_deleted: bool = Field(default=False, description="会话是否已经删除")
    phase: str = Field(..., description="停止受理阶段")
    error_code: str | None = Field(default=None, description="停止受理错误码")
    intent_fingerprint: str = Field(..., description="停止目标摘要")
    reason: str = Field(..., description="停止原因")
    result_status: str | None = Field(default=None, description="节点结果当前状态")
    process_stopped: bool | None = Field(default=None, description="是否已经取得停止收据")
    save_status: str | None = Field(default=None, description="受管会话保存进度")
    observed_at: datetime | None = Field(default=None, description="观察时间")


class SessionStopRequestResponse(BaseModel):
    """
    停止原键查询响应封装。
    """

    data: SessionStopRequestData = Field(..., description="不可变受理凭据及当前观察")
    request_id: str | None = Field(default=None, description="HTTP 请求跟踪身份")


class SessionDeleteSelection(BaseModel):
    """
    固定集合删除时用户确认的筛选摘要。
    """

    tool_type: str = Field(..., min_length=1, max_length=32, description="工具类型筛选")
    statuses: list[str] = Field(
        default_factory=lambda: ["failed", "interrupted", "stopped"],
        description="用户确认时的会话状态筛选",
    )
    project_key: str | None = Field(default=None, max_length=256, description="项目筛选摘要")


class DeleteSessionRequest(BaseModel):
    """
    工具会话固定集合删除调用方原键请求。
    """

    schema_version: Literal[1] = Field(..., description="删除请求协议版本")
    request_id: UUID = Field(..., description="调用方生成的原始删除请求标识")
    tool_type: str = Field(..., min_length=1, max_length=32, description="工具类型")
    session_ids: list[UUID] = Field(
        ..., min_length=1, max_length=256, description="用户确认的完整会话 ID 集合"
    )
    selection: SessionDeleteSelection = Field(..., description="用户确认时冻结的筛选摘要")
    selection_digest: str = Field(..., min_length=71, max_length=71, description="集合选择摘要")
    intent_fingerprint: str = Field(
        ..., min_length=71, max_length=71, description="完整删除意图摘要"
    )
    reason: str = Field(
        default="user_requested", min_length=1, max_length=64, description="删除原因"
    )


class SessionDeleteRequestData(BaseModel):
    """
    固定集合删除原键不可变回执及当前只读观察。
    """

    schema_version: Literal[1] = Field(..., description="删除请求协议版本")
    request_id: UUID = Field(..., description="调用方原始删除请求标识")
    tool_type: str = Field(..., description="工具类型")
    session_ids: list[UUID] = Field(..., description="用户确认的完整会话 ID 集合")
    selection: SessionDeleteSelection = Field(..., description="受理时冻结的筛选摘要")
    selection_digest: str = Field(..., description="集合选择摘要")
    intent_fingerprint: str = Field(..., description="完整删除意图摘要")
    reason: str = Field(..., description="删除原因")
    phase: str = Field(..., description="删除受理阶段")
    delete_task_id: str | None = Field(default=None, description="删除逻辑任务标识")
    delete_task_record_id: UUID | None = Field(default=None, description="删除受理记录标识")
    task_status: str | None = Field(default=None, description="删除任务状态")
    deleted_session_ids: list[UUID] = Field(default_factory=list, description="已删除会话 ID 集合")
    session_statuses: dict[str, str] = Field(
        default_factory=dict, description="受理时冻结的会话状态"
    )
    session_contexts: list[dict[str, object]] = Field(
        default_factory=list, description="受理时冻结的会话节点、后端和项目上下文"
    )
    session_deleted: bool = Field(default=False, description="固定集合是否已全部删除")
    collection_mismatch: bool = Field(default=False, description="当前集合是否与确认快照不一致")
    error_code: str | None = Field(default=None, description="删除错误代码")
    observed_at: datetime | None = Field(default=None, description="当前观察时间")


class SessionDeleteRequestResponse(BaseModel):
    """
    固定集合删除原键响应封装。
    """

    data: SessionDeleteRequestData = Field(..., description="不可变删除回执及当前观察")
    request_id: str | None = Field(default=None, description="HTTP 请求跟踪身份")


CreateSessionRequest.model_rebuild()
