"""
声明原始节点释放冗余收尾内容所需的短期完整性核验结果。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SkillReclamationAuthorization(BaseModel):
    """
    仅证明远端原输入当前完整可用，节点仍须独立核验停止和本地引用。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = Field(default=1, description="节点回收核验协议版本")
    request_id: UUID = Field(description="本次读取随机挑战，旧响应不能用于新回收请求")
    node_id: UUID = Field(description="原始授权节点")
    user_id: UUID = Field(description="原始内容所有者")
    account_id: UUID = Field(description="原始账户身份")
    session_id: UUID = Field(description="不随会话行删除而改变的原始会话身份")
    snapshot_id: UUID = Field(description="原始预约快照")
    finalization_id: UUID = Field(description="完整收尾身份")
    checkpoint_id: UUID = Field(description="仍保留完整内容的原始输入检查点")
    tree_digest: str = Field(pattern=r"^[a-f0-9]{64}$", description="重新验证的完整输入树摘要")
    unclean: bool = Field(strict=True, description="原始不可变终止分类")
    publication_id: UUID = Field(description="当前已提交终态发布身份")
    publication_attempt: int = Field(
        strict=True, ge=1, le=2**63 - 1, description="当前发布尝试编号"
    )
    publication_status: Literal["published", "conflicted", "detached"] = Field(
        description="当前发布或完整保留的冲突归档结果"
    )
    verified_at: datetime = Field(description="完整内容核验结束的 UTC 时刻")
    expires_at: datetime = Field(description="本次新回收意图最迟可建立的 UTC 时刻")
