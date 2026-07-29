from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import AuditLog, PortForward, Session
from agent_remote_server.repositories.port_forwards import NON_TERMINAL_FORWARD_STATUSES


async def revoke_port_forwards(
    database: AsyncSession,
    *,
    reason: str,
    actor_user_id: UUID | None,
    user_id: UUID | None = None,
    device_id: UUID | None = None,
    session_id: UUID | None = None,
    node_id: UUID | None = None,
    tool_account_id: UUID | None = None,
) -> int:
    """在所属资源撤销事务内同步终止关联端口转发。"""

    statement = select(PortForward).where(PortForward.status.in_(NON_TERMINAL_FORWARD_STATUSES))
    if user_id is not None:
        statement = statement.where(PortForward.user_id == user_id)
    if device_id is not None:
        statement = statement.where(PortForward.device_id == device_id)
    if session_id is not None:
        statement = statement.where(PortForward.session_id == session_id)
    if node_id is not None:
        statement = statement.where(PortForward.node_id == node_id)
    if tool_account_id is not None:
        statement = statement.join(Session, Session.id == PortForward.session_id).where(
            Session.tool_account_id == tool_account_id
        )
    values = list(await database.scalars(statement.with_for_update()))
    now = datetime.now(UTC)
    for port_forward in values:
        port_forward.status = "revoked"
        port_forward.stopped_at = now
        port_forward.stop_reason = reason
        port_forward.lease_expires_at = None
        database.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action="port_forward.revoked",
                target_type="port_forward",
                target_id=str(port_forward.id),
                details={"reason": reason},
            )
        )
    return len(values)
