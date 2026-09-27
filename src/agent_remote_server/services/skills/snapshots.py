"""
在单次串行化事务中固定会话规则、运行分支和完整物化内容。
"""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import Session
from agent_remote_server.models.skill_snapshots import (
    SessionSkillSnapshot,
    SessionSkillSnapshotItem,
)
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_preparation import SkillPreparationRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.services.skills.account_materialization import AccountMaterialization
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillSnapshotService:
    """
    内部预约入口，调用方另行完成能力检查并负责提交整个会话事务。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        构造共享事务中的存储、规则与运行态访问。

        :param session (AsyncSession): 外层会话创建事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 配额策略
        """
        self._session = session
        self._preparation = SkillPreparationRepository(session)
        self._library = SkillLibraryRepository(session)
        self._runtime = SkillRuntimeRepository(session)
        self._content = SkillContentService(session, store, policy)
        self._materialization = AccountMaterialization(session, store, policy)

    async def reserve(
        self, user_id: UUID, session_id: UUID, task_id: UUID, system_releases: dict[str, object]
    ) -> SessionSkillSnapshot:
        """
        第一次预约固定全部引用，重复启动不受之后配置变更影响。

        :param user_id (UUID): 已认证用户
        :param session_id (UUID): 同一事务创建或恢复的会话
        :param task_id (UUID): 精确准备任务的数据库标识
        :param system_releases (dict[str, object]): 服务端选定的系统版本引用
        :return SessionSkillSnapshot: 新建或重试复用的精确快照
        """
        async with retention_mutation(self._session, user_id):
            library = await self._library.lock_library(user_id)
            session = await self._runtime.session(user_id, session_id)
            if session is None:
                raise SkillContentError("SESSION_NOT_FOUND", "session not found")
            previous = await self._runtime.snapshot_for_session(user_id, session_id)
            if previous is not None:
                if (
                    previous.account_id,
                    previous.node_id,
                    previous.prepare_task_id,
                    previous.runtime_backend,
                ) != (session.tool_account_id, session.node_id, task_id, session.runtime_backend):
                    raise SkillContentError(
                        "SNAPSHOT_BINDING_MISMATCH", "snapshot retry changed its binding"
                    )
                if previous.status not in {"reserved", "started"}:
                    raise SkillContentError(
                        "SNAPSHOT_NOT_ACTIVE", "snapshot can no longer prepare a session"
                    )
                return previous
            await self._validate_task(session, task_id)
            account = await self._library.account(user_id, session.tool_account_id)
            if account is None or account.tool_type != session.tool_type:
                raise SkillContentError("ACCOUNT_NOT_FOUND", "session account not found")
            prepared = await self._materialization.prepare(account)
            upload = await self._content.begin(
                user_id, f"snapshot:{session_id}", prepared.manifest, "account_directory"
            )
            stored = await self._content.complete(user_id, upload.id)
            snapshot = SessionSkillSnapshot(
                id=uuid4(),
                user_id=user_id,
                account_id=account.id,
                node_id=session.node_id,
                session_id=session.id,
                session_reference_id=session.id,
                prepare_task_id=task_id,
                runtime_backend=session.runtime_backend,
                library_generation=library.generation,
                directory_epoch=prepared.epoch,
                starting_checkpoint_id=prepared.head.id,
                tree_digest=stored.digest,
                system_releases_json=system_releases,
                status="reserved",
            )
            self._runtime.add(snapshot)
            await self._runtime.flush()
            for item in prepared.selected:
                self._runtime.add(
                    SessionSkillSnapshotItem(
                        snapshot_id=snapshot.id,
                        state_id=item.state.id,
                        user_id=user_id,
                        account_id=account.id,
                        entry_name=item.name,
                        state_epoch=item.state.epoch,
                        checkpoint_id=item.checkpoint.id,
                        resolution_json=item.rule.model_dump(mode="json"),
                    )
                )
            await self._runtime.flush()
            for item in prepared.selected:
                if item.state.installation_id is not None:
                    await self._preparation.record(item.state, snapshot.id)
            await self._runtime.flush()
            return snapshot

    async def _validate_task(self, session: Session, task_id: UUID) -> None:
        """
        任务内容与会话身份完全一致才可建立授权引用。

        :param session (Session): 已锁定会话
        :param task_id (UUID): 精确任务身份
        """
        task = await self._runtime.task(session.node_id, task_id)
        expected = {
            "session_id": str(session.id),
            "user_id": str(session.user_id),
            "tool_account_id": str(session.tool_account_id),
            "runtime_backend": session.runtime_backend,
        }
        if (
            session.status != "starting"
            or task is None
            or task.task_type != "create_tool_session"
            or task.status != "pending"
            or any(task.payload.get(key) != value for key, value in expected.items())
        ):
            raise SkillContentError(
                "SNAPSHOT_BINDING_MISMATCH", "preparation task does not match starting session"
            )
