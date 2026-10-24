"""
定义精确 Claude 会话附件传输的严格节点契约。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SessionAttachmentAdmissionRequest(BaseModel):
    """
    由已认证 Node 网关提交的完整附件租约身份。
    """

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = Field(default=1, description="附件传输协议版本")
    node_id: UUID = Field(..., description="认证节点标识")
    session_id: UUID = Field(..., description="精确 Claude 会话标识")
    device_id: UUID = Field(..., description="强制命令实际设备标识")
    ssh_key_id: UUID = Field(..., description="强制命令实际 SSH 密钥标识")
    lease_id: UUID = Field(..., description="Node 生成的单用途新租约标识")


class SessionAttachmentAdmissionData(BaseModel):
    """
    返回已验证身份和有界运行时范围，不包含账户路径。
    """

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = Field(default=1, description="附件传输协议版本")
    operation: Literal["transfer_lease"] = Field(
        default="transfer_lease", description="响应操作类型"
    )
    node_id: UUID = Field(..., description="已验证节点标识")
    session_id: UUID = Field(..., description="已验证会话标识")
    device_id: UUID = Field(..., description="已验证设备标识")
    ssh_key_id: UUID = Field(..., description="已验证 SSH 密钥标识")
    lease_id: UUID = Field(..., description="已签发租约标识")
    runtime_backend: Literal["native", "docker_sandbox"] = Field(..., description="运行时后端")
    runtime_resource_id: str | None = Field(..., description="运行时资源标识")
    runtime_scope: str = Field(..., min_length=1, max_length=64, description="不含路径的运行范围")
    expires_at: datetime = Field(..., description="租约绝对过期时间")
    max_bytes: int = Field(..., gt=0, le=268435456, description="租约字节上限")
    max_entries: int = Field(..., ge=1, le=32, description="租约批次上限")


class SessionAttachmentAdmissionResponse(BaseModel):
    """
    包装附件租约响应和请求追踪标识。
    """

    model_config = ConfigDict(extra="forbid")
    data: SessionAttachmentAdmissionData = Field(..., description="附件租约")
    request_id: str | None = Field(default=None, description="请求追踪标识")


class SessionAttachmentCleanupRequest(BaseModel):
    """
    由 Node 返回的严格公开清理回执。
    """

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = Field(default=1, description="附件传输协议版本")
    operation: Literal["cleanup_receipt"] = Field(
        default="cleanup_receipt", description="回执操作类型"
    )
    node_id: UUID = Field(..., description="认证节点标识")
    session_id: UUID = Field(..., description="原会话标识")
    device_id: UUID = Field(..., description="原设备标识")
    ssh_key_id: UUID = Field(..., description="原 SSH 密钥标识")
    lease_id: UUID = Field(..., description="原租约标识")
    status: Literal["cleaned", "pending", "unknown", "rejected"] = Field(
        ..., description="节点清理状态"
    )
    removed_batches: int = Field(..., ge=0, le=32, description="删除的批次数")
    receipt_id: UUID = Field(..., description="Node 生成的公开回执标识")


class SessionAttachmentCleanupResponse(BaseModel):
    """
    返回持久化后的清理回执。
    """

    model_config = ConfigDict(extra="forbid")
    data: SessionAttachmentCleanupRequest = Field(..., description="清理回执")
    request_id: str | None = Field(default=None, description="请求追踪标识")
