"""
定义节点持久化模型。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON as JsonType
from sqlalchemy import DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from agent_remote_server.db import Base
from agent_remote_server.models.mixins import IdMixin, TimestampMixin, _utc_now


class Node(IdMixin, TimestampMixin, Base):
    """
    VPS 执行节点
    """

    __tablename__ = "nodes"
    __table_args__ = (
        Index("nodes_registration_token_hash_uidx", "registration_token_hash", unique=True),
        Index("nodes_node_token_hash_uidx", "node_token_hash", unique=True),
        Index("nodes_status_heartbeat_idx", "status", "last_heartbeat_at"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    region_code: Mapped[str] = mapped_column(String(32), nullable=False)
    tags: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    wireguard_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wireguard_public_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    wireguard_endpoint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssh_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssh_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ssh_user: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supported_tool_types: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    allowed_runtime_backends: Mapped[list[str]] = mapped_column(
        JsonType, nullable=False, default=lambda: ["docker_sandbox"]
    )
    default_runtime_backend: Mapped[str] = mapped_column(
        String(32), nullable=False, default="docker_sandbox"
    )
    runtime_policy: Mapped[dict[str, object]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    runtime_capabilities: Mapped[dict[str, object]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    # 管理员意图与节点本地计算出的 effective capability 分开保存。
    ego_browser_enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    registration_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    node_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)


class NodeHeartbeat(IdMixin, Base):
    """
    节点心跳快照
    """

    __tablename__ = "node_heartbeats"
    __table_args__ = (Index("node_heartbeats_node_created_idx", "node_id", "created_at"),)

    node_id: Mapped[UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    supported_tool_types: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    resources: Mapped[dict[str, object]] = mapped_column(JsonType, nullable=False, default=dict)
    runtime: Mapped[dict[str, object]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )


class NodeJoinCode(IdMixin, TimestampMixin, Base):
    """
    一次性 Node 加入码及其受保护的交换结果。
    """

    __tablename__ = "node_join_codes"
    __table_args__ = (
        Index("node_join_codes_hash_uidx", "code_hash", unique=True),
        Index("node_join_codes_node_status_idx", "node_id", "consumed_at", "revoked_at"),
        # 已消费交换仅凭此 ID 恢复；全局唯一性禁止不同短期码生成冲突结果。
        Index("node_join_codes_exchange_uidx", "exchange_id", unique=True),
    )

    node_id: Mapped[UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    issuer_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    server_origin: Mapped[str] = mapped_column(String(255), nullable=False)
    release_profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    wrapper_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skill_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    runtime_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    profile_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ego_browser_enabled: Mapped[bool | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exchange_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    encrypted_node_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    exchange_result_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class NodeTask(IdMixin, TimestampMixin, Base):
    """
    管理端下发给节点的持久任务
    """

    __tablename__ = "node_tasks"
    __table_args__ = (
        Index("node_tasks_task_id_uidx", "task_id", unique=True),
        Index("node_tasks_poll_idx", "node_id", "status", "lease_until"),
    )

    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    node_id: Mapped[UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JsonType, nullable=False, default=dict)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class NodeTaskResult(IdMixin, Base):
    """
    节点任务执行结果
    """

    __tablename__ = "node_task_results"
    __table_args__ = (Index("node_task_results_task_id_idx", "task_id"),)

    node_task_id: Mapped[UUID] = mapped_column(
        ForeignKey("node_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, object] | None] = mapped_column(JsonType, nullable=True)
    error: Mapped[dict[str, object] | None] = mapped_column(JsonType, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False
    )
