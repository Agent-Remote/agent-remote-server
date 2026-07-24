import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import (
    AuditLog,
    AuthToken,
    CliLoginCode,
    SshKey,
    User,
    UserDevice,
    WireGuardPeer,
)
from agent_remote_server.repositories import IdentityRepository
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

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._repository = IdentityRepository(session)

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
        :param display_name (str): 显示名

        :return TokenIssue: 管理员登录令牌
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
        :param totp_code (str): TOTP 验证码

        :return TokenIssue: 登录令牌
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

        self._revoke_token(token)
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

    async def start_cli_login(self) -> CliLoginStart:
        """
        启动 CLI device-code 登录

        :return CliLoginStart: 登录码信息
        """

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

    async def complete_cli_login(self, *, device_code: str) -> TokenIssue:
        """
        CLI 轮询完成登录

        :param device_code (str): device_code

        :return TokenIssue: 登录令牌
        """

        cli_code = await self._repository.get_cli_login_by_device_hash(
            hash_token(self._settings.secret_key, device_code)
        )
        if cli_code is None:
            raise ApiError(
                code="COMMON_NOT_FOUND", message="CLI login code was not found.", status_code=404
            )
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
        await self._audit(
            actor_user_id=user.id,
            action="auth.cli_complete",
            target_type="cli_login_code",
            target_id=str(cli_code.id),
            details={"status": "consumed"},
        )
        await self._session.commit()
        return token_issue

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
        :param display_name (str): 显示名

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
        :param display_name (str): 显示名
        :param status (str): 用户状态

        :return User: 更新后的用户
        """

        user = await self._require_user(user_id)
        if display_name is not None:
            user.display_name = display_name
        if status is not None:
            user.status = status
        await self._audit(
            actor_user_id=actor.id,
            action="users.update",
            target_type="user",
            target_id=str(user.id),
            details={"status": status} if status else {},
        )
        await self._session.commit()
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
        await self._audit(
            actor_user_id=actor.id,
            action="users.disable",
            target_type="user",
            target_id=str(user.id),
            details={},
        )
        await self._session.commit()
        return user

    async def setup_totp(self, *, user: User) -> str:
        """
        创建并保存 TOTP secret

        :param user (User): 当前用户

        :return str: 明文 secret
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
        ssh_public_key: str,
        wireguard_public_key: str | None,
    ) -> DeviceRegistrationResult:
        """
        注册用户设备

        :param user (User): 当前用户
        :param name (str): 设备名称
        :param platform (str): 设备平台
        :param ssh_public_key (str): SSH 公钥
        :param wireguard_public_key (str): WireGuard 公钥

        :return DeviceRegistrationResult: 注册结果
        """

        device = await self._repository.add_device(
            UserDevice(user_id=user.id, name=name, platform=platform, status="active")
        )
        ssh_key = await self._repository.add_ssh_key(
            SshKey(
                user_device_id=device.id,
                public_key=ssh_public_key,
                fingerprint=self._fingerprint(ssh_public_key),
                status="active",
            )
        )
        wireguard_peer = None
        if wireguard_public_key:
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
        token_issue = await self._issue_token(user=user, device=device, token_type="device")
        await self._audit(
            actor_user_id=user.id,
            action="devices.register",
            target_type="user_device",
            target_id=str(device.id),
            details={
                "platform": platform,
                "ssh_key_id": str(ssh_key.id),
                "wireguard_peer_id": str(wireguard_peer.id) if wireguard_peer else None,
            },
        )
        await self._session.commit()
        return DeviceRegistrationResult(
            device=device,
            ssh_key=ssh_key,
            wireguard_peer=wireguard_peer,
            token_issue=token_issue,
        )

    async def revoke_device(self, *, actor: User, device_id: UUID) -> UserDevice:
        """
        撤销设备和关联凭证

        :param actor (User): 操作人
        :param device_id (UUID): 设备 ID

        :return UserDevice: 撤销后的设备
        """

        device = await self._require_visible_device(actor=actor, device_id=device_id)
        revoked_at = self._now()
        device.status = "revoked"
        for ssh_key in await self._repository.list_ssh_keys_for_device(device.id):
            ssh_key.status = "revoked"
            ssh_key.revoked_at = revoked_at
        for peer in await self._repository.list_wireguard_peers_for_device(device.id):
            peer.status = "revoked"
            peer.revoked_at = revoked_at
        for token in await self._repository.list_tokens_for_device(device.id):
            self._revoke_token(token)
        await self._audit(
            actor_user_id=actor.id,
            action="devices.revoke",
            target_type="user_device",
            target_id=str(device.id),
            details={"device_user_id": str(device.user_id)},
        )
        await self._session.commit()
        return device

    async def delete_device(self, *, actor: User, device_id: UUID) -> None:
        """
        删除已撤销且未关联 workspace 的设备

        :param actor (User): 操作人
        :param device_id (UUID): 设备 ID
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

        :return list: 用户列表
        """

        return list(await self._repository.list_users())

    async def list_devices(self, *, user: User) -> list[UserDevice]:
        """
        列出当前用户设备

        :param user (User): 当前用户

        :return list: 设备列表
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
        if await self._repository.get_user_by_username(username) is not None:
            raise ApiError(
                code="COMMON_CONFLICT", message="Username already exists.", status_code=409
            )

    async def _require_user(self, user_id: UUID) -> User:
        user = await self._repository.get_user(user_id)
        if user is None:
            raise ApiError(code="COMMON_NOT_FOUND", message="User was not found.", status_code=404)
        return user

    async def _require_visible_device(self, *, actor: User, device_id: UUID) -> UserDevice:
        device = await self._repository.get_device(device_id)
        if device is None or (actor.role != "admin" and device.user_id != actor.id):
            raise ApiError(
                code="COMMON_NOT_FOUND", message="Device was not found.", status_code=404
            )
        return device

    async def _next_wireguard_ip(self) -> str:
        count = await self._repository.count_wireguard_peers()
        host = 2 + (count % 200)
        return f"10.77.0.{host}"

    def _revoke_token(self, token: AuthToken) -> None:
        token.status = "revoked"
        token.revoked_at = self._now()

    def _ensure_cli_code_pending(self, cli_code: CliLoginCode) -> None:
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
        expires_at = value if value.tzinfo else value.replace(tzinfo=UTC)
        return expires_at <= self._now()

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _create_user_code(self) -> str:
        return f"{secrets.randbelow(10_000):04d}-{secrets.randbelow(10_000):04d}"

    def _fingerprint(self, public_key: str) -> str:
        return f"SHA256:{sha256(public_key.encode('utf-8')).hexdigest()}"
