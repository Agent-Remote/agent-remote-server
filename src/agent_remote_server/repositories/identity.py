from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import (
    AuditLog,
    AuthToken,
    CliLoginCode,
    SshKey,
    User,
    UserDevice,
    WireGuardPeer,
)


class IdentityRepository:
    """
    用户、认证、设备和密钥仓储
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_user(self, user: User) -> User:
        """
        新增用户

        :param user (User): 用户实体

        :return User: 用户实体
        """

        self._session.add(user)
        await self._session.flush()
        return user

    async def has_users(self) -> bool:
        """
        判断是否已有用户

        :return bool: 是否已有用户
        """

        result = await self._session.scalar(select(User.id).limit(1))
        return result is not None

    async def get_user(self, user_id: UUID) -> User | None:
        """
        按主键读取用户

        :param user_id (UUID): 用户 ID

        :return User: 用户实体
        """

        return await self._session.get(User, user_id)

    async def get_user_by_username(self, username: str) -> User | None:
        """
        按用户名读取用户

        :param username (str): 用户名

        :return User: 用户实体
        """

        return await self._session.scalar(select(User).where(User.username == username))

    async def list_users(self) -> Sequence[User]:
        """
        列出用户

        :return Sequence: 用户列表
        """

        result = await self._session.scalars(select(User).order_by(User.created_at))
        return result.all()

    async def add_token(self, token: AuthToken) -> AuthToken:
        """
        新增认证令牌

        :param token (AuthToken): 令牌实体

        :return AuthToken: 令牌实体
        """

        self._session.add(token)
        await self._session.flush()
        return token

    async def get_token_by_hash(self, token_hash: str) -> AuthToken | None:
        """
        按哈希读取认证令牌

        :param token_hash (str): 令牌哈希

        :return AuthToken: 令牌实体
        """

        return await self._session.scalar(
            select(AuthToken).where(AuthToken.token_hash == token_hash)
        )

    async def add_cli_login_code(self, code: CliLoginCode) -> CliLoginCode:
        """
        新增 CLI 登录码

        :param code (CliLoginCode): 登录码实体

        :return CliLoginCode: 登录码实体
        """

        self._session.add(code)
        await self._session.flush()
        return code

    async def get_cli_login_by_device_hash(self, device_code_hash: str) -> CliLoginCode | None:
        """
        按 device_code 哈希读取 CLI 登录码

        :param device_code_hash (str): device_code 哈希

        :return CliLoginCode: 登录码实体
        """

        return await self._session.scalar(
            select(CliLoginCode).where(CliLoginCode.device_code_hash == device_code_hash)
        )

    async def get_cli_login_by_user_code(self, user_code: str) -> CliLoginCode | None:
        """
        按 user_code 读取 CLI 登录码

        :param user_code (str): 用户确认码

        :return CliLoginCode: 登录码实体
        """

        return await self._session.scalar(
            select(CliLoginCode).where(CliLoginCode.user_code == user_code)
        )

    async def add_device(self, device: UserDevice) -> UserDevice:
        """
        新增用户设备

        :param device (UserDevice): 设备实体

        :return UserDevice: 设备实体
        """

        self._session.add(device)
        await self._session.flush()
        return device

    async def get_device(self, device_id: UUID) -> UserDevice | None:
        """
        按主键读取设备

        :param device_id (UUID): 设备 ID

        :return UserDevice: 设备实体
        """

        return await self._session.get(UserDevice, device_id)

    async def list_devices_for_user(self, user_id: UUID) -> Sequence[UserDevice]:
        """
        列出用户设备

        :param user_id (UUID): 用户 ID

        :return Sequence: 设备列表
        """

        result = await self._session.scalars(
            select(UserDevice).where(UserDevice.user_id == user_id).order_by(UserDevice.created_at)
        )
        return result.all()

    async def add_ssh_key(self, ssh_key: SshKey) -> SshKey:
        """
        新增 SSH 公钥

        :param ssh_key (SshKey): SSH 公钥实体

        :return SshKey: SSH 公钥实体
        """

        self._session.add(ssh_key)
        await self._session.flush()
        return ssh_key

    async def list_ssh_keys_for_device(self, device_id: UUID) -> Sequence[SshKey]:
        """
        列出设备 SSH 公钥

        :param device_id (UUID): 设备 ID

        :return Sequence: SSH 公钥列表
        """

        result = await self._session.scalars(
            select(SshKey).where(SshKey.user_device_id == device_id)
        )
        return result.all()

    async def add_wireguard_peer(self, peer: WireGuardPeer) -> WireGuardPeer:
        """
        新增 WireGuard peer

        :param peer (WireGuardPeer): WireGuard peer 实体

        :return WireGuardPeer: WireGuard peer 实体
        """

        self._session.add(peer)
        await self._session.flush()
        return peer

    async def list_wireguard_peers_for_device(self, device_id: UUID) -> Sequence[WireGuardPeer]:
        """
        列出设备 WireGuard peer

        :param device_id (UUID): 设备 ID

        :return Sequence: WireGuard peer 列表
        """

        result = await self._session.scalars(
            select(WireGuardPeer).where(WireGuardPeer.user_device_id == device_id)
        )
        return result.all()

    async def count_wireguard_peers(self) -> int:
        """
        统计 WireGuard peer 数量

        :return int: peer 数量
        """

        return await self._session.scalar(select(func.count(WireGuardPeer.id))) or 0

    async def list_tokens_for_device(self, device_id: UUID) -> Sequence[AuthToken]:
        """
        列出设备令牌

        :param device_id (UUID): 设备 ID

        :return Sequence: 令牌列表
        """

        result = await self._session.scalars(
            select(AuthToken).where(AuthToken.user_device_id == device_id)
        )
        return result.all()

    async def list_audit_logs(
        self,
        *,
        actor_user_id: UUID | None,
        limit: int,
    ) -> Sequence[AuditLog]:
        """
        列出审计日志

        :param actor_user_id (UUID | None): 操作者用户 ID，None 表示不过滤
        :param limit (int): 最大返回数量

        :return Sequence: 审计日志列表
        """

        statement = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        if actor_user_id is not None:
            statement = statement.where(AuditLog.actor_user_id == actor_user_id)
        result = await self._session.scalars(statement)
        return result.all()

    async def get_audit_log(self, audit_log_id: UUID) -> AuditLog | None:
        """
        按 ID 读取审计日志

        :param audit_log_id (UUID): 审计日志 ID

        :return AuditLog | None: 审计日志实体
        """

        return await self._session.get(AuditLog, audit_log_id)

    async def add_audit_log(self, audit_log: AuditLog) -> AuditLog:
        """
        新增审计日志

        :param audit_log (AuditLog): 审计日志实体

        :return AuditLog: 审计日志实体
        """

        self._session.add(audit_log)
        await self._session.flush()
        return audit_log
