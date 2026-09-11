from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import (
    EgoBrowserBinding,
    EgoBrowserDevice,
    EgoBrowserDeviceCredential,
    EgoBrowserRequestLedger,
    EgoBrowserRevocationOutbox,
    Node,
    NodeTask,
    Session,
    Workspace,
)

LIVE_STATUSES = {"pending_device", "connecting", "probing_local_browser", "active", "paused"}
TERMINAL_STATUSES = {"stopped", "expired", "failed", "revoked"}


class EgoBrowserRepository:
    """独立 ego-browser 控制面的持久化边界。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_device(self, device: EgoBrowserDevice) -> EgoBrowserDevice:
        """
        新增一个 ego-browser 设备记录。

        :param device (EgoBrowserDevice): 独立 ego-browser 设备实体

        :return EgoBrowserDevice: 注册、轮换或撤销后的独立设备实体
        """
        self._session.add(device)
        await self._session.flush()
        return device

    async def add_device_credential(
        self, credential: EgoBrowserDeviceCredential
    ) -> EgoBrowserDeviceCredential:
        """
        新增一个独立 ego-browser 设备客户端凭据记录。

        :param credential (EgoBrowserDeviceCredential): 独立设备凭据实体

        :return EgoBrowserDeviceCredential: 已加入数据库会话的设备凭据实体
        """

        self._session.add(credential)
        await self._session.flush()
        return credential

    async def get_device_credential_by_hash(
        self, token_hash: str, *, for_update: bool = False
    ) -> EgoBrowserDeviceCredential | None:
        """
        按 keyed hash 读取独立设备凭据。

        :param token_hash (str): 一次性凭据的 keyed hash
        :param for_update (bool): 是否获取数据库行锁

        :return EgoBrowserDeviceCredential | None: 匹配的有效或历史设备凭据；不存在时为 None
        """

        statement = select(EgoBrowserDeviceCredential).where(
            EgoBrowserDeviceCredential.token_hash == token_hash
        )
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def list_device_credentials(
        self, device_id: UUID, *, for_update: bool = False
    ) -> Sequence[EgoBrowserDeviceCredential]:
        """
        读取指定设备的全部独立凭据。

        :param device_id (UUID): 独立 ego-browser 设备 ID
        :param for_update (bool): 是否获取数据库行锁

        :return Sequence[EgoBrowserDeviceCredential]: 指定设备的凭据记录列表
        """

        statement = (
            select(EgoBrowserDeviceCredential)
            .where(EgoBrowserDeviceCredential.ego_browser_device_id == device_id)
            .order_by(EgoBrowserDeviceCredential.created_at.asc(), EgoBrowserDeviceCredential.id)
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.scalars(statement)
        return result.all()

    async def get_device(
        self, device_id: UUID, *, for_update: bool = False
    ) -> EgoBrowserDevice | None:
        """
        按 ID 读取设备，可选地锁定记录。

        :param device_id (UUID): 独立 ego-browser 设备 ID
        :param for_update (bool): 是否获取数据库行锁

        :return EgoBrowserDevice | None: 匹配的独立设备；不存在时为 None
        """
        statement = select(EgoBrowserDevice).where(EgoBrowserDevice.id == device_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def list_devices(self, user_id: UUID) -> Sequence[EgoBrowserDevice]:
        """
        列出用户拥有的 ego-browser 设备。

        :param user_id (UUID): 所属用户 ID

        :return Sequence[EgoBrowserDevice]: 按稳定顺序排列的独立设备列表
        """
        result = await self._session.scalars(
            select(EgoBrowserDevice)
            .where(EgoBrowserDevice.user_id == user_id)
            .order_by(EgoBrowserDevice.created_at.desc(), EgoBrowserDevice.id)
        )
        return result.all()

    async def list_all_devices(self) -> Sequence[EgoBrowserDevice]:
        """
        按稳定顺序列出全部 ego-browser 设备。

        :return Sequence[EgoBrowserDevice]: 按稳定顺序排列的独立设备列表
        """

        result = await self._session.scalars(
            select(EgoBrowserDevice).order_by(
                EgoBrowserDevice.created_at.desc(), EgoBrowserDevice.id
            )
        )
        return result.all()

    async def get_session(self, session_id: UUID, *, for_update: bool = False) -> Session | None:
        """
        按 ID 读取工具 session，可选地锁定记录。

        :param session_id (UUID): 远端工具 session ID
        :param for_update (bool): 是否获取数据库行锁

        :return Session | None: 匹配的工具 session；不存在时为 None
        """
        statement = select(Session).where(Session.id == session_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def get_node(self, node_id: UUID) -> Node | None:
        """
        按 ID 读取节点记录。

        :param node_id (UUID): 节点 ID

        :return Node | None: 匹配的节点实体；不存在时为 None
        """
        return await self._session.get(Node, node_id)

    async def get_binding(
        self, binding_id: UUID, *, for_update: bool = False
    ) -> EgoBrowserBinding | None:
        """
        按 ID 读取 binding，可选地锁定记录。

        :param binding_id (UUID): ego-browser binding 标识
        :param for_update (bool): 是否获取数据库行锁

        :return EgoBrowserBinding | None: 匹配的 binding；不存在或无需更新时为 None
        """
        statement = select(EgoBrowserBinding).where(EgoBrowserBinding.id == binding_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def list_bindings(self, user_id: UUID) -> Sequence[EgoBrowserBinding]:
        """
        列出用户拥有的全部 binding。

        :param user_id (UUID): 所属用户 ID

        :return Sequence[EgoBrowserBinding]: 符合查询条件的 ego-browser binding 列表
        """
        result = await self._session.scalars(
            select(EgoBrowserBinding)
            .where(EgoBrowserBinding.user_id == user_id)
            .order_by(EgoBrowserBinding.created_at.desc(), EgoBrowserBinding.id)
        )
        return result.all()

    async def list_live_for_user(
        self, user_id: UUID, *, for_update: bool = False
    ) -> Sequence[EgoBrowserBinding]:
        """
        读取用户拥有的全部非终态 binding。

        :param user_id (UUID): 所属用户 ID
        :param for_update (bool): 是否获取数据库行锁

        :return Sequence[EgoBrowserBinding]: 符合查询条件的 ego-browser binding 列表
        """

        statement = (
            select(EgoBrowserBinding)
            .where(EgoBrowserBinding.user_id == user_id)
            .where(EgoBrowserBinding.status.in_(LIVE_STATUSES))
            .order_by(EgoBrowserBinding.created_at.asc(), EgoBrowserBinding.id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.scalars(statement)
        return result.all()

    async def list_all_bindings(self) -> Sequence[EgoBrowserBinding]:
        """
        按稳定顺序列出全部 ego-browser binding。

        :return Sequence[EgoBrowserBinding]: 符合查询条件的 ego-browser binding 列表
        """

        result = await self._session.scalars(
            select(EgoBrowserBinding).order_by(
                EgoBrowserBinding.created_at.desc(), EgoBrowserBinding.id
            )
        )
        return result.all()

    async def list_live_for_device(
        self, device_id: UUID, *, for_update: bool = False
    ) -> Sequence[EgoBrowserBinding]:
        """
        读取设备的全部非终态 binding。

        :param device_id (UUID): 独立 ego-browser 设备 ID
        :param for_update (bool): 是否获取数据库行锁

        :return Sequence[EgoBrowserBinding]: 符合查询条件的 ego-browser binding 列表
        """
        statement = (
            select(EgoBrowserBinding)
            .where(EgoBrowserBinding.ego_browser_device_id == device_id)
            .where(EgoBrowserBinding.status.in_(LIVE_STATUSES))
            .order_by(EgoBrowserBinding.created_at.asc(), EgoBrowserBinding.id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.scalars(statement)
        return result.all()

    async def has_any_for_device(self, device_id: UUID) -> bool:
        """
        判断设备是否仍保留任意 binding 历史。

        :param device_id (UUID): 独立 ego-browser 设备 ID

        :return bool: 设备是否存在 binding 记录
        """

        value = await self._session.scalar(
            select(EgoBrowserBinding.id)
            .where(EgoBrowserBinding.ego_browser_device_id == device_id)
            .limit(1)
        )
        return value is not None

    async def has_active_requests(self, binding_id: UUID) -> bool:
        """
        判断 binding 是否仍有未终结的执行请求。

        :param binding_id (UUID): ego-browser binding 标识

        :return bool: 是否存在可取消的活动请求
        """

        value = await self._session.scalar(
            select(EgoBrowserRequestLedger.id)
            .where(
                EgoBrowserRequestLedger.binding_id == binding_id,
                EgoBrowserRequestLedger.direction == "request",
                EgoBrowserRequestLedger.message_type == "execute",
                EgoBrowserRequestLedger.status.in_(("accepted", "cancel_requested")),
            )
            .limit(1)
        )
        return value is not None

    async def has_pending_outbox(self, binding_id: UUID) -> bool:
        """
        判断 binding 是否仍有未发布的撤销事件。

        :param binding_id (UUID): ego-browser binding 标识

        :return bool: 是否存在待发布撤销事件
        """

        value = await self._session.scalar(
            select(EgoBrowserRevocationOutbox.id)
            .where(
                EgoBrowserRevocationOutbox.binding_id == binding_id,
                EgoBrowserRevocationOutbox.delivered_at.is_(None),
            )
            .limit(1)
        )
        return value is not None

    async def delete_binding(self, binding: EgoBrowserBinding) -> None:
        """
        删除一个已通过服务层校验的 binding 及其内容无关的历史账本。

        :param binding (EgoBrowserBinding): 待删除的 binding 实体
        """

        # 显式清理子表，保证 SQLite 测试和未启用外键级联的兼容部署不留下孤儿记录。
        await self._session.execute(
            delete(EgoBrowserRequestLedger).where(EgoBrowserRequestLedger.binding_id == binding.id)
        )
        await self._session.execute(
            delete(EgoBrowserRevocationOutbox).where(
                EgoBrowserRevocationOutbox.binding_id == binding.id
            )
        )
        await self._session.delete(binding)

    async def delete_device(self, device: EgoBrowserDevice) -> None:
        """
        删除一个已通过服务层校验的独立设备及其历史凭据。

        :param device (EgoBrowserDevice): 待删除的设备实体
        """

        # 设备凭据虽声明了 CASCADE，显式删除可覆盖 SQLite 默认不启用外键的场景。
        await self._session.execute(
            delete(EgoBrowserDeviceCredential).where(
                EgoBrowserDeviceCredential.ego_browser_device_id == device.id
            )
        )
        await self._session.delete(device)

    async def list_live_for_session(
        self, session_id: UUID, *, for_update: bool = False
    ) -> Sequence[EgoBrowserBinding]:
        """
        读取工具 session 的全部非终态 binding。

        :param session_id (UUID): 远端工具 session ID
        :param for_update (bool): 是否获取数据库行锁

        :return Sequence[EgoBrowserBinding]: 符合查询条件的 ego-browser binding 列表
        """
        statement = (
            select(EgoBrowserBinding)
            .where(
                or_(
                    EgoBrowserBinding.tool_session_id == session_id,
                    EgoBrowserBinding.tool_session_reference_id == session_id,
                )
            )
            .where(EgoBrowserBinding.status.in_(LIVE_STATUSES))
            .order_by(EgoBrowserBinding.created_at.asc(), EgoBrowserBinding.id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.scalars(statement)
        return result.all()

    async def list_live_for_node(
        self, node_id: UUID, *, for_update: bool = False
    ) -> Sequence[EgoBrowserBinding]:
        """
        读取节点上的全部非终态 binding。

        :param node_id (UUID): 节点 ID
        :param for_update (bool): 是否获取数据库行锁

        :return Sequence[EgoBrowserBinding]: 符合查询条件的 ego-browser binding 列表
        """

        statement = (
            select(EgoBrowserBinding)
            .where(EgoBrowserBinding.node_id == node_id)
            .where(EgoBrowserBinding.status.in_(LIVE_STATUSES))
            .order_by(EgoBrowserBinding.created_at.asc(), EgoBrowserBinding.id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.scalars(statement)
        return result.all()

    async def list_due_bindings(
        self, now: datetime, *, for_update: bool = False
    ) -> Sequence[EgoBrowserBinding]:
        """
        读取已达到租约、宽限或绝对 TTL 的非终态 binding。

        :param now (datetime): 待规范化的时间
        :param for_update (bool): 是否获取数据库行锁

        :return Sequence[EgoBrowserBinding]: 符合查询条件的 ego-browser binding 列表
        """

        statement = (
            select(EgoBrowserBinding)
            .where(EgoBrowserBinding.status.in_(LIVE_STATUSES))
            .where(
                (EgoBrowserBinding.absolute_ttl_until <= now)
                | (
                    (EgoBrowserBinding.lease_health == "healthy")
                    & EgoBrowserBinding.lease_until.is_not(None)
                    & (EgoBrowserBinding.lease_until <= now)
                )
                | (
                    (EgoBrowserBinding.lease_health == "renewal_grace")
                    & EgoBrowserBinding.lease_grace_until.is_not(None)
                    & (EgoBrowserBinding.lease_grace_until <= now)
                )
            )
            .order_by(EgoBrowserBinding.created_at.asc(), EgoBrowserBinding.id.asc())
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.scalars(statement)
        return result.all()

    async def has_any_for_node(self, node_id: UUID) -> bool:
        """
        判断节点是否仍保留 ego-browser binding 历史。

        :param node_id (UUID): 节点 ID

        :return bool: 节点是否保留任何 ego-browser binding 历史
        """

        value = await self._session.scalar(
            select(EgoBrowserBinding.id).where(EgoBrowserBinding.node_id == node_id).limit(1)
        )
        return value is not None

    async def list_candidates(
        self, user_id: UUID
    ) -> Sequence[tuple[Session, Node, EgoBrowserBinding | None, Workspace]]:
        """
        查询用户可明确选择的 Claude session 候选。

        :param user_id (UUID): 所属用户 ID

        :return Sequence[tuple]: 可认领 session、节点、现有 binding 与工作区组合
        """
        binding = EgoBrowserBinding
        result = await self._session.execute(
            select(Session, Node, binding, Workspace)
            .join(Node, Node.id == Session.node_id)
            .join(Workspace, Workspace.id == Session.workspace_id)
            .outerjoin(
                binding,
                and_(
                    binding.tool_session_id == Session.id,
                    binding.status.in_(LIVE_STATUSES),
                ),
            )
            .where(Session.user_id == user_id)
            .where(Session.tool_type == "claude")
            .where(Session.status.in_({"running", "active", "detached"}))
            .order_by(Session.updated_at.desc(), Session.id.asc())
        )
        return result.tuples().all()

    async def add_binding(self, binding: EgoBrowserBinding) -> EgoBrowserBinding:
        """
        新增一个 ego-browser 绑定。

        :param binding (EgoBrowserBinding): ego-browser binding 实体

        :return EgoBrowserBinding: 操作后的 ego-browser binding 实体
        """
        self._session.add(binding)
        await self._session.flush()
        return binding

    async def add_ledger(self, ledger: EgoBrowserRequestLedger) -> EgoBrowserRequestLedger:
        """
        新增一条外层信封重放记录。

        :param ledger (EgoBrowserRequestLedger): 外层请求重放账本实体

        :return EgoBrowserRequestLedger: 已加入数据库会话或更新后的请求账本实体
        """
        self._session.add(ledger)
        await self._session.flush()
        return ledger

    async def get_ledger(
        self,
        *,
        binding_id: UUID,
        generation: int,
        direction: str,
        request_id: str | None = None,
        sequence: int | None = None,
        for_update: bool = False,
    ) -> EgoBrowserRequestLedger | None:
        """
        按请求或序号读取重放记录。

        :param binding_id (UUID): ego-browser binding 标识
        :param generation (int): 目标 binding generation
        :param direction (str): 外层信封传输方向
        :param request_id (str | None): 外层浏览器请求 ID
        :param sequence (int | None): 当前 generation 和方向内的信封序号
        :param for_update (bool): 是否获取数据库行锁

        :return EgoBrowserRequestLedger | None: 匹配的请求账本记录；不存在时为 None
        """
        statement = select(EgoBrowserRequestLedger).where(
            EgoBrowserRequestLedger.binding_id == binding_id,
            EgoBrowserRequestLedger.generation == generation,
            EgoBrowserRequestLedger.direction == direction,
        )
        if request_id is not None:
            statement = statement.where(EgoBrowserRequestLedger.request_id == request_id)
        if sequence is not None:
            statement = statement.where(EgoBrowserRequestLedger.sequence == sequence)
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def list_active_requests(self, *, binding_id: UUID) -> Sequence[EgoBrowserRequestLedger]:
        """
        按接受顺序查询 binding 当前仍可取消的请求。

        :param binding_id (UUID): ego-browser binding 标识

        :return Sequence[EgoBrowserRequestLedger]: 符合条件的外层请求账本记录
        """

        result = await self._session.scalars(
            select(EgoBrowserRequestLedger)
            .where(
                EgoBrowserRequestLedger.binding_id == binding_id,
                EgoBrowserRequestLedger.direction == "request",
                EgoBrowserRequestLedger.message_type == "execute",
                EgoBrowserRequestLedger.status.in_({"accepted", "cancel_requested"}),
            )
            .order_by(
                EgoBrowserRequestLedger.created_at.asc(),
                EgoBrowserRequestLedger.sequence.asc(),
            )
        )
        return result.all()

    async def cancel_generation_requests(
        self,
        *,
        binding_id: UUID,
        generation: int,
    ) -> Sequence[EgoBrowserRequestLedger]:
        """
        锁定并终结指定 generation 中尚未完成的请求。

        :param binding_id (UUID): ego-browser binding 标识
        :param generation (int): 目标 binding generation

        :return Sequence[EgoBrowserRequestLedger]: 符合条件的外层请求账本记录
        """

        result = await self._session.scalars(
            select(EgoBrowserRequestLedger)
            .where(
                EgoBrowserRequestLedger.binding_id == binding_id,
                EgoBrowserRequestLedger.generation == generation,
                EgoBrowserRequestLedger.direction == "request",
                EgoBrowserRequestLedger.message_type == "execute",
                EgoBrowserRequestLedger.status.in_(("accepted", "cancel_requested")),
            )
            .order_by(EgoBrowserRequestLedger.created_at, EgoBrowserRequestLedger.id)
            .with_for_update()
        )
        requests = result.all()
        for request in requests:
            request.status = "cancelled"
        return requests

    async def add_task(self, task: NodeTask) -> NodeTask:
        """
        新增请求取消节点任务。

        :param task (NodeTask): 待核对的 Node 取消任务

        :return NodeTask: 已加入数据库会话的 Node 任务实体
        """

        self._session.add(task)
        await self._session.flush()
        return task

    async def max_sequence(
        self,
        *,
        binding_id: UUID,
        generation: int,
        direction: str,
    ) -> int | None:
        """
        读取一个 generation/方向已接受的最大 sequence。

        :param binding_id (UUID): ego-browser binding 标识
        :param generation (int): 目标 binding generation
        :param direction (str): 外层信封传输方向

        :return int | None: 已接受的最大序号；尚无记录时为 None
        """

        return await self._session.scalar(
            select(func.max(EgoBrowserRequestLedger.sequence)).where(
                EgoBrowserRequestLedger.binding_id == binding_id,
                EgoBrowserRequestLedger.generation == generation,
                EgoBrowserRequestLedger.direction == direction,
            )
        )

    async def add_outbox(self, event: EgoBrowserRevocationOutbox) -> EgoBrowserRevocationOutbox:
        """
        新增一条撤销 outbox 事件。

        :param event (EgoBrowserRevocationOutbox): 待投递的撤销 outbox 事件

        :return EgoBrowserRevocationOutbox: 已加入数据库会话的撤销 outbox 事件
        """
        self._session.add(event)
        await self._session.flush()
        return event

    async def list_pending_outbox(
        self, *, limit: int, for_update: bool = False
    ) -> Sequence[EgoBrowserRevocationOutbox]:
        """
        按创建顺序读取尚未投递的撤销事件。

        :param limit (int): 单次处理的最大记录数；None 表示使用配置值
        :param for_update (bool): 是否获取数据库行锁

        :return Sequence[EgoBrowserRevocationOutbox]: 尚未成功投递的撤销事件列表
        """

        statement = (
            select(EgoBrowserRevocationOutbox)
            .where(EgoBrowserRevocationOutbox.delivered_at.is_(None))
            .order_by(
                EgoBrowserRevocationOutbox.created_at.asc(), EgoBrowserRevocationOutbox.id.asc()
            )
            .limit(limit)
        )
        if for_update:
            statement = statement.with_for_update(skip_locked=True)
        result = await self._session.scalars(statement)
        return result.all()

    async def get_outbox(
        self,
        *,
        binding_id: UUID,
        generation: int,
        for_update: bool = False,
    ) -> EgoBrowserRevocationOutbox | None:
        """
        按绑定代次读取一条撤销待发布事件。

        :param binding_id (UUID): ego-browser binding 标识
        :param generation (int): 目标 binding generation
        :param for_update (bool): 是否获取数据库行锁

        :return EgoBrowserRevocationOutbox | None: 指定 generation 的撤销事件；不存在时为 None
        """

        statement = select(EgoBrowserRevocationOutbox).where(
            EgoBrowserRevocationOutbox.binding_id == binding_id,
            EgoBrowserRevocationOutbox.generation == generation,
        )
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def acquire_user_lock(self, user_id: UUID) -> None:
        """
        在 PostgreSQL 中串行化认领操作，SQLite 依赖事务锁。

        :param user_id (UUID): 所属用户 ID
        """

        if self._session.get_bind().dialect.name != "postgresql":
            return
        from sqlalchemy import text

        key = user_id.int & ((1 << 63) - 1)
        await self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    async def normalize_now(self, value: datetime) -> datetime:
        """
        将无时区时间规范化为 UTC 时间。

        :param value (datetime): 待规范化的时间

        :return datetime: 包含 UTC 时区的时间
        """
        return value if value.tzinfo else value.replace(tzinfo=UTC)
