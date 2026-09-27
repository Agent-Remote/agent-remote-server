"""
固定配置受理时的完整账户来源选择，不将配置计划宣称为运行快照。
"""

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DeploymentSource(BaseModel):
    """
    所有来源共享的不可变内容选择。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: UUID = Field(description="稳定来源身份")
    revision_id: UUID = Field(description="受理时选定版本")
    content_digest: str = Field(pattern=r"^[a-f0-9]{64}$", description="原始完整内容摘要")
    name: str = Field(min_length=1, max_length=64, description="受理时目录名称")
    enabled: bool = Field(strict=True, description="受理时有效启用值")


class DeploymentLibrarySource(DeploymentSource):
    """
    包版本与安装纪元共同固定用户库来源。
    """

    origin: Literal["library"] = Field(default="library", description="用户库来源")
    installation_epoch: int = Field(strict=True, ge=1, description="原始安装纪元")


class DeploymentLocalSource(DeploymentSource):
    """
    本地版本固定账户独有初始内容，不伪造安装纪元。
    """

    origin: Literal["account_local"] = Field(default="account_local", description="账户本地来源")


type DeploymentSelection = Annotated[
    DeploymentLibrarySource | DeploymentLocalSource, Field(discriminator="origin")
]


class SkillDeploymentPlan(BaseModel):
    """
    独立于后来账户绑定和规则的完整配置投影。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = Field(default=1, description="配置计划格式版本")
    user_id: UUID = Field(description="不可变所属用户")
    operation_id: UUID = Field(description="原始配置操作身份")
    generation: int = Field(strict=True, ge=0, description="原操作提交后的配置代数")
    account_id: UUID = Field(description="原始目标账户身份")
    node_id: UUID | None = Field(description="原始绑定节点，未绑定为空")
    tool_type: str = Field(min_length=1, max_length=32, description="原始工具类型")
    runtime_backend: str | None = Field(max_length=32, description="原始固定运行后端")
    sources: tuple[DeploymentSelection, ...] = Field(description="全部有效来源，含停用来源")

    def digest(self) -> str:
        """
        排序来源后计算规范摘要，使数据库返回顺序不改变原计划身份。

        :return str: 完整计划的十六进制摘要
        """
        raw = self.model_dump(mode="json")
        raw["sources"] = [
            source.model_dump(mode="json")
            for source in sorted(self.sources, key=lambda item: (item.origin, str(item.source_id)))
        ]
        encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode()).hexdigest()
