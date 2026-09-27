"""
在规划与执行边界阻止旧配置导入修改已受管的账户技能目录。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.errors import ApiError
from agent_remote_server.repositories.skill_imports import SkillImportRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.skill_imports import SkillImportAuthorization
from agent_remote_server.services.runtime_migrations import require_runtime_migration_settled


class SkillConfigImportGuard:
    """
    目录所有权不随功能开关关闭而失效，调用方持锁直到规划或授权提交。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        复用账户状态及用户内容锁。

        :param session (AsyncSession): 当前事务
        """
        self.imports = SkillImportRepository(session)
        self.runtime = SkillRuntimeRepository(session)
        self.session = session

    async def plan(self, user_id: UUID, account_id: UUID, paths: list[str]) -> None:
        """
        任一技能路径拒绝整批规划，不能先保存其他配置任务。

        :param user_id (UUID): 已认证所有者
        :param account_id (UUID): 已授权账户
        :param paths (list[str]): 原请求全部包含路径及文件路径
        """
        await require_runtime_migration_settled(self.session, user_id, account_id)
        mode, _ = await self._directory(user_id, account_id)
        _check_paths(mode, paths)

    async def authorize(self, node_id: UUID, task_id: str) -> SkillImportAuthorization:
        """
        用新鲜账户模式授权当前租约，旧排队正文不能保存一次永久写权限。

        :param node_id (UUID): 认证节点
        :param task_id (str): 精确外部任务身份
        :return SkillImportAuthorization: 不含内容的当前绑定
        """
        task = await self.imports.task(node_id, task_id)
        if task is None:
            raise _not_found()
        user_id = _identity(task.payload.get("user_id"))
        account_id = _identity(task.payload.get("tool_account_id"))
        if await self.imports.account(user_id, account_id) is None:
            raise _not_found()
        await SkillStorageRepository(self.session).lock_usage(user_id)
        task = await self.imports.task(node_id, task_id)
        account = await self.imports.account(user_id, account_id)
        if (
            task is None
            or account is None
            or task.status not in {"leased", "running"}
            or task.lease_until is None
            or task.payload.get("user_id") != str(user_id)
            or task.payload.get("tool_account_id") != str(account_id)
            or task.payload.get("tool_type") != account.tool_type
        ):
            raise _not_found()
        lease = task.lease_until
        if lease.replace(tzinfo=lease.tzinfo or UTC) <= datetime.now(UTC):
            raise _not_found()
        files = task.payload.get("files")
        if not isinstance(files, list) or not files:
            raise _not_found()
        paths = []
        for file in files:
            if not isinstance(file, dict) or not isinstance(file.get("path"), str):
                raise _not_found()
            paths.append(file["path"])
        await require_runtime_migration_settled(self.session, user_id, account_id)
        mode, epoch = await self._directory(user_id, account_id)
        _check_paths(mode, paths)
        return SkillImportAuthorization.model_validate(
            {
                "task_id": task.task_id,
                "node_id": node_id,
                "user_id": user_id,
                "account_id": account_id,
                "directory_mode": mode,
                "directory_epoch": epoch,
            }
        )

    async def _directory(self, user_id: UUID, account_id: UUID) -> tuple[str, int]:
        """
        不创建目录状态，不把缺失记录误判成已经接管。

        :param user_id (UUID): 所有者
        :param account_id (UUID): 账户身份
        :return tuple[str, int]: 当前模式及纪元
        """
        directory = await self.runtime.directory(user_id, account_id)
        return (directory.mode, directory.epoch) if directory else ("legacy", 0)


def _check_paths(mode: str, paths: list[str]) -> None:
    """
    路径按原接口别名归一化，只识别账户发现根，不扩大到插件或项目目录。

    :param mode (str): 当前账户模式
    :param paths (list[str]): 待写或待发现路径
    """
    if mode == "legacy":
        return
    for path in paths:
        normalized = path.strip().replace("$HOME/", "~/").rstrip("/")
        if normalized == "~/.claude/skills" or normalized.startswith("~/.claude/skills/"):
            raise ApiError(
                code="SKILL_MANAGER_OWNS_PATH",
                message=(
                    "Skill management owns this path; use account import-config --exclude-skills."
                ),
                status_code=409,
            )


def _identity(value: object) -> UUID:
    """
    非字符串或非法身份统一隐藏为任务不存在。

    :param value (object): 任务中的身份
    :return UUID: 规范身份
    """
    if not isinstance(value, str):
        raise _not_found()
    try:
        return UUID(value)
    except ValueError as error:
        raise _not_found() from error


def _not_found() -> ApiError:
    """
    不泄露其他账户的任务、模式或所有者状态。

    :return ApiError: 统一授权失败
    """
    return ApiError(
        code="COMMON_NOT_FOUND", message="Config import task was not found.", status_code=404
    )
