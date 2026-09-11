from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Response, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import (
    EgoBrowserDeviceAuth,
    EgoBrowserPrincipal,
    get_current_node,
    get_current_user,
    get_ego_browser_device_auth,
    get_ego_browser_principal,
    get_ego_browser_relay_store,
    get_ego_browser_revocation_bus,
    get_ego_browser_user_or_device_auth,
    get_session,
    get_settings,
)
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.ego_browser.relay import (
    EGO_BROWSER_PROTOCOL,
    EgoBrowserRelayHub,
    EgoBrowserRelayStore,
    EgoBrowserRelayTicketClaims,
    EgoBrowserRevocationPublisher,
)
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    EgoBrowserBinding,
    EgoBrowserDevice,
    EgoBrowserRequestLedger,
    Node,
    User,
)
from agent_remote_server.repositories.ego_browser import EgoBrowserRepository
from agent_remote_server.schemas.auth import EmptyResponse
from agent_remote_server.schemas.ego_browser import (
    EgoBrowserActiveRequestData,
    EgoBrowserActiveRequestListData,
    EgoBrowserActiveRequestListResponse,
    EgoBrowserAllowlistConfirmRequest,
    EgoBrowserAllowlistData,
    EgoBrowserAllowlistResponse,
    EgoBrowserBindingCandidateData,
    EgoBrowserBindingCandidateListData,
    EgoBrowserBindingCandidateListResponse,
    EgoBrowserBindingClaimRequest,
    EgoBrowserBindingData,
    EgoBrowserBindingListData,
    EgoBrowserBindingListResponse,
    EgoBrowserBindingResponse,
    EgoBrowserBindingStatus,
    EgoBrowserCancelRequest,
    EgoBrowserCancelResponse,
    EgoBrowserConcurrencyMode,
    EgoBrowserConnectedRequest,
    EgoBrowserCredentialProfile,
    EgoBrowserDeviceCredentialIssueData,
    EgoBrowserDeviceData,
    EgoBrowserDeviceListData,
    EgoBrowserDeviceListResponse,
    EgoBrowserDeviceRegisterRequest,
    EgoBrowserDeviceResponse,
    EgoBrowserDeviceRevokeRequest,
    EgoBrowserDeviceStatus,
    EgoBrowserLeaseHealth,
    EgoBrowserLifecycleRequest,
    EgoBrowserNodeBindingData,
    EgoBrowserNodeBindingListData,
    EgoBrowserNodeBindingListResponse,
    EgoBrowserNodeRenewData,
    EgoBrowserNodeRenewRequest,
    EgoBrowserNodeRenewResponse,
    EgoBrowserPolicyData,
    EgoBrowserPolicyResponse,
    EgoBrowserProofChallengeData,
    EgoBrowserProofChallengeRequest,
    EgoBrowserProofChallengeResponse,
    EgoBrowserProtocol,
    EgoBrowserRelayTicketData,
    EgoBrowserRelayTicketRequest,
    EgoBrowserRelayTicketResponse,
    EgoBrowserReleaseProfile,
    EgoBrowserRenewRequest,
    EgoBrowserRequestStatus,
    EgoBrowserResumeRequest,
)
from agent_remote_server.services.ego_browser import (
    EgoBrowserDeviceCredentialIssue,
    EgoBrowserProofChallengeIssue,
    EgoBrowserRelayTicketResult,
    EgoBrowserService,
)

router = APIRouter(prefix="/ego-browser", tags=["ego-browser"])
node_router = APIRouter(prefix="/node-api/ego-browser", tags=["node-api"])


def _device_data(
    device: EgoBrowserDevice,
    credential: EgoBrowserDeviceCredentialIssue | None = None,
) -> EgoBrowserDeviceData:
    """把独立 ego-browser 设备实体转换为零内容响应。"""

    credential_data = None
    if credential is not None:
        credential_data = EgoBrowserDeviceCredentialIssueData(
            id=credential.credential.id,
            ego_browser_device_id=credential.credential.ego_browser_device_id,
            credential_profile=cast(
                EgoBrowserCredentialProfile, credential.credential.credential_profile
            ),
            generation=credential.credential.generation,
            revision=credential.credential.revision,
            expires_at=_required_aware(credential.credential.expires_at),
            access_token=credential.raw_token,
            expires_in=credential.expires_in,
        )
    return EgoBrowserDeviceData(
        id=device.id,
        user_id=device.user_id,
        public_key=device.public_key,
        encryption_public_key=device.encryption_public_key,
        generation=device.generation,
        status=cast(EgoBrowserDeviceStatus, device.status),
        platform="macos",
        release_profile=cast(EgoBrowserReleaseProfile, device.release_profile),
        signer_certificate_sha256=device.signer_certificate_sha256,
        credential_profile=cast(EgoBrowserCredentialProfile, device.credential_profile),
        bridge_protocol_version=device.bridge_protocol_version,
        bridge_version=device.bridge_version,
        local_ego_browser_runtime_version=device.local_ego_browser_runtime_version,
        ego_lite_runtime_version=device.ego_lite_runtime_version,
        skill_version=device.skill_version,
        capabilities=list(device.capabilities),
        allowlist_revision=device.allowlist_revision,
        allowlist_roots_digest=device.allowlist_roots_digest,
        learning_bundle_digest=device.learning_bundle_digest,
        last_seen_at=_aware(device.last_seen_at),
        created_at=_required_aware(device.created_at),
        updated_at=_required_aware(device.updated_at),
        credential=credential_data,
    )


def _proof_challenge_data(
    issue: EgoBrowserProofChallengeIssue,
) -> EgoBrowserProofChallengeData:
    """把短期 PoP challenge 转换为无秘密响应。"""

    return EgoBrowserProofChallengeData(
        challenge=issue.challenge,
        expires_at=_required_aware(issue.expires_at),
    )


def _binding_data(
    binding: EgoBrowserBinding, *, encryption_public_key: str | None = None
) -> EgoBrowserBindingData:
    """把独立 binding 实体转换为零内容响应。"""

    return EgoBrowserBindingData(
        id=binding.id,
        user_id=binding.user_id,
        ego_browser_device_id=binding.ego_browser_device_id,
        # 密钥归独立设备记录所有；已联表调用方可用临时属性，常规 API 路径则显式传入。
        encryption_public_key=encryption_public_key
        if encryption_public_key is not None
        else getattr(binding, "encryption_public_key", None),
        tool_session_id=binding.binding_tool_session_id,
        node_id=binding.node_id,
        status=cast(EgoBrowserBindingStatus, binding.status),
        control_channel="ego_browser_bridge",
        relay_binding_kind="ego_browser",
        authorization_mode="ego_browser_script_full_trust",
        authorization_policy_version=binding.authorization_policy_version,
        authorized_at=_required_aware(binding.authorized_at),
        release_profile=cast(EgoBrowserReleaseProfile, binding.release_profile),
        signer_certificate_sha256=binding.signer_certificate_sha256,
        credential_profile=cast(EgoBrowserCredentialProfile, binding.credential_profile),
        remote_platform="linux",
        local_platform="macos",
        local_runtime_version=binding.local_runtime_version,
        ego_lite_runtime_version=binding.ego_lite_runtime_version,
        skill_version=binding.skill_version,
        bridge_protocol_version=binding.bridge_protocol_version,
        task_space_label=binding.task_space_label,
        allowlist_revision=binding.allowlist_revision,
        allowlist_roots_digest=binding.allowlist_roots_digest,
        learning_bundle_digest=binding.learning_bundle_digest,
        concurrency_mode=cast(EgoBrowserConcurrencyMode, binding.concurrency_mode),
        max_parallel_requests=binding.max_parallel_requests,
        capabilities=list(binding.capabilities),
        lease_until=_aware(binding.lease_until),
        lease_health=cast(EgoBrowserLeaseHealth, binding.lease_health),
        lease_grace_until=_aware(binding.lease_grace_until),
        lease_renew_interval_seconds=binding.lease_renew_interval_seconds,
        lease_renew_failure_grace_seconds=binding.lease_renew_failure_grace_seconds,
        absolute_ttl_until=_required_aware(binding.absolute_ttl_until),
        generation=binding.generation,
        connected_at=_aware(binding.connected_at),
        stopped_at=_aware(binding.stopped_at),
        stop_reason=binding.stop_reason,
        revoked_at=_aware(binding.revoked_at),
        created_at=_required_aware(binding.created_at),
        updated_at=_required_aware(binding.updated_at),
    )


def _request_data(request: EgoBrowserRequestLedger) -> EgoBrowserActiveRequestData:
    """把 request ledger 转换为不含浏览器内容的控制元数据。"""

    return EgoBrowserActiveRequestData(
        id=request.id,
        binding_id=request.binding_id,
        generation=request.generation,
        request_id=request.request_id,
        sequence=request.sequence,
        message_type="execute",
        payload_bytes=request.payload_bytes,
        status=cast(EgoBrowserRequestStatus, request.status),
        created_at=_required_aware(request.created_at),
    )


def _aware(value: datetime | None) -> datetime | None:
    """为 SQLite 返回的 naive datetime 补充 UTC 时区。"""

    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _required_aware(value: datetime) -> datetime:
    """为必填时间字段补充 UTC 时区。"""

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _device_key(session: AsyncSession, binding: EgoBrowserBinding) -> str | None:
    """读取 binding 对应的非敏感加密公钥。"""

    device = await EgoBrowserRepository(session).get_device(binding.ego_browser_device_id)
    return device.encryption_public_key if device is not None else None


def _ticket_data(
    result: EgoBrowserRelayTicketResult,
    binding_id: UUID,
) -> EgoBrowserRelayTicketData:
    """把一次性 relay 票据转换为响应数据。"""

    return EgoBrowserRelayTicketData(
        role=result.role,
        generation=result.generation,
        relay_binding_kind="ego_browser",
        relay_path=f"/api/v1/ego-browser/bindings/{binding_id}/relay",
        relay_ticket=result.relay_ticket,
        expires_at=_required_aware(result.expires_at),
    )


def _node_binding_data(
    binding: EgoBrowserBinding, *, encryption_public_key: str | None = None
) -> EgoBrowserNodeBindingData:
    """把 binding 转换为 Node 可见的非秘密元数据。"""

    return EgoBrowserNodeBindingData(
        binding_id=binding.id,
        ego_browser_device_id=binding.ego_browser_device_id,
        encryption_public_key=encryption_public_key
        if encryption_public_key is not None
        else getattr(binding, "encryption_public_key", None),
        tool_session_id=binding.binding_tool_session_id,
        node_id=binding.node_id,
        status=cast(EgoBrowserBindingStatus, binding.status),
        control_channel="ego_browser_bridge",
        relay_binding_kind="ego_browser",
        authorization_mode="ego_browser_script_full_trust",
        authorization_policy_version=binding.authorization_policy_version,
        generation=binding.generation,
        release_profile=cast(EgoBrowserReleaseProfile, binding.release_profile),
        signer_certificate_sha256=binding.signer_certificate_sha256,
        credential_profile=cast(EgoBrowserCredentialProfile, binding.credential_profile),
        remote_platform="linux",
        local_platform="macos",
        bridge_protocol_version=binding.bridge_protocol_version,
        local_runtime_version=binding.local_runtime_version,
        ego_lite_runtime_version=binding.ego_lite_runtime_version,
        skill_version=binding.skill_version,
        task_space_label=binding.task_space_label,
        allowlist_revision=binding.allowlist_revision,
        allowlist_roots_digest=binding.allowlist_roots_digest,
        learning_bundle_digest=binding.learning_bundle_digest,
        concurrency_mode=cast(EgoBrowserConcurrencyMode, binding.concurrency_mode),
        max_parallel_requests=binding.max_parallel_requests,
        capabilities=list(binding.capabilities),
        lease_until=_aware(binding.lease_until),
        lease_health=cast(EgoBrowserLeaseHealth, binding.lease_health),
        lease_grace_until=_aware(binding.lease_grace_until),
        lease_renew_interval_seconds=binding.lease_renew_interval_seconds,
        lease_renew_failure_grace_seconds=binding.lease_renew_failure_grace_seconds,
        absolute_ttl_until=_required_aware(binding.absolute_ttl_until),
    )


@router.get("/policy", response_model=EgoBrowserPolicyResponse)
async def get_policy(
    settings: Annotated[Settings, Depends(get_settings)],
    _user: Annotated[User, Depends(get_ego_browser_user_or_device_auth)],
) -> EgoBrowserPolicyResponse:
    """
    读取 ego-browser bridge 的公开策略元数据。

    :param settings (Settings): 应用配置
    :param _user (User): 已认证主体；仅用于执行访问控制

    :return EgoBrowserPolicyResponse: ego-browser Bridge 策略响应
    """

    return EgoBrowserPolicyResponse(
        data=EgoBrowserPolicyData(
            enabled=settings.ego_browser_bridge_enabled,
            protocol=cast(EgoBrowserProtocol, EGO_BROWSER_PROTOCOL),
            authorization_mode="ego_browser_script_full_trust",
            authorization_policy_version=1,
            remote_platform="linux",
            local_platform="macos",
            lease_seconds=settings.ego_browser_lease_seconds,
            lease_renew_interval_seconds=settings.ego_browser_lease_renew_interval_seconds,
            lease_renew_failure_grace_seconds=settings.ego_browser_lease_renew_failure_grace_seconds,
            admission_min_remaining_seconds=settings.ego_browser_lease_admission_min_remaining_seconds,
            absolute_ttl_seconds=settings.ego_browser_absolute_ttl_seconds,
            max_parallel_requests=settings.ego_browser_max_parallel_requests,
            max_frame_bytes=settings.ego_browser_relay_max_frame_bytes,
            max_script_bytes=1_048_576,
        ),
        request_id=get_request_id(),
    )


@router.get("/devices", response_model=EgoBrowserDeviceListResponse)
async def list_ego_browser_devices(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_ego_browser_user_or_device_auth)],
    all_users: bool = False,
) -> EgoBrowserDeviceListResponse:
    """
    列出当前用户注册的独立 ego-browser 设备。

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param user (User): 当前操作用户
    :param all_users (bool): 是否包含其他用户拥有的记录

    :return EgoBrowserDeviceListResponse: 独立设备列表响应
    """

    devices = await EgoBrowserService(session, settings).list_devices(
        user=user, all_users=all_users
    )
    return EgoBrowserDeviceListResponse(
        data=EgoBrowserDeviceListData(items=[_device_data(device) for device in devices]),
        request_id=get_request_id(),
    )


@router.post("/devices/register", response_model=EgoBrowserDeviceResponse)
async def register_ego_browser_device(
    payload: EgoBrowserDeviceRegisterRequest,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    relay_store: Annotated[EgoBrowserRelayStore, Depends(get_ego_browser_relay_store)],
    revocation_bus: Annotated[
        EgoBrowserRevocationPublisher,
        Depends(get_ego_browser_revocation_bus),
    ],
) -> EgoBrowserDeviceResponse:
    """
    注册或轮换当前用户的独立 ego-browser 设备密钥。

    :param payload (EgoBrowserDeviceRegisterRequest): 独立设备注册或密钥轮换请求
    :param response (Response): 用于设置安全响应头的 HTTP 响应
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param user (User): 当前操作用户
    :param relay_store (EgoBrowserRelayStore): ego-browser 一次性票据与 PoP challenge 存储
    :param revocation_bus (EgoBrowserRevocationPublisher): ego-browser generation 撤销发布器

    :return EgoBrowserDeviceResponse: 独立设备响应
    """

    service = EgoBrowserService(
        session,
        settings,
        relay_store=relay_store,
        revocation_publisher=revocation_bus,
    )
    device = await service.register_device(
        user=user,
        payload=payload,
        commit=False,
    )
    credential = await service.issue_device_credential(
        user=user,
        device_id=device.id,
        commit=False,
    )
    await session.commit()
    await service.publish_pending_revocations()
    response.headers["Cache-Control"] = "no-store"
    return EgoBrowserDeviceResponse(
        data=_device_data(device, credential), request_id=get_request_id()
    )


@router.post("/devices/{device_id}/revoke", response_model=EgoBrowserDeviceResponse)
async def revoke_ego_browser_device(
    device_id: UUID,
    payload: EgoBrowserDeviceRevokeRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[EgoBrowserPrincipal, Depends(get_ego_browser_principal)],
    relay_store: Annotated[EgoBrowserRelayStore, Depends(get_ego_browser_relay_store)],
    revocation_bus: Annotated[
        EgoBrowserRevocationPublisher,
        Depends(get_ego_browser_revocation_bus),
    ],
) -> EgoBrowserDeviceResponse:
    """
    永久撤销独立设备、全部 binding 和全部设备凭据。

    :param device_id (UUID): 独立 ego-browser 设备 ID
    :param payload (EgoBrowserDeviceRevokeRequest): 独立设备永久撤销请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param principal (EgoBrowserPrincipal): 当前认证的用户或独立设备主体
    :param relay_store (EgoBrowserRelayStore): ego-browser 一次性票据与 PoP challenge 存储
    :param revocation_bus (EgoBrowserRevocationPublisher): ego-browser generation 撤销发布器

    :return EgoBrowserDeviceResponse: 独立设备响应
    """

    device = await EgoBrowserService(
        session,
        settings,
        relay_store=relay_store,
        revocation_publisher=revocation_bus,
    ).revoke_device(
        user=principal.user,
        device_id=device_id,
        payload=payload,
        authenticated_device_id=(
            principal.device_auth.device.id if principal.device_auth is not None else None
        ),
    )
    return EgoBrowserDeviceResponse(data=_device_data(device), request_id=get_request_id())


@router.delete("/devices/{device_id}", response_model=EmptyResponse)
async def delete_ego_browser_device(
    device_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> EmptyResponse:
    """
    删除已撤销且没有 binding 历史的独立设备。

    :param device_id (UUID): 独立 ego-browser 设备 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param user (User): 当前用户或管理员

    :return EmptyResponse: 空响应
    """

    await EgoBrowserService(session, settings).delete_device(user=user, device_id=device_id)
    return EmptyResponse(request_id=get_request_id())


@router.post("/proof-challenges", response_model=EgoBrowserProofChallengeResponse)
async def issue_ego_browser_proof_challenge(
    payload: EgoBrowserProofChallengeRequest,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[EgoBrowserPrincipal, Depends(get_ego_browser_principal)],
    relay_store: Annotated[EgoBrowserRelayStore, Depends(get_ego_browser_relay_store)],
) -> EgoBrowserProofChallengeResponse:
    """
    为一个明确的 Device Client 操作签发一次性 PoP challenge。

    :param payload (EgoBrowserProofChallengeRequest): PoP challenge 签发请求
    :param response (Response): 用于设置安全响应头的 HTTP 响应
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param principal (EgoBrowserPrincipal): 当前认证的用户或独立设备主体
    :param relay_store (EgoBrowserRelayStore): ego-browser 一次性票据与 PoP challenge 存储

    :return EgoBrowserProofChallengeResponse: 不含秘密材料的 PoP challenge 响应
    """

    issue = await EgoBrowserService(
        session, settings, relay_store=relay_store
    ).issue_proof_challenge(
        user=principal.user,
        payload=payload,
        authenticated_device=principal.device_auth.device if principal.device_auth else None,
    )
    response.headers["Cache-Control"] = "no-store"
    return EgoBrowserProofChallengeResponse(
        data=_proof_challenge_data(issue),
        request_id=get_request_id(),
    )


@router.get("/bindings", response_model=EgoBrowserBindingListResponse)
async def list_ego_browser_bindings(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_ego_browser_user_or_device_auth)],
    revocation_bus: Annotated[
        EgoBrowserRevocationPublisher,
        Depends(get_ego_browser_revocation_bus),
    ],
    all_users: bool = False,
) -> EgoBrowserBindingListResponse:
    """
    列出当前用户的 ego-browser binding 元数据。

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param user (User): 当前操作用户
    :param revocation_bus (EgoBrowserRevocationPublisher): ego-browser generation 撤销发布器
    :param all_users (bool): 是否包含其他用户拥有的记录

    :return EgoBrowserBindingListResponse: ego-browser binding 列表响应
    """

    bindings = await EgoBrowserService(
        session,
        settings,
        revocation_publisher=revocation_bus,
    ).list_bindings(user=user, all_users=all_users)
    items = [
        _binding_data(item, encryption_public_key=await _device_key(session, item))
        for item in bindings
    ]
    return EgoBrowserBindingListResponse(
        data=EgoBrowserBindingListData(items=items), request_id=get_request_id()
    )


@router.get("/bindings/candidates", response_model=EgoBrowserBindingCandidateListResponse)
async def list_ego_browser_candidates(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_ego_browser_user_or_device_auth)],
) -> EgoBrowserBindingCandidateListResponse:
    """
    列出供用户明确选择的远端 Claude session 候选。

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param user (User): 当前操作用户

    :return EgoBrowserBindingCandidateListResponse: 可认领的远端 session 候选列表响应
    """

    candidates = await EgoBrowserService(session, settings).list_candidates(user=user)
    data = [EgoBrowserBindingCandidateData.model_validate(item) for item in candidates]
    return EgoBrowserBindingCandidateListResponse(
        data=EgoBrowserBindingCandidateListData(items=data),
        request_id=get_request_id(),
    )


@router.post("/bindings/claim", response_model=EgoBrowserBindingResponse)
async def claim_ego_browser_binding(
    payload: EgoBrowserBindingClaimRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    device_auth: Annotated[EgoBrowserDeviceAuth, Depends(get_ego_browser_device_auth)],
    relay_store: Annotated[EgoBrowserRelayStore, Depends(get_ego_browser_relay_store)],
) -> EgoBrowserBindingResponse:
    """
    原子 claim 一个明确选择的远端 Claude session。

    :param payload (EgoBrowserBindingClaimRequest): binding 显式认领请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param device_auth (EgoBrowserDeviceAuth): 当前独立设备的认证上下文
    :param relay_store (EgoBrowserRelayStore): ego-browser 一次性票据与 PoP challenge 存储

    :return EgoBrowserBindingResponse: ego-browser binding 响应

    :raises ApiError: 独立设备凭据与请求选择的设备不一致
    """

    if payload.ego_browser_device_id != device_auth.device.id:
        raise ApiError(
            code="EGO_BROWSER_CREDENTIAL_DEVICE_MISMATCH",
            message="The device credential does not match the selected device.",
            status_code=403,
        )
    result = await EgoBrowserService(session, settings, relay_store=relay_store).claim(
        user=device_auth.user,
        payload=payload,
        device_id=device_auth.device.id,
    )
    return EgoBrowserBindingResponse(
        data=_binding_data(
            result.binding, encryption_public_key=device_auth.device.encryption_public_key
        ),
        request_id=get_request_id(),
    )


@router.get("/bindings/{binding_id}", response_model=EgoBrowserBindingResponse)
async def get_ego_browser_binding(
    binding_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_ego_browser_user_or_device_auth)],
) -> EgoBrowserBindingResponse:
    """
    读取当前用户拥有的 binding 元数据。

    :param binding_id (UUID): ego-browser binding 标识
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param user (User): 当前操作用户

    :return EgoBrowserBindingResponse: ego-browser binding 响应
    """

    binding = await EgoBrowserService(session, settings).get_binding(
        user=user,
        binding_id=binding_id,
    )
    return EgoBrowserBindingResponse(
        data=_binding_data(binding, encryption_public_key=await _device_key(session, binding)),
        request_id=get_request_id(),
    )


@router.get(
    "/bindings/{binding_id}/requests",
    response_model=EgoBrowserActiveRequestListResponse,
)
async def list_active_ego_browser_requests(
    binding_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    revocation_bus: Annotated[
        EgoBrowserRevocationPublisher,
        Depends(get_ego_browser_revocation_bus),
    ],
) -> EgoBrowserActiveRequestListResponse:
    """
    列出当前用户可取消的 active browser requests。

    :param binding_id (UUID): ego-browser binding 标识
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param user (User): 当前操作用户
    :param revocation_bus (EgoBrowserRevocationPublisher): ego-browser generation 撤销发布器

    :return EgoBrowserActiveRequestListResponse: 当前可取消请求列表响应
    """

    requests = await EgoBrowserService(
        session,
        settings,
        revocation_publisher=revocation_bus,
    ).list_active_requests(
        user=user,
        binding_id=binding_id,
    )
    return EgoBrowserActiveRequestListResponse(
        data=EgoBrowserActiveRequestListData(items=[_request_data(item) for item in requests]),
        request_id=get_request_id(),
    )


@router.post(
    "/bindings/{binding_id}/requests/{browser_request_id}/cancel",
    response_model=EgoBrowserCancelResponse,
)
async def cancel_ego_browser_request(
    binding_id: UUID,
    browser_request_id: str,
    payload: EgoBrowserCancelRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> EgoBrowserCancelResponse:
    """
    通过 Node 代理取消一个确切的活动浏览器请求。

    :param binding_id (UUID): ego-browser binding 标识
    :param browser_request_id (str): 待取消的浏览器请求 ID
    :param payload (EgoBrowserCancelRequest): 浏览器请求取消参数
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param user (User): 当前操作用户

    :return EgoBrowserCancelResponse: 浏览器请求取消状态响应
    """

    request = await EgoBrowserService(session, settings).cancel_request(
        user=user,
        binding_id=binding_id,
        request_id=browser_request_id,
        payload=payload,
    )
    return EgoBrowserCancelResponse(
        data=_request_data(request),
        request_id=get_request_id(),
    )


@router.post("/bindings/{binding_id}/connected", response_model=EgoBrowserBindingResponse)
async def mark_ego_browser_connected(
    binding_id: UUID,
    payload: EgoBrowserConnectedRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    device_auth: Annotated[EgoBrowserDeviceAuth, Depends(get_ego_browser_device_auth)],
    relay_store: Annotated[EgoBrowserRelayStore, Depends(get_ego_browser_relay_store)],
    revocation_bus: Annotated[
        EgoBrowserRevocationPublisher,
        Depends(get_ego_browser_revocation_bus),
    ],
) -> EgoBrowserBindingResponse:
    """
    确认本地 Bridge 能力并激活 binding。

    :param binding_id (UUID): ego-browser binding 标识
    :param payload (EgoBrowserConnectedRequest): Bridge 连接完成请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param device_auth (EgoBrowserDeviceAuth): 当前独立设备的认证上下文
    :param relay_store (EgoBrowserRelayStore): ego-browser 一次性票据与 PoP challenge 存储
    :param revocation_bus (EgoBrowserRevocationPublisher): ego-browser generation 撤销发布器

    :return EgoBrowserBindingResponse: ego-browser binding 响应
    """

    binding = await EgoBrowserService(
        session,
        settings,
        relay_store=relay_store,
        revocation_publisher=revocation_bus,
    ).connected(
        user=device_auth.user,
        binding_id=binding_id,
        payload=payload,
        device_id=device_auth.device.id,
    )
    return EgoBrowserBindingResponse(
        data=_binding_data(binding, encryption_public_key=device_auth.device.encryption_public_key),
        request_id=get_request_id(),
    )


@router.post("/bindings/{binding_id}/renew", response_model=EgoBrowserBindingResponse)
async def renew_ego_browser_binding(
    binding_id: UUID,
    payload: EgoBrowserRenewRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    device_auth: Annotated[EgoBrowserDeviceAuth, Depends(get_ego_browser_device_auth)],
    relay_store: Annotated[EgoBrowserRelayStore, Depends(get_ego_browser_relay_store)],
    revocation_bus: Annotated[
        EgoBrowserRevocationPublisher,
        Depends(get_ego_browser_revocation_bus),
    ],
) -> EgoBrowserBindingResponse:
    """
    续订当前代次的 ego-browser 绑定租约。

    :param binding_id (UUID): ego-browser binding 标识
    :param payload (EgoBrowserRenewRequest): Bridge 发起的 binding 续租请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param device_auth (EgoBrowserDeviceAuth): 当前独立设备的认证上下文
    :param relay_store (EgoBrowserRelayStore): ego-browser 一次性票据与 PoP challenge 存储
    :param revocation_bus (EgoBrowserRevocationPublisher): ego-browser generation 撤销发布器

    :return EgoBrowserBindingResponse: ego-browser binding 响应
    """

    binding = await EgoBrowserService(
        session,
        settings,
        relay_store=relay_store,
        revocation_publisher=revocation_bus,
    ).renew(
        user=device_auth.user,
        binding_id=binding_id,
        payload=payload,
        device_id=device_auth.device.id,
    )
    return EgoBrowserBindingResponse(
        data=_binding_data(binding, encryption_public_key=device_auth.device.encryption_public_key),
        request_id=get_request_id(),
    )


@router.post("/bindings/{binding_id}/pause", response_model=EgoBrowserBindingResponse)
async def pause_ego_browser_binding(
    binding_id: UUID,
    payload: EgoBrowserLifecycleRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[EgoBrowserPrincipal, Depends(get_ego_browser_principal)],
    relay_store: Annotated[EgoBrowserRelayStore, Depends(get_ego_browser_relay_store)],
    revocation_bus: Annotated[
        EgoBrowserRevocationPublisher,
        Depends(get_ego_browser_revocation_bus),
    ],
) -> EgoBrowserBindingResponse:
    """
    暂停绑定并撤销旧代次。

    :param binding_id (UUID): ego-browser binding 标识
    :param payload (EgoBrowserLifecycleRequest): binding 生命周期操作请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param principal (EgoBrowserPrincipal): 当前认证的用户或独立设备主体
    :param relay_store (EgoBrowserRelayStore): ego-browser 一次性票据与 PoP challenge 存储
    :param revocation_bus (EgoBrowserRevocationPublisher): ego-browser generation 撤销发布器

    :return EgoBrowserBindingResponse: ego-browser binding 响应
    """

    service = EgoBrowserService(
        session,
        settings,
        relay_store=relay_store,
        revocation_publisher=revocation_bus,
    )
    if principal.device_auth is None:
        binding = await service.pause_by_user(
            user=principal.user,
            binding_id=binding_id,
            generation=payload.generation,
        )
    else:
        binding = await service.pause(
            user=principal.user,
            binding_id=binding_id,
            payload=payload,
            device_id=principal.device_auth.device.id,
        )
    return EgoBrowserBindingResponse(
        data=_binding_data(binding, encryption_public_key=await _device_key(session, binding)),
        request_id=get_request_id(),
    )


@router.post("/bindings/{binding_id}/resume", response_model=EgoBrowserBindingResponse)
async def resume_ego_browser_binding(
    binding_id: UUID,
    payload: EgoBrowserResumeRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    device_auth: Annotated[EgoBrowserDeviceAuth, Depends(get_ego_browser_device_auth)],
    relay_store: Annotated[EgoBrowserRelayStore, Depends(get_ego_browser_relay_store)],
    revocation_bus: Annotated[
        EgoBrowserRevocationPublisher,
        Depends(get_ego_browser_revocation_bus),
    ],
) -> EgoBrowserBindingResponse:
    """
    在用户再次确认后恢复暂停的 binding。

    :param binding_id (UUID): ego-browser binding 标识
    :param payload (EgoBrowserResumeRequest): binding 恢复与用户确认请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param device_auth (EgoBrowserDeviceAuth): 当前独立设备的认证上下文
    :param relay_store (EgoBrowserRelayStore): ego-browser 一次性票据与 PoP challenge 存储
    :param revocation_bus (EgoBrowserRevocationPublisher): ego-browser generation 撤销发布器

    :return EgoBrowserBindingResponse: ego-browser binding 响应
    """

    binding = await EgoBrowserService(
        session,
        settings,
        relay_store=relay_store,
        revocation_publisher=revocation_bus,
    ).resume(
        user=device_auth.user,
        binding_id=binding_id,
        payload=payload,
        device_id=device_auth.device.id,
    )
    return EgoBrowserBindingResponse(
        data=_binding_data(binding, encryption_public_key=device_auth.device.encryption_public_key),
        request_id=get_request_id(),
    )


@router.post("/bindings/{binding_id}/stop", response_model=EgoBrowserBindingResponse)
async def stop_ego_browser_binding(
    binding_id: UUID,
    payload: EgoBrowserLifecycleRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[EgoBrowserPrincipal, Depends(get_ego_browser_principal)],
    relay_store: Annotated[EgoBrowserRelayStore, Depends(get_ego_browser_relay_store)],
    revocation_bus: Annotated[
        EgoBrowserRevocationPublisher,
        Depends(get_ego_browser_revocation_bus),
    ],
) -> EgoBrowserBindingResponse:
    """
    停止绑定并撤销当前中继代次。

    :param binding_id (UUID): ego-browser binding 标识
    :param payload (EgoBrowserLifecycleRequest): binding 生命周期操作请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param principal (EgoBrowserPrincipal): 当前认证的用户或独立设备主体
    :param relay_store (EgoBrowserRelayStore): ego-browser 一次性票据与 PoP challenge 存储
    :param revocation_bus (EgoBrowserRevocationPublisher): ego-browser generation 撤销发布器

    :return EgoBrowserBindingResponse: ego-browser binding 响应
    """

    service = EgoBrowserService(
        session,
        settings,
        relay_store=relay_store,
        revocation_publisher=revocation_bus,
    )
    if principal.device_auth is None:
        binding = await service.stop_by_user(
            user=principal.user,
            binding_id=binding_id,
            generation=payload.generation,
            reason=payload.reason,
        )
    else:
        binding = await service.stop(
            user=principal.user,
            binding_id=binding_id,
            payload=payload,
            device_id=principal.device_auth.device.id,
        )
    return EgoBrowserBindingResponse(
        data=_binding_data(binding, encryption_public_key=await _device_key(session, binding)),
        request_id=get_request_id(),
    )


@router.post("/bindings/{binding_id}/revoke", response_model=EgoBrowserBindingResponse)
async def revoke_ego_browser_binding(
    binding_id: UUID,
    payload: EgoBrowserLifecycleRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[EgoBrowserPrincipal, Depends(get_ego_browser_principal)],
    relay_store: Annotated[EgoBrowserRelayStore, Depends(get_ego_browser_relay_store)],
    revocation_bus: Annotated[
        EgoBrowserRevocationPublisher,
        Depends(get_ego_browser_revocation_bus),
    ],
) -> EgoBrowserBindingResponse:
    """
    永久撤销 binding。

    :param binding_id (UUID): ego-browser binding 标识
    :param payload (EgoBrowserLifecycleRequest): binding 生命周期操作请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param principal (EgoBrowserPrincipal): 当前认证的用户或独立设备主体
    :param relay_store (EgoBrowserRelayStore): ego-browser 一次性票据与 PoP challenge 存储
    :param revocation_bus (EgoBrowserRevocationPublisher): ego-browser generation 撤销发布器

    :return EgoBrowserBindingResponse: ego-browser binding 响应
    """

    service = EgoBrowserService(
        session,
        settings,
        relay_store=relay_store,
        revocation_publisher=revocation_bus,
    )
    if principal.device_auth is None:
        binding = await service.stop_by_user(
            user=principal.user,
            binding_id=binding_id,
            generation=payload.generation,
            reason=payload.reason,
            revoke=True,
        )
    else:
        binding = await service.stop(
            user=principal.user,
            binding_id=binding_id,
            payload=payload,
            revoke=True,
            device_id=principal.device_auth.device.id,
        )
    return EgoBrowserBindingResponse(
        data=_binding_data(binding, encryption_public_key=await _device_key(session, binding)),
        request_id=get_request_id(),
    )


@router.delete("/bindings/{binding_id}", response_model=EmptyResponse)
async def delete_ego_browser_binding(
    binding_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> EmptyResponse:
    """
    删除已终结且撤销通知已发布的 binding 历史。

    :param binding_id (UUID): ego-browser binding 标识
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param user (User): 当前用户或管理员

    :return EmptyResponse: 空响应
    """

    await EgoBrowserService(session, settings).delete_binding(user=user, binding_id=binding_id)
    return EmptyResponse(request_id=get_request_id())


@router.get("/bindings/{binding_id}/allowlist", response_model=EgoBrowserAllowlistResponse)
async def get_ego_browser_allowlist(
    binding_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_ego_browser_user_or_device_auth)],
) -> EgoBrowserAllowlistResponse:
    """
    读取绑定的文件允许列表元数据和修订版本。

    :param binding_id (UUID): ego-browser binding 标识
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param user (User): 当前操作用户

    :return EgoBrowserAllowlistResponse: binding 文件 allowlist 元数据响应
    """

    value = await EgoBrowserService(session, settings).get_allowlist(
        user=user,
        binding_id=binding_id,
    )
    return EgoBrowserAllowlistResponse(
        data=EgoBrowserAllowlistData.model_validate(value),
        request_id=get_request_id(),
    )


@router.post(
    "/bindings/{binding_id}/allowlist/confirm",
    response_model=EgoBrowserBindingResponse,
)
async def confirm_ego_browser_allowlist(
    binding_id: UUID,
    payload: EgoBrowserAllowlistConfirmRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    device_auth: Annotated[EgoBrowserDeviceAuth, Depends(get_ego_browser_device_auth)],
    relay_store: Annotated[EgoBrowserRelayStore, Depends(get_ego_browser_relay_store)],
    revocation_bus: Annotated[
        EgoBrowserRevocationPublisher,
        Depends(get_ego_browser_revocation_bus),
    ],
) -> EgoBrowserBindingResponse:
    """
    在用户确认新 revision 后更新 allowlist 元数据。

    :param binding_id (UUID): ego-browser binding 标识
    :param payload (EgoBrowserAllowlistConfirmRequest): allowlist 版本确认请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param device_auth (EgoBrowserDeviceAuth): 当前独立设备的认证上下文
    :param relay_store (EgoBrowserRelayStore): ego-browser 一次性票据与 PoP challenge 存储
    :param revocation_bus (EgoBrowserRevocationPublisher): ego-browser generation 撤销发布器

    :return EgoBrowserBindingResponse: ego-browser binding 响应
    """

    binding = await EgoBrowserService(
        session,
        settings,
        relay_store=relay_store,
        revocation_publisher=revocation_bus,
    ).confirm_allowlist(
        user=device_auth.user,
        binding_id=binding_id,
        payload=payload,
        device_id=device_auth.device.id,
    )
    return EgoBrowserBindingResponse(
        data=_binding_data(binding, encryption_public_key=device_auth.device.encryption_public_key),
        request_id=get_request_id(),
    )


@router.post(
    "/bindings/{binding_id}/relay-ticket",
    response_model=EgoBrowserRelayTicketResponse,
)
@router.post(
    "/bindings/{binding_id}/relay",
    response_model=EgoBrowserRelayTicketResponse,
)
async def issue_bridge_relay_ticket(
    binding_id: UUID,
    payload: EgoBrowserRelayTicketRequest,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    device_auth: Annotated[EgoBrowserDeviceAuth, Depends(get_ego_browser_device_auth)],
    relay_store: Annotated[EgoBrowserRelayStore, Depends(get_ego_browser_relay_store)],
) -> EgoBrowserRelayTicketResponse:
    """
    为本地 Bridge 签发一次性 relay ticket。

    :param binding_id (UUID): ego-browser binding 标识
    :param payload (EgoBrowserRelayTicketRequest): 一次性 relay 票据签发请求
    :param response (Response): 用于设置安全响应头的 HTTP 响应
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param device_auth (EgoBrowserDeviceAuth): 当前独立设备的认证上下文
    :param relay_store (EgoBrowserRelayStore): ego-browser 一次性票据与 PoP challenge 存储

    :return EgoBrowserRelayTicketResponse: 仅返回一次的 relay 票据响应
    """

    service = EgoBrowserService(session, settings, relay_store)
    result = await service.issue_relay_ticket(
        binding_id=binding_id,
        payload=payload,
        role="bridge",
        user=device_auth.user,
        device_id=device_auth.device.id,
    )
    response.headers["Cache-Control"] = "no-store"
    return EgoBrowserRelayTicketResponse(
        data=_ticket_data(result, binding_id),
        request_id=get_request_id(),
    )


@node_router.post(
    "/bindings/{binding_id}/relay-ticket",
    response_model=EgoBrowserRelayTicketResponse,
)
@node_router.post(
    "/bindings/{binding_id}/relay",
    response_model=EgoBrowserRelayTicketResponse,
)
async def issue_wrapper_relay_ticket(
    binding_id: UUID,
    payload: EgoBrowserRelayTicketRequest,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    node: Annotated[Node, Depends(get_current_node)],
    relay_store: Annotated[EgoBrowserRelayStore, Depends(get_ego_browser_relay_store)],
) -> EgoBrowserRelayTicketResponse:
    """
    为已认证 Node 包装器签发一次性中继票据。

    :param binding_id (UUID): ego-browser binding 标识
    :param payload (EgoBrowserRelayTicketRequest): 一次性 relay 票据签发请求
    :param response (Response): 用于设置安全响应头的 HTTP 响应
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param node (Node): 当前操作对应的节点
    :param relay_store (EgoBrowserRelayStore): ego-browser 一次性票据与 PoP challenge 存储

    :return EgoBrowserRelayTicketResponse: 仅返回一次的 relay 票据响应
    """

    service = EgoBrowserService(session, settings, relay_store)
    result = await service.issue_relay_ticket(
        binding_id=binding_id,
        payload=payload,
        role="wrapper",
        node=node,
    )
    response.headers["Cache-Control"] = "no-store"
    return EgoBrowserRelayTicketResponse(
        data=_ticket_data(result, binding_id),
        request_id=get_request_id(),
    )


@node_router.get("/bindings", response_model=EgoBrowserNodeBindingListResponse)
async def list_node_ego_browser_bindings(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    node: Annotated[Node, Depends(get_current_node)],
    revocation_bus: Annotated[
        EgoBrowserRevocationPublisher,
        Depends(get_ego_browser_revocation_bus),
    ],
) -> EgoBrowserNodeBindingListResponse:
    """
    列出当前 Node 承载的 ego-browser binding 非秘密元数据。

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param node (Node): 当前操作对应的节点
    :param revocation_bus (EgoBrowserRevocationPublisher): ego-browser generation 撤销发布器

    :return EgoBrowserNodeBindingListResponse: 当前 Node 的 live binding 列表响应
    """

    bindings = await EgoBrowserService(
        session,
        settings,
        revocation_publisher=revocation_bus,
    ).list_node_bindings(node=node)
    return EgoBrowserNodeBindingListResponse(
        data=EgoBrowserNodeBindingListData(
            items=[
                _node_binding_data(item, encryption_public_key=await _device_key(session, item))
                for item in bindings
            ]
        ),
        request_id=get_request_id(),
    )


@node_router.post(
    "/bindings/{binding_id}/renew",
    response_model=EgoBrowserNodeRenewResponse,
)
async def renew_node_ego_browser_binding(
    binding_id: UUID,
    payload: EgoBrowserNodeRenewRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    node: Annotated[Node, Depends(get_current_node)],
    revocation_bus: Annotated[
        EgoBrowserRevocationPublisher,
        Depends(get_ego_browser_revocation_bus),
    ],
) -> EgoBrowserNodeRenewResponse:
    """
    由当前已认证 Node 续租 ego-browser 绑定。

    :param binding_id (UUID): ego-browser binding 标识
    :param payload (EgoBrowserNodeRenewRequest): Node 发起的 binding 续租请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 异步数据库会话
    :param node (Node): 当前操作对应的节点
    :param revocation_bus (EgoBrowserRevocationPublisher): ego-browser generation 撤销发布器

    :return EgoBrowserNodeRenewResponse: Node 续租结果响应
    """

    binding = await EgoBrowserService(
        session,
        settings,
        revocation_publisher=revocation_bus,
    ).renew_for_node(
        node=node,
        binding_id=binding_id,
        payload=payload,
    )
    return EgoBrowserNodeRenewResponse(
        data=EgoBrowserNodeRenewData(
            binding_id=binding.id,
            generation=binding.generation,
            lease_until=_aware(binding.lease_until),
            lease_health=cast(EgoBrowserLeaseHealth, binding.lease_health),
            lease_grace_until=_aware(binding.lease_grace_until),
            absolute_ttl_until=_required_aware(binding.absolute_ttl_until),
        ),
        request_id=get_request_id(),
    )


@router.websocket("/bindings/{binding_id}/relay")
async def ego_browser_relay(binding_id: UUID, websocket: WebSocket) -> None:
    """
    消费一次性票据并转发通过准入校验的不透明外层帧。

    :param binding_id (UUID): ego-browser binding 标识
    :param websocket (WebSocket): 当前 relay WebSocket 连接
    """

    settings: Settings = websocket.app.state.settings
    if not settings.ego_browser_bridge_enabled:
        await websocket.close(code=1008)
        return
    authorization = websocket.headers.get("authorization", "")
    scheme, _, ticket = authorization.partition(" ")
    if scheme.lower() != "bearer" or not ticket:
        ticket = websocket.query_params.get("ticket", "")
    if not ticket:
        await websocket.close(code=1008)
        return
    relay_store: EgoBrowserRelayStore = websocket.app.state.ego_browser_relay_store
    claims = await relay_store.consume_ticket(
        token_hash=_hash_ticket(settings, ticket),
    )
    if claims is None or claims.binding.binding_id != binding_id:
        await websocket.close(code=1008)
        return
    session_factory = websocket.app.state.session_factory
    async with session_factory() as session:
        service = EgoBrowserService(session, settings)
        if not await service.relay_claims_are_current(claims):
            await websocket.close(code=1008)
            return
        hub: EgoBrowserRelayHub = websocket.app.state.ego_browser_relay_hub

        async def validate(
            current_claims: EgoBrowserRelayTicketClaims,
            _raw: bytes,
            envelope: dict[str, object],
        ) -> None:
            """
            校验单个 outer frame 的 binding、租约和重放状态。

            :param current_claims (EgoBrowserRelayTicketClaims): 一次性 relay 票据声明
            :param _raw (bytes): 原始外层信封字节；该回调只校验解析后的信封
            :param envelope (dict[str, object]): 已解析的外层信封元数据
            """

            await service.admit_outer_envelope(claims=current_claims, envelope=envelope)

        await hub.connect(claims, websocket, validate)


def _hash_ticket(settings: Settings, ticket: str) -> str:
    """计算一次性 relay ticket 的 keyed hash。"""

    from agent_remote_server.security import hash_token

    return hash_token(settings.secret_key, ticket)
