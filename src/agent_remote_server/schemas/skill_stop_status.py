"""
定义独立于进程状态的受管会话保存进度。
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from agent_remote_server.schemas.skill_terminations import CaptureError

SaveStatus = Literal[
    "awaiting_node",
    "capture_pending",
    "local_durable",
    "upload_pending",
    "persisted",
    "persisted_unclean",
    "published",
    "conflicted",
    "detached",
    "superseded",
]


class SkillStopStatus(BaseModel):
    """
    使用原始快照身份查询最新权威状态，不把历史发布等同于当前内容保留。
    """

    operation_id: UUID = Field(..., description="原始快照兼收尾操作标识")
    session_id: UUID = Field(..., description="原始会话标识，删除后仍保留")
    account_id: UUID = Field(..., description="原始账户标识")
    process_status: str = Field(..., description="当前会话显示状态，删除后为 deleted")
    process_stopped: bool = Field(..., description="是否已收到原始运行时的精确终止凭据")
    capture_error: CaptureError | None = Field(
        default=None, description="尚未完成冻结的固定失败原因"
    )
    status: SaveStatus = Field(..., description="数据保存与发布状态")
    unclean: bool | None = Field(..., description="已确认快照的异常退出分类，未知时为空")
    finalization_id: UUID | None = Field(..., description="已开始上传的收尾记录标识")
    checkpoint_id: UUID | None = Field(..., description="完整保存的原始输入检查点")
    publication_id: UUID | None = Field(..., description="最新发布尝试标识")
    content_retained: bool = Field(..., description="服务器是否仍保留完整收尾内容")


class SkillStopStatusResponse(BaseModel):
    """
    沿用会话接口响应封装，不授予修改或内容读取权限。
    """

    data: SkillStopStatus = Field(..., description="保存进度")
    request_id: str | None = Field(default=None, description="请求标识")
