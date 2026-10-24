"""
实现身份业务逻辑。
"""

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.device_control.relay_hub import DeviceRelayHub
from agent_remote_server.ego_browser.relay import EgoBrowserRevocationPublisher
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    AuthToken,
    CliLoginCode,
    DeviceRegistrationRequest,
    NodeTask,
    SshKey,
    User,
    UserDevice,
    WireGuardPeer,
)
from agent_remote_server.repositories import (
    DeviceRegistrationRequestRepository,
    IdentityRepository,
    NodeRepository,
)
from agent_remote_server.repositories.cli_login_sessions import CliLoginSessionRepository
from agent_remote_server.repositories.device_sessions import DeviceSessionRepository
from agent_remote_server.security import (
    create_opaque_token,
    decrypt_text,
    encrypt_text,
    generate_totp_secret,
    hash_password,
    hash_token,
    verify_password,
    verify_totp_code,
)
from agent_remote_server.services.cli_login_sessions import CliLoginSessionService
from agent_remote_server.services.device_sessions import DeviceSessionService
from agent_remote_server.services.ego_browser import EgoBrowserService
from agent_remote_server.services.port_forward_revocation import revoke_port_forwards
from agent_remote_server.services.session_attachment import SessionAttachmentService
from agent_remote_server.services.ssh_keys import ssh_key_sync_payload, ssh_key_sync_task_id


@dataclass(frozen=True)
class TokenIssue:
    """
    新签发令牌
    """

    raw_token: str
    record: AuthToken
    expires_in: int


@dataclass(frozen=True)
class CliLoginStart:
    """
    CLI 登录启动结果
    """

    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class DeviceRegistrationResult:
    """
    设备注册结果
    """

    device: UserDevice
    ssh_key: SshKey
    wireguard_peer: WireGuardPeer | None
    token_issue: TokenIssue


class IdentityService:
    """
    身份认证和设备服务
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        relay_hub: DeviceRelayHub | None = None,
        ego_browser_revocation_publisher: EgoBrowserRevocationPublisher | None = None,
    ) -> None:
        """
        初始化身份业务服务。

        :param session (AsyncSession): 会话
        :param settings (Settings): 配置
        :param relay_hub (DeviceRelayHub | None): 中继中心
        :param ego_browser_revocation_publisher (EgoBrowserRevocationPublisher | None): 撤销发布器
        """
        self._session = session
        self._settings = settings
        self._repository = IdentityRepository(session)
        self._node_repository = NodeRepository(session)
        self._relay_hub = relay_hub
        self._ego_browser_revocation_publisher = ego_browser_revocation_publisher

    async def bootstrap_required(self) -> bool:
        """
        判断系统是否需要创建首个管理员

        :return bool: 是否需要初始化
        """

        return not await self._repository.has_users()

    async def bootstrap_admin(
        self,
        *,
        username: str,
        password: str,
        display_name: str | None,
    ) -> TokenIssue:
        """
        初始化第一个管理员

        :param username (str): 用户名
        :param password (str): 明文密码
        :param display_name (str | None): 显示名
        :return TokenIssue: 管理员登录令牌
        :raises ApiError: 系统已经完成首个管理员初始化
        """

        if await self._repository.has_users():
            raise ApiError(
                code="COMMON_CONFLICT",
                message="System has already been bootstrapped.",
                status_code=409,
            )

        user = await self._repository.add_user(
            User(
                username=username,
                display_name=display_name or username,
                role="admin",
                status="active",
                password_hash=hash_password(password),
                totp_enabled=False,
            )
        )
        token_issue = await self._issue_token(user=user, device=None, token_type="user")
        await self._audit(
            actor_user_id=user.id,
            action="auth.bootstrap_admin",
            target_type="user",
            target_id=str(user.id),
            details={"username": username},
        )
        await self._session.commit()
        return token_issue

    async def login(
        self,
        *,
        username: str,
        password: str,
        totp_code: str | None,
    ) -> TokenIssue:
        """
        用户名密码登录

        :param username (str): 用户名
        :param password (str): 明文密码
        :param totp_code (str | None): TOTP 验证码
        :return TokenIssue: 登录令牌
        :raises ApiError: 用户凭据无效或仍需有效的 TOTP 验证码
        """

        user = await self._repository.get_user_by_username(username)
        if (
            user is None
            or user.status != "active"
            or not verify_password(password, user.password_hash)
        ):
            raise ApiError(
                code="AUTH_INVALID_CREDENTIALS",
                message="Invalid username, password, or TOTP code.",
                status_code=401,
            )

        if user.totp_enabled:
            if not totp_code:
                raise ApiError(
                    code="AUTH_TOTP_REQUIRED",
                    message="TOTP verification is required.",
                    status_code=401,
                )
            if user.encrypted_totp_secret is None:
                raise ApiError(
                    code="AUTH_INVALID_CREDENTIALS",
                    message="Invalid username, password, or TOTP code.",
                    status_code=401,
                )
            secret = decrypt_text(self._settings.secret_key, user.encrypted_totp_secret)
            if not verify_totp_code(secret, totp_code):
                raise ApiError(
                    code="AUTH_INVALID_CREDENTIALS",
                    message="Invalid username, password, or TOTP code.",
                    status_code=401,
                )

        token_issue = await self._issue_token(user=user, device=None, token_type="user")
        await self._audit(
            actor_user_id=user.id,
            action="auth.login",
            target_type="user",
            target_id=str(user.id),
            details={"username": username},
        )
        await self._session.commit()
        return token_issue

    async def logout(self, token: AuthToken) -> None:
        """
        注销当前令牌

        :param token (AuthToken): 当前令牌
        """

        await CliLoginSessionService(self._session, self._settings).revoke(token)
        locked = await CliLoginSessionRepository(self._session).lock_token(token.id)
        if locked is not None:
            self._revoke_token(locked)
        await self._audit(
            actor_user_id=token.user_id,
            action="auth.logout",
            target_type="auth_token",
            target_id=str(token.id),
            details={"token_type": token.token_type},
        )
        await self._session.commit()

    async def refresh_token(self, token: AuthToken) -> TokenIssue:
        """
        刷新当前令牌

        :param token (AuthToken): 当前令牌
        :return TokenIssue: 新令牌
        """

        repository = CliLoginSessionRepository(self._session)
        locked = await repository.lock_token(token.id)
        if locked is None or locked.status != "active":
            raise ApiError(
                code="AUTH_TOKEN_REVOKED", message="Token has been revoked.", status_code=401
            )
        if locked.cli_session_id is not None:
            raise ApiError(
                code="AUTH_CLI_REFRESH_REQUIRED",
                message="Use the CLI session refresh credential.",
                status_code=401,
            )
        user = await self._require_user(token.user_id)
        device = (
            await self._repository.get_device(token.user_device_id)
            if token.user_device_id
            else None
        )
        self._revoke_token(token)
        token_issue = await self._issue_token(user=user, device=device, token_type=token.token_type)
        await self._audit(
            actor_user_id=user.id,
            action="auth.refresh",
            target_type="auth_token",
            target_id=str(token.id),
            details={"token_type": token.token_type},
        )
        await self._session.commit()
        return token_issue

    async def start_cli_login(self, *, completion_request_id: UUID | None = None) -> CliLoginStart:
        """
        启动 CLI 设备码登录

        :param completion_request_id (UUID | None): 可选的完成回执恢复键
        :return CliLoginStart: 登录码信息
        """

        if completion_request_id is not None:
            existing = await self._repository.get_cli_login_by_completion_request_id(
                completion_request_id
            )
            if existing is not None:
                raise ApiError(
                    code="AUTH_CLI_COMPLETION_KEY_REUSED",
                    message="The CLI completion key was already used.",
                    status_code=409,
                )
        device_code = create_opaque_token("devcode")
        user_code = self._create_user_code()
        expires_in = self._settings.cli_login_ttl_seconds
        interval = self._settings.cli_login_poll_interval_seconds
        await self._repository.add_cli_login_code(
            CliLoginCode(
                device_code_hash=hash_token(self._settings.secret_key, device_code),
                user_code=user_code,
                status="pending",
                expires_at=self._now() + timedelta(seconds=expires_in),
                interval_seconds=interval,
                completion_request_id=completion_request_id,
            )
        )
        await self._session.commit()
        return CliLoginStart(
            device_code=device_code,
            user_code=user_code,
            verification_url=(
                f"{self._settings.public_base_url.rstrip('/')}/cli?{urlencode({'code': user_code})}"
            ),
            expires_in=expires_in,
            interval=interval,
        )

    async def approve_cli_login(self, *, user: User, user_code: str) -> None:
        """
        管理端确认 CLI 登录

        :param user (User): 当前用户
        :param user_code (str): 用户确认码
        :raises ApiError: CLI 登录码不存在或不再可确认
        """

        cli_code = await self._repository.get_cli_login_by_user_code(user_code)
        if cli_code is None:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="CLI login code was not found.", status_code=404
            )
        self._ensure_cli_code_pending(cli_code)
        cli_code.status = "approved"
        cli_code.approved_user_id = user.id
        await self._audit(
            actor_user_id=user.id,
            action="auth.cli_approve",
            target_type="cli_login_code",
            target_id=str(cli_code.id),
            details={"user_code": user_code},
        )
        await self._session.commit()

    async def complete_cli_login(
        self, *, device_code: str, completion_request_id: UUID | None = None
    ) -> TokenIssue:
        """
        CLI 轮询完成登录

        :param device_code (str): 设备代码
        :param completion_request_id (UUID | None): 原始完成回执恢复键
        :return TokenIssue: 登录令牌
        :raises ApiError: CLI 登录码不存在、已过期、未获批准或已不可使用
        """

        cli_code = await self._repository.lock_cli_login_by_device_hash(
            hash_token(self._settings.secret_key, device_code)
        )
        if cli_code is None:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="CLI login code was not found.", status_code=404
            )
        if cli_code.completion_request_id != completion_request_id:
            raise ApiError(
                code="AUTH_CLI_COMPLETION_KEY_MISMATCH",
                message="The CLI completion key does not match the original request.",
                status_code=409,
            )
        if cli_code.status == "consumed":
            return await self._recover_cli_login_completion(cli_code)
        if self._is_expired(cli_code.expires_at):
            cli_code.status = "expired"
            await self._session.commit()
            raise ApiError(
                code="AUTH_TOKEN_EXPIRED", message="CLI login code has expired.", status_code=401
            )
        if cli_code.status == "pending":
            raise ApiError(
                code="COMMON_BAD_REQUEST", message="CLI login is not approved.", status_code=400
            )
        if cli_code.status != "approved" or cli_code.approved_user_id is None:
            raise ApiError(
                code="COMMON_CONFLICT", message="CLI login code is not usable.", status_code=409
            )

        user = await self._require_user(cli_code.approved_user_id)
        token_issue = await self._issue_token(user=user, device=None, token_type="user")
        cli_code.status = "consumed"
        cli_code.consumed_at = self._now()
        if cli_code.completion_request_id is not None:
            cli_code.encrypted_completion_receipt = encrypt_text(
                self._settings.secret_key,
                json.dumps(
                    {
                        "access_token": token_issue.raw_token,
                        "token_type": "bearer",
                        "expires_in": token_issue.expires_in,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            cli_code.completion_receipt_expires_at = token_issue.record.expires_at
        await self._audit(
            actor_user_id=user.id,
            action="auth.cli_complete",
            target_type="cli_login_code",
            target_id=str(cli_code.id),
            details={"status": "consumed"},
        )
        await self._session.commit()
        return token_issue

    async def recover_cli_login(self, *, completion_request_id: UUID) -> TokenIssue:
        """
        按原始键恢复一个已完成的设备码结果。

        :param completion_request_id (UUID): 原始完成回执键
        :return TokenIssue: 登录令牌
        """

        cli_code = await self._repository.get_cli_login_by_completion_request_id(
            completion_request_id
        )
        if cli_code is None:
            raise ApiError(
                code="AUTH_CLI_COMPLETION_NOT_FOUND",
                message="CLI login completion was not found.",
                status_code=404,
            )
        if cli_code.status != "consumed":
            if self._is_expired(cli_code.expires_at):
                cli_code.status = "expired"
                await self._session.commit()
                raise ApiError(
                    code="AUTH_TOKEN_EXPIRED",
                    message="CLI login code has expired.",
                    status_code=401,
                )
            raise ApiError(
                code="COMMON_BAD_REQUEST",
                message="CLI login is not approved.",
                status_code=400,
            )
        return await self._recover_cli_login_completion(cli_code)

    async def _recover_cli_login_completion(self, cli_code: CliLoginCode) -> TokenIssue:
        """
        解密并校验登录回执，任何无效状态都安全失败。

        :param cli_code (CliLoginCode): 登录码实体
        :return TokenIssue: 回执中的登录令牌
        """

        if (
            cli_code.completion_request_id is None
            or cli_code.encrypted_completion_receipt is None
            or cli_code.completion_receipt_expires_at is None
            or self._is_expired(cli_code.completion_receipt_expires_at)
        ):
            raise ApiError(
                code="UNKNOWN_RESULT",
                message="CLI login completion cannot be recovered.",
                status_code=503,
            )
        try:
            receipt = json.loads(
                decrypt_text(self._settings.secret_key, cli_code.encrypted_completion_receipt)
            )
            raw_token = receipt["access_token"]
            expires_in = int(receipt["expires_in"])
            if receipt.get("token_type") != "bearer" or not isinstance(raw_token, str):
                raise ValueError("invalid receipt")
        except Exception as exc:
            raise ApiError(
                code="UNKNOWN_RESULT",
                message="CLI login completion cannot be recovered.",
                status_code=503,
            ) from exc
        token = await self._repository.get_token_by_hash(
            hash_token(self._settings.secret_key, raw_token)
        )
        if (
            token is None
            or token.token_type != "user"
            or token.status != "active"
            or self._is_expired(token.expires_at)
        ):
            raise ApiError(
                code="UNKNOWN_RESULT",
                message="CLI login completion token is no longer recoverable.",
                status_code=503,
            )
        return TokenIssue(raw_token=raw_token, record=token, expires_in=expires_in)

    async def create_user(
        self,
        *,
        actor: User,
        username: str,
        password: str,
        role: str,
        display_name: str | None,
    ) -> User:
        """
        创建用户

        :param actor (User): 操作人
        :param username (str): 用户名
        :param password (str): 明文密码
        :param role (str): 角色
        :param display_name (str | None): 显示名
        :return User: 新用户
        """

        await self._ensure_username_available(username)
        user = await self._repository.add_user(
            User(
                username=username,
                display_name=display_name or username,
                role=role,
                status="active",
                password_hash=hash_password(password),
                totp_enabled=False,
            )
        )
        await self._audit(
            actor_user_id=actor.id,
            action="users.create",
            target_type="user",
            target_id=str(user.id),
            details={"username": username, "role": role},
        )
        await self._session.commit()
        return user

    async def update_user(
        self,
        *,
        actor: User,
        user_id: UUID,
        display_name: str | None,
        status: str | None,
    ) -> User:
        """
        更新用户

        :param actor (User): 操作人
        :param user_id (UUID): 用户 ID
        :param display_name (str | None): 显示名
        :param status (str | None): 用户状态
        :return User: 更新后的用户
        """

        user = await self._require_user(user_id)
        ego_browser_service = EgoBrowserService(
            self._session,
            self._settings,
            revocation_publisher=self._ego_browser_revocation_publisher,
        )
        if display_name is not None:
            user.display_name = display_name
        if status is not None:
            user.status = status
            if status != "active":
                await ego_browser_service.revoke_for_user(
                    user_id=user.id,
                    reason="user_disabled",
                    commit=False,
                    publish=False,
                )
                await revoke_port_forwards(
                    self._session,
                    reason="user_revoked",
                    actor_user_id=actor.id,
                    user_id=user.id,
                )
        await self._audit(
            actor_user_id=actor.id,
            action="users.update",
            target_type="user",
            target_id=str(user.id),
            details={"status": status} if status else {},
        )
        await self._session.commit()
        await ego_browser_service.publish_pending_revocations()
        return user

    async def disable_user(self, *, actor: User, user_id: UUID) -> User:
        """
        禁用用户

        :param actor (User): 操作人
        :param user_id (UUID): 用户 ID
        :return User: 禁用后的用户
        """

        user = await self._require_user(user_id)
        user.status = "disabled"
        ego_browser_service = EgoBrowserService(
            self._session,
            self._settings,
            revocation_publisher=self._ego_browser_revocation_publisher,
        )
        await ego_browser_service.revoke_for_user(
            user_id=user.id,
            reason="user_disabled",
            commit=False,
            publish=False,
        )
        await revoke_port_forwards(
            self._session,
            reason="user_revoked",
            actor_user_id=actor.id,
            user_id=user.id,
        )
        await self._audit(
            actor_user_id=actor.id,
            action="users.disable",
            target_type="user",
            target_id=str(user.id),
            details={},
        )
        await self._session.commit()
        await ego_browser_service.publish_pending_revocations()
        return user

    async def setup_totp(self, *, user: User) -> str:
        """
        创建并保存 TOTP 密钥

        :param user (User): 当前用户
        :return str: 明文密钥
        """

        secret = generate_totp_secret()
        user.encrypted_totp_secret = encrypt_text(self._settings.secret_key, secret)
        user.totp_enabled = False
        await self._audit(
            actor_user_id=user.id,
            action="auth.totp_setup",
            target_type="user",
            target_id=str(user.id),
            details={"enabled": False},
        )
        await self._session.commit()
        return secret

    async def verify_totp(self, *, user: User, code: str) -> None:
        """
        验证并启用 TOTP

        :param user (User): 当前用户
        :param code (str): TOTP 验证码
        :raises ApiError: TOTP 尚未设置或验证码无效
        """

        if user.encrypted_totp_secret is None:
            raise ApiError(
                code="COMMON_BAD_REQUEST", message="TOTP has not been set up.", status_code=400
            )
        secret = decrypt_text(self._settings.secret_key, user.encrypted_totp_secret)
        if not verify_totp_code(secret, code):
            raise ApiError(
                code="AUTH_INVALID_CREDENTIALS",
                message="Invalid username, password, or TOTP code.",
                status_code=401,
            )
        user.totp_enabled = True
        await self._audit(
            actor_user_id=user.id,
            action="auth.totp_verify",
            target_type="user",
            target_id=str(user.id),
            details={"enabled": True},
        )
        await self._session.commit()

    async def register_device(
        self,
        *,
        user: User,
        name: str,
        platform: str,
        cli_version: str | None,
        ssh_public_key: str,
        wireguard_public_key: str | None,
        existing_device_id: UUID | None = None,
        request_id: UUID | None = None,
    ) -> DeviceRegistrationResult:
        """
        注册用户设备

        :param user (User): 当前用户
        :param name (str): 设备名称
        :param platform (str): 设备平台
        :param cli_version (str | None): agent-remote CLI 版本
        :param ssh_public_key (str): SSH 公钥
        :param wireguard_public_key (str | None): WireGuard 公钥
        :param existing_device_id (UUID | None): 需要复用的现有设备 ID
        :param request_id (UUID | None): 调用方幂等请求标识
        :return DeviceRegistrationResult: 注册结果
        :raises ApiError: 指定的复用设备不存在或未处于启用状态
        """

        request_repository = DeviceRegistrationRequestRepository(self._session)
        intent_fingerprint = self._device_registration_fingerprint(
            name=name,
            platform=platform,
            cli_version=cli_version,
            ssh_public_key=ssh_public_key,
            wireguard_public_key=wireguard_public_key,
            existing_device_id=existing_device_id,
        )
        if request_id is not None:
            previous = await request_repository.get_for_update(user.id, request_id)
            if previous is not None:
                if previous.intent_fingerprint != intent_fingerprint:
                    raise ApiError(
                        code="DEVICE_REGISTRATION_KEY_REUSED",
                        message="The device registration key was already used for another intent.",
                        status_code=409,
                    )
                return await self._recover_device_registration(previous, user=user)

        reused = existing_device_id is not None
        if existing_device_id is None:
            device = await self._repository.add_device(
                UserDevice(
                    user_id=user.id,
                    name=name,
                    platform=platform,
                    cli_version=cli_version,
                    status="active",
                    last_seen_at=self._now(),
                )
            )
        else:
            existing_device = await self._repository.get_device(existing_device_id)
            if existing_device is None or existing_device.user_id != user.id:
                raise ApiError(
                    code="COMMON_NOT_FOUND", message="Device was not found.", status_code=404
                )
            if existing_device.status != "active":
                raise ApiError(
                    code="DEVICE_REUSE_REQUIRES_ACTIVE",
                    message=(
                        "The existing device is not active. Remove the local login before "
                        "registering a new device."
                    ),
                    status_code=409,
                )
            device = existing_device
            device.name = name
            device.platform = platform
            device.cli_version = cli_version
            device.last_seen_at = self._now()

        fingerprint = self._fingerprint(ssh_public_key)
        ssh_keys = list(await self._repository.list_ssh_keys_for_device(device.id))
        ssh_key = next(
            (
                item
                for item in ssh_keys
                if item.status == "active" and item.fingerprint == fingerprint
            ),
            None,
        )
        if ssh_key is None:
            ssh_key = await self._repository.add_ssh_key(
                SshKey(
                    user_device_id=device.id,
                    public_key=ssh_public_key,
                    fingerprint=fingerprint,
                    status="active",
                )
            )

        active_peers = [
            peer
            for peer in await self._repository.list_wireguard_peers_for_device(device.id)
            if peer.status == "active"
        ]
        wireguard_peer = active_peers[0] if active_peers else None
        if wireguard_peer is not None and wireguard_public_key:
            wireguard_peer.public_key = wireguard_public_key
        elif wireguard_peer is None and wireguard_public_key:
            wireguard_peer = await self._repository.add_wireguard_peer(
                WireGuardPeer(
                    peer_type="device",
                    user_device_id=device.id,
                    node_id=None,
                    public_key=wireguard_public_key,
                    encrypted_private_key=None,
                    ip_address=await self._next_wireguard_ip(),
                    status="active",
                )
            )
        if reused:
            for token in await self._repository.list_tokens_for_device(device.id):
                self._revoke_token(token)
        token_issue = await self._issue_token(user=user, device=device, token_type="device")
        if request_id is not None:
            await request_repository.add(
                DeviceRegistrationRequest(
                    user_id=user.id,
                    request_id=request_id,
                    intent_fingerprint=intent_fingerprint,
                    device_id=device.id,
                    ssh_key_id=ssh_key.id,
                    wireguard_peer_id=wireguard_peer.id if wireguard_peer else None,
                    receipt_json={
                        "name": device.name,
                        "platform": device.platform,
                        "cli_version": device.cli_version,
                        "status": device.status,
                        "created_at": device.created_at.isoformat(),
                        "last_seen_at": (
                            device.last_seen_at.isoformat() if device.last_seen_at else None
                        ),
                    },
                    encrypted_device_token=encrypt_text(
                        self._settings.secret_key, token_issue.raw_token
                    ),
                    token_expires_at=token_issue.record.expires_at,
                    result_expires_at=token_issue.record.expires_at,
                    phase="completed",
                )
            )
        await self._audit(
            actor_user_id=user.id,
            action="devices.login" if reused else "devices.register",
            target_type="user_device",
            target_id=str(device.id),
            details={
                "platform": platform,
                "cli_version": cli_version,
                "ssh_key_id": str(ssh_key.id),
                "wireguard_peer_id": str(wireguard_peer.id) if wireguard_peer else None,
                "reused": reused,
            },
        )
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            if request_id is not None:
                previous = await request_repository.get(user.id, request_id)
                if previous is not None:
                    if previous.intent_fingerprint != intent_fingerprint:
                        raise ApiError(
                            code="DEVICE_REGISTRATION_KEY_REUSED",
                            message=(
                                "The device registration key was already used for another intent."
                            ),
                            status_code=409,
                        ) from None
                    return await self._recover_device_registration(previous, user=user)
            raise
        return DeviceRegistrationResult(
            device=device,
            ssh_key=ssh_key,
            wireguard_peer=wireguard_peer,
            token_issue=token_issue,
        )

    async def _recover_device_registration(
        self, record: DeviceRegistrationRequest, *, user: User
    ) -> DeviceRegistrationResult:
        """
        从短期加密回执恢复第一次设备登记结果。

        :param record (DeviceRegistrationRequest): 原键登记记录
        :param user (User): 当前用户
        :return DeviceRegistrationResult: 原始登记结果
        :raises ApiError: 回执已过期、损坏或令牌不再有效
        """

        if (
            record.phase != "completed"
            or record.device_id is None
            or record.ssh_key_id is None
            or record.encrypted_device_token is None
            or record.token_expires_at is None
            or record.result_expires_at is None
        ):
            raise ApiError(
                code="UNKNOWN_RESULT",
                message="The device registration result cannot be recovered.",
                status_code=503,
            )
        if self._is_expired(record.result_expires_at):
            raise ApiError(
                code="DEVICE_REGISTRATION_RECOVERY_EXPIRED",
                message="The device registration recovery window has expired.",
                status_code=410,
            )
        try:
            raw_token = decrypt_text(self._settings.secret_key, record.encrypted_device_token)
        except Exception as exc:
            raise ApiError(
                code="UNKNOWN_RESULT",
                message="The device registration result cannot be recovered.",
                status_code=503,
            ) from exc
        token = await self._repository.get_token_by_hash(
            hash_token(self._settings.secret_key, raw_token)
        )
        if (
            token is None
            or token.user_id != user.id
            or token.user_device_id != record.device_id
            or token.status != "active"
            or self._is_expired(token.expires_at)
        ):
            raise ApiError(
                code="UNKNOWN_RESULT",
                message="The device registration token is no longer recoverable.",
                status_code=503,
            )
        device = await self._repository.get_device(record.device_id)
        if device is None or device.user_id != user.id:
            raise ApiError(
                code="UNKNOWN_RESULT",
                message="The registered device is no longer recoverable.",
                status_code=503,
            )
        ssh_key = next(
            (
                item
                for item in await self._repository.list_ssh_keys_for_device(device.id)
                if item.id == record.ssh_key_id
            ),
            None,
        )
        if ssh_key is None:
            raise ApiError(
                code="UNKNOWN_RESULT",
                message="The registered SSH key is no longer recoverable.",
                status_code=503,
            )
        wireguard_peer = None
        if record.wireguard_peer_id is not None:
            wireguard_peer = next(
                (
                    item
                    for item in await self._repository.list_wireguard_peers_for_device(device.id)
                    if item.id == record.wireguard_peer_id
                ),
                None,
            )
            if wireguard_peer is None:
                raise ApiError(
                    code="UNKNOWN_RESULT",
                    message="The registered WireGuard peer is no longer recoverable.",
                    status_code=503,
                )
        token_expires_at = (
            token.expires_at
            if token.expires_at.tzinfo is not None
            else token.expires_at.replace(tzinfo=UTC)
        )
        expires_in = max(1, int((token_expires_at - self._now()).total_seconds()))
        return DeviceRegistrationResult(
            device=device,
            ssh_key=ssh_key,
            wireguard_peer=wireguard_peer,
            token_issue=TokenIssue(raw_token=raw_token, record=token, expires_in=expires_in),
        )

    @staticmethod
    def _device_registration_fingerprint(
        *,
        name: str,
        platform: str,
        cli_version: str | None,
        ssh_public_key: str,
        wireguard_public_key: str | None,
        existing_device_id: UUID | None,
    ) -> str:
        """
        计算不含密钥明文的设备登记意图摘要。

        :param name (str): 设备名称
        :param platform (str): 设备平台
        :param cli_version (str | None): CLI 版本
        :param ssh_public_key (str): SSH 公钥
        :param wireguard_public_key (str | None): WireGuard 公钥
        :param existing_device_id (UUID | None): 复用设备标识
        :return str: 固定格式的 SHA-256 摘要
        """

        value = json.dumps(
            {
                "name": name,
                "platform": platform,
                "cli_version": cli_version,
                "ssh_public_key": ssh_public_key,
                "wireguard_public_key": wireguard_public_key,
                "existing_device_id": str(existing_device_id) if existing_device_id else None,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"

    async def revoke_device(self, *, actor: User, device_id: UUID) -> UserDevice:
        """
        撤销设备和关联凭证

        :param actor (User): 操作人
        :param device_id (UUID): 设备 ID
        :return UserDevice: 撤销后的设备
        """

        device = await self._require_visible_device(actor=actor, device_id=device_id)
        device_stop = await DeviceSessionService(
            self._session, self._settings, self._relay_hub
        ).stop_for_device(
            device_id=device.id,
            reason="device_revoked",
            actor_user_id=actor.id,
            audit_action="device_session.device_revoke",
            commit=False,
        )
        ego_browser_service = EgoBrowserService(
            self._session,
            self._settings,
            revocation_publisher=self._ego_browser_revocation_publisher,
        )
        # 两类设备身份彼此独立；用户凭据撤销必须同步收回 live browser binding。
        await ego_browser_service.revoke_for_user(
            user_id=device.user_id,
            reason="user_device_revoked",
            commit=False,
            publish=False,
        )
        revoked_at = self._now()
        await SessionAttachmentService(self._session).invalidate_for_device(
            device.id, "device_revoked"
        )
        device.status = "revoked"
        for ssh_key in await self._repository.list_ssh_keys_for_device(device.id):
            ssh_key.status = "revoked"
            ssh_key.revoked_at = revoked_at
        for peer in await self._repository.list_wireguard_peers_for_device(device.id):
            peer.status = "revoked"
            peer.revoked_at = revoked_at
        for token in await self._repository.list_tokens_for_device(device.id):
            self._revoke_token(token)
        for node in await self._node_repository.list_nodes():
            task_id = ssh_key_sync_task_id(node_id=node.id, device_id=device.id, ssh_keys=[])
            if await self._node_repository.get_task_by_task_id(task_id) is None:
                await self._node_repository.add_task(
                    NodeTask(
                        node_id=node.id,
                        task_id=task_id,
                        task_type="sync_ssh_keys",
                        status="pending",
                        payload=ssh_key_sync_payload(
                            device_id=device.id,
                            ssh_user=node.ssh_user or "agent-remote",
                            ssh_keys=[],
                        ),
                        retry_count=0,
                    )
                )
        await revoke_port_forwards(
            self._session,
            reason="device_revoked",
            actor_user_id=actor.id,
            device_id=device.id,
        )
        await self._audit(
            actor_user_id=actor.id,
            action="devices.revoke",
            target_type="user_device",
            target_id=str(device.id),
            details={"device_user_id": str(device.user_id)},
        )
        await self._session.commit()
        await DeviceSessionService(
            self._session, self._settings, self._relay_hub
        ).close_revoked_bindings(device_stop.revoked_bindings)
        await ego_browser_service.publish_pending_revocations()
        return device

    async def delete_device(self, *, actor: User, device_id: UUID) -> None:
        """
        删除已撤销且未关联 workspace 的设备

        :param actor (User): 操作人
        :param device_id (UUID): 设备 ID
        :raises ApiError: 设备未撤销或仍有关联工作区与控制历史
        """

        device = await self._require_visible_device(actor=actor, device_id=device_id)
        if device.status != "revoked":
            raise ApiError(
                code="DEVICE_DELETE_REQUIRES_REVOKED",
                message="Revoke the device before deleting it.",
                status_code=409,
            )
        if await self._repository.has_workspaces_for_device(device.id):
            raise ApiError(
                code="DEVICE_DELETE_BLOCKED",
                message="Delete the device workspaces before deleting the device.",
                status_code=409,
            )
        if await DeviceSessionRepository(self._session).has_any_for_device(device.id):
            raise ApiError(
                code="DEVICE_DELETE_BINDING_HISTORY",
                message="Device control history must expire before deleting this device.",
                status_code=409,
            )
        await self._audit(
            actor_user_id=actor.id,
            action="devices.delete",
            target_type="user_device",
            target_id=str(device.id),
            details={"device_user_id": str(device.user_id)},
        )
        await self._repository.delete_device(device)
        await self._session.commit()

    async def rotate_device_token(self, *, actor: User, device_id: UUID) -> TokenIssue:
        """
        轮换设备令牌

        :param actor (User): 操作人
        :param device_id (UUID): 设备 ID
        :return TokenIssue: 新设备令牌
        :raises ApiError: 设备已经撤销
        """

        device = await self._require_visible_device(actor=actor, device_id=device_id)
        if device.status != "active":
            raise ApiError(
                code="DEVICE_REVOKED", message="Device has been revoked.", status_code=403
            )
        for token in await self._repository.list_tokens_for_device(device.id):
            self._revoke_token(token)
        user = await self._require_user(device.user_id)
        token_issue = await self._issue_token(user=user, device=device, token_type="device")
        await self._audit(
            actor_user_id=actor.id,
            action="devices.rotate_token",
            target_type="user_device",
            target_id=str(device.id),
            details={},
        )
        await self._session.commit()
        return token_issue

    async def list_users(self) -> list[User]:
        """
        列出所有用户

        :return list[User]: 用户列表
        """

        return list(await self._repository.list_users())

    async def list_devices(self, *, user: User) -> list[UserDevice]:
        """
        列出当前用户设备

        :param user (User): 当前用户
        :return list[UserDevice]: 设备列表
        """

        return list(await self._repository.list_devices_for_user(user.id))

    async def get_visible_device(self, *, actor: User, device_id: UUID) -> UserDevice:
        """
        读取当前用户可见设备

        :param actor (User): 操作人
        :param device_id (UUID): 设备 ID
        :return UserDevice: 设备实体
        """

        return await self._require_visible_device(actor=actor, device_id=device_id)

    async def recover_device_registration(
        self, *, user: User, request_id: UUID
    ) -> DeviceRegistrationResult:
        """
        读取设备登记原键的短期回执，不发起新登记或令牌轮换。

        :param user (User): 当前用户
        :param request_id (UUID): 调用方原键
        :return DeviceRegistrationResult: 原始登记结果
        :raises ApiError: 原键不存在或回执不可恢复
        """

        record = await DeviceRegistrationRequestRepository(self._session).get(user.id, request_id)
        if record is None:
            raise ApiError(
                code="DEVICE_REGISTRATION_REQUEST_NOT_FOUND",
                message="Device registration request was not found.",
                status_code=404,
            )
        return await self._recover_device_registration(record, user=user)

    async def get_user(self, user_id: UUID) -> User:
        """
        读取用户

        :param user_id (UUID): 用户 ID
        :return User: 用户实体
        """

        return await self._require_user(user_id)

    async def _issue_token(
        self,
        *,
        user: User,
        device: UserDevice | None,
        token_type: str,
    ) -> TokenIssue:
        """
        签发令牌。

        :param user (User): 用户
        :param device (UserDevice | None): 设备
        :param token_type (str): 令牌类型
        :return TokenIssue: 令牌
        """
        raw_token = create_opaque_token("art")
        expires_in = (
            self._settings.device_token_ttl_seconds
            if token_type == "device"
            else self._settings.access_token_ttl_seconds
        )
        token = await self._repository.add_token(
            AuthToken(
                user_id=user.id,
                user_device_id=device.id if device else None,
                token_hash=hash_token(self._settings.secret_key, raw_token),
                token_type=token_type,
                status="active",
                expires_at=self._now() + timedelta(seconds=expires_in),
            )
        )
        return TokenIssue(raw_token=raw_token, record=token, expires_in=expires_in)

    async def _audit(
        self,
        *,
        actor_user_id: UUID | None,
        action: str,
        target_type: str,
        target_id: str,
        details: dict[str, object],
    ) -> None:
        """
        读取审计记录。

        :param actor_user_id (UUID | None): actor 用户 ID
        :param action (str): 操作
        :param target_type (str): target 类型
        :param target_id (str): 审计目标 ID
        :param details (dict[str, object]): 详情
        """
        await self._repository.add_audit_log(
            AuditLog(
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
        )

    async def _ensure_username_available(self, username: str) -> None:
        """
        确保用户名可用状态。

        :param username (str): 用户名
        """
        if await self._repository.get_user_by_username(username) is not None:
            raise ApiError(
                code="COMMON_CONFLICT", message="Username already exists.", status_code=409
            )

    async def _require_user(self, user_id: UUID) -> User:
        """
        获取并校验用户。

        :param user_id (UUID): 用户 ID
        :return User: 用户
        """
        user = await self._repository.get_user(user_id)
        if user is None:
            raise ApiError(code="COMMON_NOT_FOUND", message="User was not found.", status_code=404)
        return user

    async def _require_visible_device(self, *, actor: User, device_id: UUID) -> UserDevice:
        """
        获取并校验可见状态设备。

        :param actor (User): 操作用户
        :param device_id (UUID): 设备 ID
        :return UserDevice: 可见状态设备
        """
        device = await self._repository.get_device(device_id)
        if device is None or (actor.role != "admin" and device.user_id != actor.id):
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Device was not found.", status_code=404
            )
        return device

    async def _next_wireguard_ip(self) -> str:
        """
        返回下一个 WireGuard ip。

        :return str: 下一个 WireGuard ip
        """
        count = await self._repository.count_wireguard_peers()
        host = 2 + (count % 200)
        return f"10.77.0.{host}"

    def _revoke_token(self, token: AuthToken) -> None:
        """
        撤销令牌。

        :param token (AuthToken): 令牌
        """
        token.status = "revoked"
        token.revoked_at = self._now()

    def _ensure_cli_code_pending(self, cli_code: CliLoginCode) -> None:
        """
        确保CLI 代码待处理状态。

        :param cli_code (CliLoginCode): CLI 代码
        """
        if self._is_expired(cli_code.expires_at):
            cli_code.status = "expired"
            raise ApiError(
                code="AUTH_TOKEN_EXPIRED", message="CLI login code has expired.", status_code=401
            )
        if cli_code.status != "pending":
            raise ApiError(
                code="COMMON_CONFLICT", message="CLI login code is not pending.", status_code=409
            )

    def _is_expired(self, value: datetime) -> bool:
        """
        判断是否过期状态。

        :param value (datetime): 值
        :return bool: 是否满足校验条件
        """
        expires_at = value if value.tzinfo else value.replace(tzinfo=UTC)
        return expires_at <= self._now()

    def _now(self) -> datetime:
        """
        获取当前时间。

        :return datetime: 当前时间
        """
        return datetime.now(UTC)

    def _create_user_code(self) -> str:
        """
        创建用户代码。

        :return str: 用户代码
        """
        return f"{secrets.randbelow(10_000):04d}-{secrets.randbelow(10_000):04d}"

    def _fingerprint(self, public_key: str) -> str:
        """
        返回指纹。

        :param public_key (str): 公钥
        :return str: 指纹
        """
        return f"SHA256:{sha256(public_key.encode('utf-8')).hexdigest()}"
