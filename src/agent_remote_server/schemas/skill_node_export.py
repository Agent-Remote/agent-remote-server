"""
定义只读冻结导出的精确身份、短期连接授权和在线重验封套。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class NodeExportBinding(BaseModel):
    """
    只绑定原启动快照，不以当前账户配置重建历史身份。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot_id: UUID = Field(description="原始精确快照")
    session_id: UUID = Field(description="永久保留的原会话引用")
    user_id: UUID = Field(description="原始内容所有者")
    account_id: UUID = Field(description="原始账户")
    node_id: UUID = Field(description="唯一来源节点")
    task_id: UUID = Field(description="原始准备任务数据库身份")
    library_generation: int = Field(ge=0, strict=True, description="原库代数")
    directory_epoch: int = Field(ge=1, strict=True, description="原目录纪元")
    initial_tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$", description="原准备树摘要")


class NodeExportRequest(BaseModel):
    """
    登录用户选择已有设备身份，不提交来源节点或文件路径。
    """

    model_config = ConfigDict(extra="forbid")
    device_id: UUID = Field(description="本地已注册设备")
    ssh_key_id: UUID = Field(description="本地对应的活跃 SSH 公钥身份")


class NodeExportAuthorization(BaseModel):
    """
    连接授权不代表冻结数据存在，也不证明 SSH 密钥已同步完成。
    """

    model_config = ConfigDict(extra="forbid")
    binding: NodeExportBinding = Field(description="精确原始身份")
    device_id: UUID = Field(description="唯一允许的设备")
    ssh_key_id: UUID = Field(description="唯一允许的 SSH 公钥")
    grant: str = Field(
        min_length=1, max_length=4096, repr=False, description="仅内存保存的短期凭据"
    )
    expires_at: datetime = Field(description="固定授权失效时间")
    ssh_host: str = Field(min_length=1, max_length=255, description="来源节点 SSH 地址")
    ssh_port: int = Field(ge=1, le=65535, description="来源节点 SSH 端口")
    ssh_user: str = Field(min_length=1, max_length=64, description="受限 SSH 用户")
    authorization_task_id: str = Field(description="既有密钥同步逻辑任务身份")
    authorization_task_status: Literal["pending", "leased", "running", "succeeded"] = Field(
        description="仅密钥同步进度，不是导出完成状态"
    )


class NodeExportVerification(NodeExportRequest):
    """
    原节点回显授权，设备与公钥必须来自 SSH 强制命令身份。
    """

    grant: str = Field(min_length=1, max_length=4096, repr=False, description="原短期凭据")


class NodeExportPermission(BaseModel):
    """
    在线校验结果只允许读取该冻结身份，不承诺本地数据齐全。
    """

    model_config = ConfigDict(extra="forbid")
    binding: NodeExportBinding = Field(description="本次重验的原始身份")
    device_id: UUID = Field(description="已重验设备")
    ssh_key_id: UUID = Field(description="已重验 SSH 公钥")
    expires_at: datetime = Field(description="当前所验凭据的固定失效时间")
    recheck_seconds: int = Field(
        default=10, ge=1, le=10, strict=True, description="传输中最长重验间隔"
    )
    incoming_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", description="已知收尾摘要，未知时不推断"
    )
    unclean: bool | None = Field(
        default=None, strict=True, description="已知终止分类，未知时不推断"
    )


class NodeExportRenewal(BaseModel):
    """
    明确关联前一凭据与同一原始身份的新短期凭据，不改变已有读取权限。
    """

    model_config = ConfigDict(extra="forbid")
    previous_grant_digest: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="本次请求凭据的精确 SHA-256 摘要"
    )
    grant: str = Field(
        min_length=1, max_length=4096, repr=False, description="仅内存保存的下一段短期凭据"
    )
    permission: NodeExportPermission = Field(description="当前原始身份、收尾事实及新凭据期限")
