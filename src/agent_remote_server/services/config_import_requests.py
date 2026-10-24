"""
将审核意图、精确节点任务和原始受理凭据作为单个事务发布。
"""

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.errors import ApiError
from agent_remote_server.models import ToolAccount, ToolAccountConfigImportRequest, User
from agent_remote_server.repositories.config_import_requests import ConfigImportRequestRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.config_import_requests import (
    ConfigImportManifestEntry,
    ConfigImportReceipt,
    ConfigImportRequest,
    WarningCode,
)
from agent_remote_server.schemas.tool_accounts import ToolAccountConfigImportFile
from agent_remote_server.services.config_import_intent import (
    intent_fingerprint,
    invalid_import,
    manifest,
)
from agent_remote_server.services.tool_accounts import (
    ASK_CONFIG_IMPORT_PREFIXES,
    DEFAULT_CONFIG_IMPORT_PATHS,
    RESUME_HISTORY_PATHS,
    ToolAccountService,
)


def not_found() -> ApiError:
    """
    缺失及跨范围身份使用相同错误，避免泄露其他用户的受理历史。

    :return ApiError: 固定的不可见记录错误
    """
    return ApiError(
        code="COMMON_NOT_FOUND", message="Config import request was not found.", status_code=404
    )


def replay(
    record: ToolAccountConfigImportRequest, account_id: UUID, fingerprint: str
) -> ConfigImportReceipt:
    """
    同用户键只能代表同一个账户及完整原始意图。

    :param record (ToolAccountConfigImportRequest): 原始不可变凭据
    :param account_id (UUID): 本次已授权账户
    :param fingerprint (str): 本次重新计算的意图
    :return ConfigImportReceipt: 未修改的原始受理数据
    """
    if record.tool_account_id != account_id or record.intent_fingerprint != fingerprint:
        raise ApiError(
            code="CONFIG_IMPORT_REQUEST_CONFLICT",
            message="Request identity is already bound to a different import.",
            status_code=409,
        )
    return ConfigImportReceipt.model_validate(record.receipt_json)


class ConfigImportRequestService:
    """
    接受、重放和恢复都先验证当前用户对精确账户的权限。
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """
        复用原有任务规划服务，但由此服务拥有唯一提交点。

        :param session (AsyncSession): 当前请求事务
        :param settings (Settings): 节点和账户配置
        """
        self.session = session
        self.repository = ConfigImportRequestRepository(session)
        self.legacy = ToolAccountService(session, settings)

    async def authorized(self, user: User, account_id: UUID, *, lock: bool = False) -> ToolAccount:
        """
        不允许管理员或别的设备用户越过原账户归属。

        :param user (User): 已通过活动凭据校验的用户
        :param account_id (UUID): 当前请求账户
        :param lock (bool): 是否串行化账户写入准入
        :return ToolAccount: 新鲜读取的已授权账户
        """
        account = await self.repository.account(user.id, account_id, lock=lock)
        if account is None:
            raise not_found()
        return account

    async def get(self, user: User, account_id: UUID, request_id: UUID) -> ConfigImportReceipt:
        """
        只读定位原调用方键，不取得写锁或修改任务及设备活动时间。

        :param user (User): 当前活动用户
        :param account_id (UUID): 原始账户范围
        :param request_id (UUID): 原始调用方键
        :return ConfigImportReceipt: 原始接受凭据而非执行成功声明
        """
        await self.authorized(user, account_id)
        record = await self.repository.request(user.id, request_id)
        if record is None or record.tool_account_id != account_id:
            raise not_found()
        return ConfigImportReceipt.model_validate(record.receipt_json)

    async def create(
        self, user: User, account_id: UUID, payload: ConfigImportRequest
    ) -> tuple[ConfigImportReceipt, bool]:
        """
        丢失响应后相同请求返回原始任务，唯一约束冲突也必须先回滚所有新任务。

        :param user (User): 当前活动用户
        :param account_id (UUID): 已选择的目标账户
        :param payload (ConfigImportRequest): 严格原始请求
        :return tuple[ConfigImportReceipt, bool]: 不可变凭据及是否首次接受
        """
        await self.authorized(user, account_id)
        entries = manifest(payload)
        fingerprint = intent_fingerprint(account_id, payload, entries)
        if fingerprint != payload.expected_intent_fingerprint:
            raise ApiError(
                code="CONFIG_IMPORT_INTENT_MISMATCH",
                message="Reviewed import intent does not match the request.",
                status_code=422,
            )
        request_id = UUID(payload.request_id)
        prior = await self.repository.request(user.id, request_id)
        if prior is not None:
            return replay(prior, account_id, fingerprint), False
        user_id = user.id
        try:
            await SkillStorageRepository(self.session).lock_usage(user_id)
            account = await self.authorized(user, account_id, lock=True)
            prior = await self.repository.request(user_id, request_id)
            if prior is not None:
                result = replay(prior, account_id, fingerprint)
                await self.session.rollback()
                return result, False
            result = await self._accept(user, account, payload, entries, fingerprint)
            await self.session.commit()
            return result, True
        except IntegrityError:
            await self.session.rollback()
            current_account = await self.repository.account(user_id, account_id)
            if current_account is None:
                raise not_found() from None
            prior = await self.repository.request(user_id, request_id)
            if prior is not None:
                return replay(prior, account_id, fingerprint), False
            raise
        except Exception:
            await self.session.rollback()
            raise

    async def _accept(
        self,
        user: User,
        account: ToolAccount,
        payload: ConfigImportRequest,
        entries: list[ConfigImportManifestEntry],
        fingerprint: str,
    ) -> ConfigImportReceipt:
        """
        在持锁事务内按既有准入规则创建唯一任务，并加入其完整受理凭据。

        :param user (User): 原始已认证用户
        :param account (ToolAccount): 已锁定并刷新授权的账户
        :param payload (ConfigImportRequest): 原始严格请求
        :param entries (list[ConfigImportManifestEntry]): 独立复核过的内容身份
        :param fingerprint (str): 原始完整意图摘要
        :return ConfigImportReceipt: 尚待外层事务提交的受理凭据
        """
        if account.status == "disabled":
            raise ApiError(
                code="ACCOUNT_DISABLED",
                message="Disabled accounts cannot accept imports.",
                status_code=409,
            )
        accepted, rejected, _ = self.legacy._classify_config_import_paths(
            include=payload.include,
            exclude=set(payload.exclude),
            include_resume_history=payload.include_resume_history,
        )
        if rejected or accepted != payload.include:
            raise invalid_import()
        result = await self.legacy.plan_config_import(
            user=user,
            account_id=account.id,
            tool_type=payload.tool_type,
            include=payload.include,
            exclude=payload.exclude,
            files=[
                ToolAccountConfigImportFile(**file.model_dump())
                for file in sorted(payload.files, key=lambda file: file.path)
            ],
            include_resume_history=payload.include_resume_history,
            dry_run=False,
            commit=False,
        )
        if (
            result.task_id is None
            or result.account_remote_path is None
            or result.imported_file_count != len(entries)
        ):
            raise RuntimeError("config import task admission did not produce its exact receipt")
        task = await self.repository.task(result.task_id)
        if (
            task is None
            or task.payload.get("tool_account_id") != str(account.id)
            or task.payload.get("user_id") != str(user.id)
        ):
            raise RuntimeError("config import task identity did not match receipt scope")
        warnings: list[WarningCode] = []
        for path in accepted:
            if path in DEFAULT_CONFIG_IMPORT_PATHS or path in RESUME_HISTORY_PATHS:
                continue
            warning: WarningCode = (
                "CONFIG_IMPORT_EXECUTABLE_REVIEW_REQUIRED"
                if any(path.startswith(prefix) for prefix in ASK_CONFIG_IMPORT_PREFIXES)
                else "CONFIG_IMPORT_CUSTOM_MARKDOWN_REVIEW_REQUIRED"
            )
            if warning not in warnings:
                warnings.append(warning)
        receipt = ConfigImportReceipt(
            schema_version=1,
            request_id=payload.request_id,
            tool_account_id=str(account.id),
            tool_type=payload.tool_type,
            source=payload.source,
            include=payload.include,
            exclude=payload.exclude,
            include_resume_history=payload.include_resume_history,
            dry_run=False,
            manifest=entries,
            intent_fingerprint=fingerprint,
            task_id=result.task_id,
            accepted=result.accepted,
            rejected=[],
            warnings=warnings,
            account_remote_path=result.account_remote_path,
            file_count=len(entries),
        )
        await self.repository.add(
            ToolAccountConfigImportRequest(
                user_id=user.id,
                request_id=UUID(payload.request_id),
                tool_account_id=account.id,
                tool_type=account.tool_type,
                node_id=task.node_id,
                task_record_id=task.id,
                task_id=task.task_id,
                intent_fingerprint=fingerprint,
                receipt_json=receipt.model_dump(mode="json"),
            )
        )
        return receipt
