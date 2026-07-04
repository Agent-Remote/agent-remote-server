from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON as JsonType
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
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
    supported_tool_types: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
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
