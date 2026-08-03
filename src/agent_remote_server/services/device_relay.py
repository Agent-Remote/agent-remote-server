import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.device_relay_store import (
    DeviceRelayBinding,
    DeviceRelayRole,
    DeviceRelayStore,
    DeviceRelayTicketClaims,
)
from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuthToken, DeviceSession, Node
from agent_remote_server.repositories.device_sessions import DeviceSessionRepository
from agent_remote_server.security import create_opaque_token, hash_token

RELAY_ELIGIBLE_STATUSES = {"pending_device", "pending_user_approval", "active"}


@dataclass(frozen=True)
class IssuedDeviceRelayMaterial:
    """
    已签发的本代设备中继连接材料
    """

    status: Literal["waiting", "ready"]
    role: DeviceRelayRole
    generation: int
    relay_ticket: str | None
    peer_spki_sha256: str | None
    exporter_context: str | None
    expires_at: datetime | None


class DeviceRelayService:
    """
    设备中继临时连接材料签发服务
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        relay_store: DeviceRelayStore,
    ) -> None:
        """
        初始化设备中继临时连接材料签发服务

        :param session (AsyncSession): 异步数据库会话
        :param settings (Settings): 应用配置
        :param relay_store (DeviceRelayStore): 设备中继短期状态存储
        """

        self._session = session
        self._settings = settings
        self._relay_store = relay_store
        self._repository = DeviceSessionRepository(session)

    async def register_device(
        self,
        *,
        token: AuthToken,
        device_session_id: UUID,
        generation: int,
        spki_sha256: str,
    ) -> IssuedDeviceRelayMaterial:
        """
        由绑定设备注册本代临时公钥并获取对端材料

        :param token (AuthToken): 当前设备认证令牌
        :param device_session_id (UUID): 设备控制会话 ID
        :param generation (int): 当前连接代次
        :param spki_sha256 (str): 本端临时证书 SPKI 摘要

        :return IssuedDeviceRelayMaterial: 本代设备角色连接材料
        """

        device_session = await self._require_session(device_session_id)
        if token.token_type != "device" or token.user_device_id != device_session.device_id:
            raise ApiError(
                code="COMMON_NOT_FOUND",
                message="Device session was not found.",
                status_code=404,
            )
        return await self._register(
            device_session=device_session,
            role="device",
            credential_id=token.id,
            generation=generation,
            spki_sha256=spki_sha256,
        )

    async def register_proxy(
        self,
        *,
        node: Node,
        device_session_id: UUID,
        generation: int,
        spki_sha256: str,
    ) -> IssuedDeviceRelayMaterial:
        """
        由绑定 Node 注册远端 proxy 本代临时公钥并获取对端材料

        :param node (Node): 当前认证 Node
        :param device_session_id (UUID): 设备控制会话 ID
        :param generation (int): 当前连接代次
        :param spki_sha256 (str): proxy 临时证书 SPKI 摘要

        :return IssuedDeviceRelayMaterial: 本代 proxy 角色连接材料
        """

        device_session = await self._require_session(device_session_id)
        if node.id != device_session.node_id:
            raise ApiError(
                code="COMMON_NOT_FOUND",
                message="Device session was not found.",
                status_code=404,
            )
        return await self._register(
            device_session=device_session,
            role="proxy",
            credential_id=None,
            generation=generation,
            spki_sha256=spki_sha256,
        )

    async def _register(
        self,
        *,
        device_session: DeviceSession,
        role: DeviceRelayRole,
        credential_id: UUID | None,
        generation: int,
        spki_sha256: str,
    ) -> IssuedDeviceRelayMaterial:
        now = datetime.now(UTC)
        expires_at = self._aware(device_session.expires_at)
        if expires_at <= now:
            raise ApiError(
                code="DEVICE_CONTROL_SESSION_EXPIRED",
                message="Device session has expired.",
                status_code=409,
            )
        if device_session.status not in RELAY_ELIGIBLE_STATUSES:
            raise ApiError(
                code="DEVICE_CONTROL_STATE_CONFLICT",
                message="Device session state does not permit relay material.",
                status_code=409,
            )
        if generation != device_session.generation:
            raise ApiError(
                code="DEVICE_CONTROL_GENERATION_MISMATCH",
                message="Device session generation does not match.",
                status_code=409,
            )

        material_expires_at = min(
            expires_at,
            now + timedelta(seconds=self._settings.device_relay_material_ttl_seconds),
        )
        ttl = max(1, int((material_expires_at - now).total_seconds()))
        binding = self._binding(device_session)
        try:
            exchange = await self._relay_store.exchange(
                binding=binding,
                role=role,
                spki_sha256=spki_sha256,
                exporter_context=secrets.token_hex(32),
                ttl=ttl,
            )
        except ValueError as exc:
            raise ApiError(
                code="DEVICE_CONTROL_RELAY_KEY_CHANGED",
                message="The relay key changed within the current generation.",
                status_code=409,
            ) from exc
        if exchange.status == "waiting":
            return IssuedDeviceRelayMaterial(
                status="waiting",
                role=role,
                generation=generation,
                relay_ticket=None,
                peer_spki_sha256=None,
                exporter_context=None,
                expires_at=None,
            )
        if exchange.status == "already_issued":
            raise ApiError(
                code="DEVICE_CONTROL_RELAY_MATERIAL_ALREADY_ISSUED",
                message="Relay material was already issued for this role and generation.",
                status_code=409,
            )
        if exchange.peer_spki_sha256 is None or exchange.exporter_context is None:
            raise RuntimeError("ready relay exchange omitted peer material")

        relay_ticket = create_opaque_token("drelay")
        await self._relay_store.issue_ticket(
            token_hash=hash_token(self._settings.secret_key, relay_ticket),
            claims=DeviceRelayTicketClaims(
                binding=binding,
                role=role,
                credential_id=credential_id,
            ),
            ttl=self._settings.device_relay_ticket_ttl_seconds,
        )
        return IssuedDeviceRelayMaterial(
            status="ready",
            role=role,
            generation=generation,
            relay_ticket=relay_ticket,
            peer_spki_sha256=exchange.peer_spki_sha256,
            exporter_context=exchange.exporter_context,
            expires_at=material_expires_at,
        )

    async def _require_session(self, device_session_id: UUID) -> DeviceSession:
        device_session = await self._repository.get(device_session_id)
        if device_session is None:
            raise ApiError(
                code="COMMON_NOT_FOUND",
                message="Device session was not found.",
                status_code=404,
            )
        return device_session

    def _binding(self, device_session: DeviceSession) -> DeviceRelayBinding:
        return DeviceRelayBinding(
            user_id=device_session.user_id,
            device_id=device_session.device_id,
            tool_session_id=device_session.binding_tool_session_id,
            device_session_id=device_session.id,
            node_id=device_session.node_id,
            generation=device_session.generation,
        )

    def _aware(self, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)
