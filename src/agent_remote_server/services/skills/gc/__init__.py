"""
在已有历史退役之后原子解除精确树引用、结算额度并登记持久化删除任务。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_content_deletions import SkillContentDeletion
from agent_remote_server.models.skill_storage import SkillContentObject
from agent_remote_server.repositories.skill_content_gc import SkillContentGCRepository
from agent_remote_server.repositories.skill_retention import (
    RetentionIndex,
    SkillRetentionRepository,
)
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.gc.build import (
    affected_objects,
    reclamation_plan,
    requested_keys,
)
from agent_remote_server.services.skills.gc.planning import ContentReclamationPlan
from agent_remote_server.services.skills.retention.build import protection
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.retention.trees import tree_retention
from agent_remote_server.skill_manager.retention.graph import RetentionKey
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy

type ReclamationSnapshot = tuple[
    ContentReclamationPlan, RetentionIndex, tuple[SkillContentObject, ...]
]


@dataclass(frozen=True)
class ContentReclamationResult:
    """
    只报告已结算逻辑额度和新持久化任务，磁盘完成进度由原任务单独查询。
    """

    trees: tuple[RetentionKey, ...]
    package_bytes: int
    state_bytes: int
    pending_file_bytes: int
    deletion_ids: tuple[UUID, ...]


class SkillContentReclamationService:
    """
    内部精确内容回收边界，公开调用方仍需把原请求回执包含在同一外层事务。
    """

    def __init__(self, session: AsyncSession, policy: SkillStoragePolicy) -> None:
        """
        复用调用方事务与部署保留策略。

        :param session (AsyncSession): 外层最终提交的事务
        :param policy (SkillStoragePolicy): 树保留配置
        """
        self._session = session
        self._policy = policy
        self._storage = SkillStorageRepository(session)
        self._retention = SkillRetentionRepository(session)
        self._repository = SkillContentGCRepository(session)

    async def preview(
        self,
        user_id: UUID,
        trees: tuple[RetentionKey, ...],
        *,
        objects: tuple[RetentionKey, ...] = (),
        all_unreferenced: bool = False,
    ) -> ContentReclamationPlan:
        """
        只读完整选择，不改变时钟、树、配额或删除任务。

        :param user_id (UUID): 已认证所有者
        :param trees (tuple[RetentionKey, ...]): 精确授权树
        :param objects (tuple[RetentionKey, ...]): 显式清理此前因租约暂留的分类对象
        :param all_unreferenced (bool): 是否明确越过普通树等待
        :return ContentReclamationPlan: 可完整重验的内部计划
        """
        plan, _, _ = await self._plan(user_id, trees, objects, all_unreferenced=all_unreferenced)
        return plan

    async def _plan(
        self,
        user_id: UUID,
        trees: tuple[RetentionKey, ...],
        objects: tuple[RetentionKey, ...],
        *,
        all_unreferenced: bool,
    ) -> ReclamationSnapshot:
        """
        在同一个锁定快照中读取计划与执行所需的真实对象。

        :param user_id (UUID): 所有者
        :param trees (tuple[RetentionKey, ...]): 树选择
        :param objects (tuple[RetentionKey, ...]): 对象选择
        :param all_unreferenced (bool): 是否提前
        :return ReclamationSnapshot: 计划与同事务记录
        """
        trees, objects = requested_keys(trees, trees=True), requested_keys(objects, trees=False)
        if len(trees) + len(objects) > 1_000_000:
            raise ValueError("content reclamation identity limit exceeded")
        if await self._storage.lock_existing_usage(user_id) is None:
            raise SkillContentError("CONTENT_NOT_FOUND", "user has no retained content")
        index = await self._retention.load(user_id)
        selected_objects = affected_objects(index, trees) | set(objects)
        rows = await self._repository.objects(user_id, {key.identity for key in selected_objects})
        now = datetime.now(UTC)
        plan = reclamation_plan(
            user_id,
            index,
            tree_retention(index, protection(index, now), self._policy),
            rows,
            trees,
            objects,
            now,
            all_unreferenced=all_unreferenced,
        )
        return plan, index, rows

    async def apply(
        self, user_id: UUID, expected: ContentReclamationPlan
    ) -> ContentReclamationResult:
        """
        整份重验后才修改树、额度、标记和任务，调用方捕获错误也不会留下半次回收。

        :param user_id (UUID): 已认证所有者
        :param expected (ContentReclamationPlan): 已审阅完整计划
        :return ContentReclamationResult: 已结算额度与待提交的新任务身份
        """
        if expected.user_id != user_id:
            raise SkillContentError("CONTENT_NOT_FOUND", "content plan belongs to another user")
        async with retention_mutation(self._session, user_id):
            actual, index, rows = await self._plan(
                user_id,
                expected.requested_trees,
                expected.requested_objects,
                all_unreferenced=expected.all_unreferenced,
            )
            if actual != expected:
                raise SkillContentError("HEAD_CHANGED", "content reclamation preview has changed")
            if not actual.ready:
                raise SkillContentError(
                    "CONTENT_REFERENCED", "content reclamation plan has blockers"
                )
            return await self._release(actual, index, rows)

    async def _release(
        self,
        plan: ContentReclamationPlan,
        index: RetentionIndex,
        rows: tuple[SkillContentObject, ...],
    ) -> ContentReclamationResult:
        """
        最后物理引用消失才登记删除，零预留活动上传保留分类计量。

        :param plan (ContentReclamationPlan): 同锁重验通过的计划
        :param index (RetentionIndex): 同锁完整引用库存
        :param rows (tuple[SkillContentObject, ...]): 同锁对象记录
        :return ContentReclamationResult: 逻辑结算及持久化任务
        """
        usage = await self._storage.lock_existing_usage(plan.user_id)
        assert usage is not None
        if usage.package_bytes < plan.package_bytes or usage.state_bytes < plan.state_bytes:
            raise SkillContentError("CONTENT_ACCOUNTING_INVALID", "content usage is inconsistent")
        usage.package_bytes -= plan.package_bytes
        usage.state_bytes -= plan.state_bytes
        selected = set(plan.requested_trees)
        for tree in index.trees:
            key = RetentionKey(
                "package_tree" if tree.category == "package" else "state_tree", tree.digest
            )
            if key in selected:
                await self._repository.remove_tree(tree)
        await self._repository.flush()
        by_key = {
            RetentionKey(
                "package_object" if row.category == "package" else "state_object", row.digest
            ): row
            for row in rows
        }
        tasks: list[UUID] = []
        for blob in plan.blobs:
            if blob.delete_file:
                task = SkillContentDeletion(
                    id=uuid4(),
                    user_id=plan.user_id,
                    digest=blob.digest,
                    size=blob.objects[0].size,
                    category_mask=sum(
                        1 if row.key.kind == "package_object" else 2 for row in blob.objects
                    ),
                    status="pending",
                    attempts=0,
                    next_attempt_at=datetime.now(UTC),
                )
                self._repository.add_task(task)
                tasks.append(task.id)
            for obj in blob.objects:
                if not obj.release:
                    continue
                row = by_key[obj.key]
                if blob.delete_file:
                    row.status = "deleting"
                else:
                    await self._repository.remove_object(row)
        await self._repository.flush()
        return ContentReclamationResult(
            plan.requested_trees,
            plan.package_bytes,
            plan.state_bytes,
            plan.pending_file_bytes,
            tuple(tasks),
        )
