from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Response, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import (
    get_current_node,
    get_current_token,
    get_current_user,
    get_device_relay_store,
    get_session,
    get_settings,
    require_admin,
    require_current_device_control_release,
)
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.device_control_release import (
    DeviceControlReleaseEvidence,
    DeviceControlReleaseEvidenceError,
    ensure_device_control_release_evidence_current,
)
from agent_remote_server.device_relay_hub import DeviceRelayHub
from agent_remote_server.device_relay_store import DeviceRelayStore, DeviceRelayTicketClaims
from agent_remote_server.errors import ApiError
from agent_remote_server.models import AuthToken, DeviceSession, Node, User
from agent_remote_server.repositories.device_sessions import DeviceSessionRepository
from agent_remote_server.schemas.device_sessions import (
    AbortDeviceActionRequest,
    ApproveDeviceSessionRequest,
    CreateDeviceSessionRequest,
    DeviceControlPolicyData,
    DeviceControlPolicyResponse,
    DeviceRelayMaterialData,
    DeviceRelayMaterialResponse,
    DeviceSessionData,
    DeviceSessionListData,
    DeviceSessionListResponse,
    DeviceSessionResponse,
    DeviceSessionStatus,
    RegisterDeviceRelayMaterialRequest,
    RenewDeviceSessionRequest,
    StopDeviceSessionRequest,
)
from agent_remote_server.security import hash_token
from agent_remote_server.services.device_relay import (
    DeviceRelayService,
    IssuedDeviceRelayMaterial,
)
from agent_remote_server.services.device_sessions import DeviceSessionService

router = APIRouter(prefix="/device-sessions", tags=["device-sessions"])
node_router = APIRouter(prefix="/node-api/device-sessions", tags=["node-api"])
DeviceControlReleaseGate = Annotated[None, Depends(require_current_device_control_release)]


@router.get("/policy", response_model=DeviceControlPolicyResponse)
async def get_device_control_policy(
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(require_admin)],
) -> DeviceControlPolicyResponse:
    """
    读取管理员可见的设备控制部署策略

    :param settings (Settings): 应用配置
    :param user (User): 当前管理员用户

    :return DeviceControlPolicyResponse: 不含连接材料的设备控制部署策略响应
    """

    return DeviceControlPolicyResponse(
        data=DeviceControlPolicyData(
            enabled=settings.device_control_enabled,
            platform="macos",
            protocol_version=1,
            lease_seconds=settings.device_session_lease_seconds,
            maximum_ttl_seconds=settings.device_session_max_ttl_seconds,
            relay_maximum_frame_bytes=settings.device_relay_max_frame_bytes,
            relay_maximum_bytes_per_second=settings.device_relay_max_bytes_per_second,
            relay_maximum_connection_seconds=settings.device_relay_max_connection_seconds,
            local_approval_required=True,
        ),
        request_id=get_request_id(),
    )


def _data(device_session: DeviceSession) -> DeviceSessionData:
    """
    将设备控制实体转换为公开的零内容响应

    :param device_session (DeviceSession): 设备控制会话实体

    :return DeviceSessionData: 设备控制会话公开数据
    """

    return DeviceSessionData(
        id=device_session.id,
        user_id=device_session.user_id,
        device_id=device_session.device_id,
        tool_session_id=device_session.tool_session_id,
        node_id=device_session.node_id,
        platform="macos",
        status=cast(DeviceSessionStatus, device_session.status),
        generation=device_session.generation,
        lease_until=device_session.lease_until,
        expires_at=device_session.expires_at,
        lock_acquired_at=device_session.lock_acquired_at,
        stopped_at=device_session.stopped_at,
        stop_reason=device_session.stop_reason,
        created_at=device_session.created_at,
    )


def _relay_material_data(
    device_session_id: UUID,
    material: IssuedDeviceRelayMaterial,
) -> DeviceRelayMaterialData:
    """
    转换设备中继临时连接材料响应

    :param device_session_id (UUID): 设备控制会话 ID
    :param material (IssuedDeviceRelayMaterial): 已签发临时连接材料

    :return DeviceRelayMaterialData: 设备中继临时连接材料数据
    """

    relay_path = (
        f"/api/v1/device-sessions/{device_session_id}/relay" if material.status == "ready" else None
    )
    return DeviceRelayMaterialData(
        status=material.status,
        role=material.role,
        generation=material.generation,
        relay_path=relay_path,
        relay_ticket=material.relay_ticket,
        peer_spki_sha256=material.peer_spki_sha256,
        exporter_context=material.exporter_context,
        expires_at=material.expires_at,
    )


@router.post("", response_model=DeviceSessionResponse)
async def create_device_session(
    payload: CreateDeviceSessionRequest,
    _release_gate: DeviceControlReleaseGate,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> DeviceSessionResponse:
    """
    创建严格绑定当前用户、设备和远端工具 session 的控制会话

    :param payload (CreateDeviceSessionRequest): 创建设备控制会话请求
    :param _release_gate (None): 当前生产发布证据门禁
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param token (AuthToken): 当前认证令牌

    :return DeviceSessionResponse: 新建设备控制会话响应
    """

    device_session = await DeviceSessionService(session, settings).create(
        user=user,
        token=token,
        device_id=payload.device_id,
        tool_session_id=payload.tool_session_id,
    )
    return DeviceSessionResponse(data=_data(device_session), request_id=get_request_id())


@router.get("", response_model=DeviceSessionListResponse)
async def list_device_sessions(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    all_users: bool = False,
) -> DeviceSessionListResponse:
    """
    列出当前用户的设备控制会话

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param all_users (bool): 是否以管理员身份读取全部用户会话

    :return DeviceSessionListResponse: 设备控制会话列表响应
    """

    service = DeviceSessionService(session, settings)
    items = (
        await service.list_for_admin(user=user)
        if all_users
        else await service.list_for_user(user=user)
    )
    return DeviceSessionListResponse(
        data=DeviceSessionListData(items=[_data(item) for item in items]),
        request_id=get_request_id(),
    )


@router.get("/device-inbox", response_model=DeviceSessionListResponse)
async def list_device_session_inbox(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> DeviceSessionListResponse:
    """
    列出当前认证设备可处理的控制会话

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param token (AuthToken): 当前设备认证令牌

    :return DeviceSessionListResponse: 不含审批和连接材料的设备会话 inbox
    """

    items = await DeviceSessionService(session, settings).list_for_device(token=token)
    return DeviceSessionListResponse(
        data=DeviceSessionListData(items=[_data(item) for item in items]),
        request_id=get_request_id(),
    )


@router.get("/{device_session_id}", response_model=DeviceSessionResponse)
async def get_device_session(
    device_session_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> DeviceSessionResponse:
    """
    读取当前用户拥有的设备控制会话

    :param device_session_id (UUID): 设备控制会话 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return DeviceSessionResponse: 设备控制会话响应
    """

    device_session = await DeviceSessionService(session, settings).get_for_user(
        user=user, device_session_id=device_session_id
    )
    return DeviceSessionResponse(data=_data(device_session), request_id=get_request_id())


@router.post("/{device_session_id}/device-connected", response_model=DeviceSessionResponse)
async def mark_device_connected(
    device_session_id: UUID,
    payload: RenewDeviceSessionRequest,
    _release_gate: DeviceControlReleaseGate,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> DeviceSessionResponse:
    """
    由绑定设备确认通道连接并进入本机审批状态

    :param device_session_id (UUID): 设备控制会话 ID
    :param payload (RenewDeviceSessionRequest): 当前连接代次请求
    :param _release_gate (None): 当前生产发布证据门禁
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param token (AuthToken): 当前设备认证令牌

    :return DeviceSessionResponse: 更新后的设备控制会话响应
    """

    device_session = await DeviceSessionService(session, settings).mark_device_connected(
        token=token,
        device_session_id=device_session_id,
        generation=payload.generation,
    )
    return DeviceSessionResponse(data=_data(device_session), request_id=get_request_id())


@router.post("/{device_session_id}/approve", response_model=DeviceSessionResponse)
async def approve_device_session(
    device_session_id: UUID,
    payload: ApproveDeviceSessionRequest,
    _release_gate: DeviceControlReleaseGate,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> DeviceSessionResponse:
    """
    保存绑定设备上的用户审批摘要并激活短租约

    :param device_session_id (UUID): 设备控制会话 ID
    :param payload (ApproveDeviceSessionRequest): 本机应用审批请求
    :param _release_gate (None): 当前生产发布证据门禁
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param token (AuthToken): 当前设备认证令牌

    :return DeviceSessionResponse: 审批后的设备控制会话响应
    """

    device_session = await DeviceSessionService(session, settings).approve(
        token=token,
        device_session_id=device_session_id,
        generation=payload.generation,
        approvals=payload.approvals,
    )
    return DeviceSessionResponse(data=_data(device_session), request_id=get_request_id())


@router.post("/{device_session_id}/lock", response_model=DeviceSessionResponse)
async def acquire_device_lock(
    device_session_id: UUID,
    payload: RenewDeviceSessionRequest,
    _release_gate: DeviceControlReleaseGate,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> DeviceSessionResponse:
    """
    由绑定设备在首次成功动作后获取机器控制锁

    :param device_session_id (UUID): 设备控制会话 ID
    :param payload (RenewDeviceSessionRequest): 当前连接代次请求
    :param _release_gate (None): 当前生产发布证据门禁
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param token (AuthToken): 当前设备认证令牌

    :return DeviceSessionResponse: 已持有机器锁的设备控制会话响应
    """

    device_session = await DeviceSessionService(session, settings).acquire_lock(
        token=token,
        device_session_id=device_session_id,
        generation=payload.generation,
    )
    return DeviceSessionResponse(data=_data(device_session), request_id=get_request_id())


@router.post("/{device_session_id}/renew", response_model=DeviceSessionResponse)
async def renew_device_session(
    device_session_id: UUID,
    payload: RenewDeviceSessionRequest,
    _release_gate: DeviceControlReleaseGate,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> DeviceSessionResponse:
    """
    由绑定设备续订当前代次短租约

    :param device_session_id (UUID): 设备控制会话 ID
    :param payload (RenewDeviceSessionRequest): 当前连接代次请求
    :param _release_gate (None): 当前生产发布证据门禁
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param token (AuthToken): 当前设备认证令牌

    :return DeviceSessionResponse: 已续租的设备控制会话响应
    """

    device_session = await DeviceSessionService(session, settings).renew(
        token=token,
        device_session_id=device_session_id,
        generation=payload.generation,
    )
    return DeviceSessionResponse(data=_data(device_session), request_id=get_request_id())


@router.post("/{device_session_id}/reconnect", response_model=DeviceSessionResponse)
async def reconnect_device_session(
    device_session_id: UUID,
    payload: RenewDeviceSessionRequest,
    _release_gate: DeviceControlReleaseGate,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> DeviceSessionResponse:
    """
    由绑定设备创建新连接代次且不重放动作

    :param device_session_id (UUID): 设备控制会话 ID
    :param payload (RenewDeviceSessionRequest): 断线前连接代次请求
    :param _release_gate (None): 当前生产发布证据门禁
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param token (AuthToken): 当前设备认证令牌

    :return DeviceSessionResponse: 进入新代次的设备控制会话响应
    """

    device_session = await DeviceSessionService(session, settings).reconnect(
        token=token,
        device_session_id=device_session_id,
        generation=payload.generation,
    )
    return DeviceSessionResponse(data=_data(device_session), request_id=get_request_id())


@router.post("/{device_session_id}/abort", response_model=DeviceSessionResponse)
async def abort_device_action(
    device_session_id: UUID,
    payload: AbortDeviceActionRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> DeviceSessionResponse:
    """
    由绑定设备中止当前动作但保留机器锁

    :param device_session_id (UUID): 设备控制会话 ID
    :param payload (AbortDeviceActionRequest): 当前动作中止请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param token (AuthToken): 当前设备认证令牌

    :return DeviceSessionResponse: 等待新代次连接的设备控制会话响应
    """

    device_session = await DeviceSessionService(session, settings).abort_action(
        token=token,
        device_session_id=device_session_id,
        generation=payload.generation,
        reason=payload.reason,
    )
    return DeviceSessionResponse(data=_data(device_session), request_id=get_request_id())


@router.post("/{device_session_id}/stop", response_model=DeviceSessionResponse)
async def stop_device_session(
    device_session_id: UUID,
    payload: StopDeviceSessionRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> DeviceSessionResponse:
    """
    由当前用户或绑定设备立即停止并撤销设备控制

    :param device_session_id (UUID): 设备控制会话 ID
    :param payload (StopDeviceSessionRequest): 停止原因请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :param token (AuthToken): 当前认证令牌

    :return DeviceSessionResponse: 已停止的设备控制会话响应
    """

    service = DeviceSessionService(session, settings)
    if token.token_type == "device":
        device_session = await service.stop_by_device(
            token=token, device_session_id=device_session_id, reason=payload.reason
        )
    elif token.token_type == "user":
        if user.role == "admin":
            device_session = await service.stop_by_admin(
                user=user, device_session_id=device_session_id, reason=payload.reason
            )
        else:
            device_session = await service.stop_by_user(
                user=user, device_session_id=device_session_id, reason=payload.reason
            )
    else:
        raise ApiError(code="COMMON_FORBIDDEN", message="Unsupported token type.", status_code=403)
    return DeviceSessionResponse(data=_data(device_session), request_id=get_request_id())


@router.post(
    "/{device_session_id}/relay-material",
    response_model=DeviceRelayMaterialResponse,
)
async def register_device_relay_material(
    device_session_id: UUID,
    payload: RegisterDeviceRelayMaterialRequest,
    response: Response,
    _release_gate: DeviceControlReleaseGate,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[AuthToken, Depends(get_current_token)],
    relay_store: Annotated[DeviceRelayStore, Depends(get_device_relay_store)],
) -> DeviceRelayMaterialResponse:
    """
    由绑定设备注册本代临时公钥并仅一次获取对端连接材料

    :param device_session_id (UUID): 设备控制会话 ID
    :param payload (RegisterDeviceRelayMaterialRequest): 本端临时公钥请求
    :param response (Response): HTTP 响应对象
    :param _release_gate (None): 当前生产发布证据门禁
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param token (AuthToken): 当前设备认证令牌
    :param relay_store (DeviceRelayStore): 设备中继短期状态存储

    :return DeviceRelayMaterialResponse: 设备角色临时连接材料响应
    """

    material = await DeviceRelayService(session, settings, relay_store).register_device(
        token=token,
        device_session_id=device_session_id,
        generation=payload.generation,
        spki_sha256=payload.spki_sha256,
    )
    response.headers["Cache-Control"] = "no-store"
    return DeviceRelayMaterialResponse(
        data=_relay_material_data(device_session_id, material),
        request_id=get_request_id(),
    )


@node_router.post(
    "/{device_session_id}/relay-material",
    response_model=DeviceRelayMaterialResponse,
)
async def register_proxy_relay_material(
    device_session_id: UUID,
    payload: RegisterDeviceRelayMaterialRequest,
    response: Response,
    _release_gate: DeviceControlReleaseGate,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    node: Annotated[Node, Depends(get_current_node)],
    relay_store: Annotated[DeviceRelayStore, Depends(get_device_relay_store)],
) -> DeviceRelayMaterialResponse:
    """
    由绑定 Node 注册 proxy 本代临时公钥并仅一次获取对端连接材料

    :param device_session_id (UUID): 设备控制会话 ID
    :param payload (RegisterDeviceRelayMaterialRequest): proxy 临时公钥请求
    :param response (Response): HTTP 响应对象
    :param _release_gate (None): 当前生产发布证据门禁
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param node (Node): 当前认证 Node
    :param relay_store (DeviceRelayStore): 设备中继短期状态存储

    :return DeviceRelayMaterialResponse: proxy 角色临时连接材料响应
    """

    material = await DeviceRelayService(session, settings, relay_store).register_proxy(
        node=node,
        device_session_id=device_session_id,
        generation=payload.generation,
        spki_sha256=payload.spki_sha256,
    )
    response.headers["Cache-Control"] = "no-store"
    return DeviceRelayMaterialResponse(
        data=_relay_material_data(device_session_id, material),
        request_id=get_request_id(),
    )


@router.websocket("/{device_session_id}/relay")
async def relay_device_ciphertext(
    device_session_id: UUID,
    websocket: WebSocket,
) -> None:
    """
    消费角色绑定的一次性票据并只转发限长密文帧

    :param device_session_id (UUID): 设备控制会话 ID
    :param websocket (WebSocket): 当前角色 WebSocket
    """

    settings: Settings = websocket.app.state.settings
    evidence: DeviceControlReleaseEvidence | None = getattr(
        websocket.app.state,
        "device_control_release_evidence",
        None,
    )
    try:
        ensure_device_control_release_evidence_current(
            environment=settings.environment,
            enabled=settings.device_control_enabled,
            evidence=evidence,
        )
    except DeviceControlReleaseEvidenceError:
        await websocket.close(code=1008)
        return

    authorization = websocket.headers.get("authorization", "")
    scheme, _, ticket = authorization.partition(" ")
    if scheme.lower() != "bearer" or not ticket:
        await websocket.close(code=1008)
        return
    relay_store: DeviceRelayStore = websocket.app.state.device_relay_store
    claims = await relay_store.consume_ticket(token_hash=hash_token(settings.secret_key, ticket))
    if claims is None or claims.binding.device_session_id != device_session_id:
        await websocket.close(code=1008)
        return
    session_factory = websocket.app.state.session_factory
    async with session_factory() as session:
        if not await _relay_claims_are_current(session, claims):
            await websocket.close(code=1008)
            return
    hub: DeviceRelayHub = websocket.app.state.device_relay_hub
    await hub.connect(claims, websocket)


async def _relay_claims_are_current(
    session: AsyncSession,
    claims: DeviceRelayTicketClaims,
) -> bool:
    """
    重新校验一次性票据对应的完整当前授权状态

    :param session (AsyncSession): 数据库会话
    :param claims (DeviceRelayTicketClaims): 已消费票据声明

    :return bool: 票据绑定是否仍然有效
    """

    binding = claims.binding
    repository = DeviceSessionRepository(session)
    device_session = await repository.get(binding.device_session_id)
    if device_session is None:
        return False
    exact_binding = (
        device_session.user_id == binding.user_id
        and device_session.device_id == binding.device_id
        and device_session.tool_session_id == binding.tool_session_id
        and device_session.node_id == binding.node_id
        and device_session.generation == binding.generation
    )
    expires_at = (
        device_session.expires_at
        if device_session.expires_at.tzinfo
        else device_session.expires_at.replace(tzinfo=UTC)
    )
    if (
        not exact_binding
        or device_session.status not in {"pending_device", "pending_user_approval", "active"}
        or expires_at <= datetime.now(UTC)
    ):
        return False
    device = await repository.get_device(binding.device_id)
    node = await repository.get_node(binding.node_id)
    tool_session = await repository.get_tool_session(binding.tool_session_id)
    if (
        device is None
        or device.status != "active"
        or node is None
        or node.status not in {"healthy", "degraded"}
        or tool_session is None
        or tool_session.status not in {"running", "active", "detached"}
    ):
        return False
    if claims.role == "proxy":
        return claims.credential_id is None
    if claims.credential_id is None:
        return False
    credential = await session.get(AuthToken, claims.credential_id)
    if credential is None:
        return False
    credential_expires_at = (
        credential.expires_at
        if credential.expires_at.tzinfo
        else credential.expires_at.replace(tzinfo=UTC)
    )
    return (
        credential.status == "active"
        and credential.token_type == "device"
        and credential.user_device_id == binding.device_id
        and credential_expires_at > datetime.now(UTC)
    )
