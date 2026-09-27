"""
用未上传清单预览人工解决候选，发布授权仍完全由正常解决命令决定。
"""

from functools import partial
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.repositories.skill_resolution import SkillResolutionRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.schemas.skill_merge import SkillMergeResult
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.schemas.skill_resolution_preview import (
    SkillResolutionContentPreview,
    SkillResolutionPreviewRequest,
)
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.services.skills.migration_content import MigrationContent
from agent_remote_server.services.skills.migration_resolution_choices import (
    choice_spec as migration_choice_spec,
)
from agent_remote_server.services.skills.migration_resolution_choices import (
    load_choices as load_migration_choices,
)
from agent_remote_server.services.skills.migration_resolution_plan import MigrationResolutionPlanner
from agent_remote_server.services.skills.publication_context import PublicationContext
from agent_remote_server.services.skills.resolution_choices import choice_spec, load_choices
from agent_remote_server.services.skills.resolution_context import ResolutionContext
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.migration_resolution import (
    migration_resolution_unit,
    resolve_migration_conflicts,
)
from agent_remote_server.skill_manager.resolution import (
    LoadedResolutionChoice,
    choices_overlap,
    resolve_directory_conflicts,
)
from agent_remote_server.skill_manager.storage.io import run_storage_io
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy
from agent_remote_server.skill_manager.tree_diff import manifest_difference


class SkillResolutionPreviewService:
    """
    只读预览没有上传租约或幂等受理身份，人工摘要不能进入数据库引用。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        复用保存输入查询和纯算法，绝不调用发布器。

        :param session (AsyncSession): 请求只读事务
        :param store (PrivateObjectStore): 已保存内容卷
        :param policy (SkillStoragePolicy): 清单资源边界
        """
        self.store = store
        self.policy = policy
        self.publications = ResolutionContext(
            PublicationContext(
                SkillLibraryRepository(session),
                SkillPublicationRepository(session),
                SkillRuntimeRepository(session),
                SkillContentService(session, store, policy),
            ),
            SkillResolutionRepository(session),
            SkillLocalRepository(session),
        )
        self.migrations = MigrationResolutionPlanner(session, store, policy)

    async def publication(
        self, user_id: UUID, conflict_id: UUID, request: SkillResolutionPreviewRequest
    ) -> SkillResolutionContentPreview:
        """
        读取同版本保存选择，人工替换只进入本次纯清单计算。

        :param user_id (UUID): 认证所有者
        :param conflict_id (UUID): 原始会话发布身份
        :param request (SkillResolutionPreviewRequest): 上传前人工清单
        :return SkillResolutionContentPreview: 非发布授权的完整元数据预览
        """
        context = self.publications
        await context.publication.library.read_library(user_id)
        row = await context.require(user_id, conflict_id)
        if row.status != "conflicted":
            raise SkillContentError("CONFLICT_NOT_ACTIVE", "conflict is no longer active")
        plan = await context.repository.plan(row)
        revision = plan.revision if plan else 0
        _revision(request, revision)
        self._limits(request)
        inputs = await context.load(row)
        if inputs.stale_reason is not None:
            raise SkillContentError(
                "STATE_PRECONDITION_CHANGED",
                "comparison requires recomputation",
                details={"reasons": [inputs.stale_reason]},
            )
        saved = [choice_spec(item) for item in await context.repository.choices(row)]
        choices = _remaining_choices(saved, request.choice, inputs.names)
        loaded = await load_choices(context.publication.content, self.store, user_id, choices)
        loaded.append(LoadedResolutionChoice(request.choice, request.manifest))
        result = await _calculate(
            partial(
                resolve_directory_conflicts,
                inputs.base,
                inputs.current,
                inputs.incoming,
                inputs.names,
                inputs.conflicts,
                loaded,
            )
        )
        assert inputs.directory is not None and inputs.directory.head_checkpoint_id is not None
        checkpoint = await context.publication.runtime.checkpoint(
            user_id, row.account_id, inputs.directory.head_checkpoint_id
        )
        if checkpoint is None or not checkpoint.retained or checkpoint.tree_digest is None:
            raise SkillContentError("STATE_EXPIRED", "saved directory is not retained")
        directory = await context.publication.content.read_tree(
            user_id, "state", checkpoint.tree_digest
        )
        return SkillResolutionContentPreview(
            kind="publication",
            conflict_id=conflict_id,
            account_id=row.account_id,
            plan_revision=revision,
            proposed_tree_digest=manifest_digest(request.manifest),
            choices=[*choices, request.choice],
            candidate_complete=result.merged is not None,
            result_tree_digest=manifest_digest(result.merged) if result.merged else None,
            remaining=result.conflicts,
            unit=(),
            current_tree_digest=manifest_digest(inputs.current),
            directory_tree_digest=manifest_digest(directory),
            changes=manifest_difference(directory, result.merged, None) if result.merged else None,
            target_revision_id=None,
            target_modified=None,
            target_changes=None,
            original_changes=None,
        )

    async def migration(
        self, user_id: UUID, conflict_id: UUID, request: SkillResolutionPreviewRequest
    ) -> SkillResolutionContentPreview:
        """
        迁移预览使用固定四侧和原目标版本，不把人工元数据授予关联写入权限。

        :param user_id (UUID): 认证所有者
        :param conflict_id (UUID): 原始迁移身份
        :param request (SkillResolutionPreviewRequest): 上传前完整人工清单
        :return SkillResolutionContentPreview: 完整覆盖和仍待验证内容
        """
        planner = self.migrations
        row = await planner.conflicts.require(user_id, conflict_id)
        if row.status != "conflicted":
            raise SkillContentError("CONFLICT_NOT_ACTIVE", "migration is no longer active")
        plan = await planner.repository.plan(row)
        revision = plan.revision if plan else 0
        _revision(request, revision)
        self._limits(request)
        info = await planner.conflicts.info(user_id, conflict_id)
        if info.live.recomputation_reasons:
            raise SkillContentError(
                "STATE_PRECONDITION_CHANGED",
                "comparison requires recomputation",
                details={"reasons": list(info.live.recomputation_reasons)},
            )
        inputs = await planner.inputs(info, user_id)
        saved = [migration_choice_spec(item) for item in await planner.repository.choices(row)]
        choices = _remaining_choices(saved, request.choice, set(inputs.names))
        loaded = await load_migration_choices(
            planner.conflicts.queries.content, self.store, row, planner.repository, choices
        )
        loaded.append(LoadedResolutionChoice(request.choice, request.manifest))
        result = await _calculate(partial(resolve_migration_conflicts, inputs, loaded))
        original, _ = await MigrationContent(planner.conflicts.queries, self.store).original(
            user_id, row.installation_id, info.live.target.revision_id, info.name
        )
        changes = manifest_difference(original, result.merged, info.name) if result.merged else None
        return SkillResolutionContentPreview(
            kind="migration",
            conflict_id=conflict_id,
            account_id=row.account_id,
            plan_revision=revision,
            proposed_tree_digest=manifest_digest(request.manifest),
            choices=[*choices, request.choice],
            candidate_complete=result.merged is not None,
            result_tree_digest=manifest_digest(result.merged) if result.merged else None,
            remaining=result.conflicts,
            unit=migration_resolution_unit(inputs),
            current_tree_digest=manifest_digest(inputs.current),
            directory_tree_digest=manifest_digest(inputs.directory),
            changes=manifest_difference(inputs.directory, result.merged, None)
            if result.merged
            else None,
            target_revision_id=info.live.target.revision_id,
            target_modified=bool(changes) if changes is not None else None,
            target_changes=manifest_difference(inputs.current, result.merged, info.name)
            if result.merged
            else None,
            original_changes=changes,
        )

    def _limits(self, request: SkillResolutionPreviewRequest) -> None:
        """
        清单预览同样限制声明内容量，用户总额度和实际对象仍由上传与发布检查。

        :param request (SkillResolutionPreviewRequest): 人工清单
        """
        try:
            self.policy.validate_manifest(request.manifest, "account_directory")
        except ValueError as error:
            raise SkillContentError("QUOTA_EXCEEDED", "custom manifest exceeds limits") from error


def _revision(request: SkillResolutionPreviewRequest, actual: int) -> None:
    """
    预览不能静默覆盖并发保存的解决计划。

    :param request (SkillResolutionPreviewRequest): 请求预期
    :param actual (int): 当前保存计划版本
    """
    if request.expected_revision != actual:
        raise SkillContentError(
            "PLAN_REVISION_CONFLICT",
            "resolution plan changed",
            details={"expected": request.expected_revision, "current": actual},
        )


def _remaining_choices(
    saved: list[SkillResolutionChoice], replacement: SkillResolutionChoice, names: set[str]
) -> list[SkillResolutionChoice]:
    """
    只在内存排除重叠选择，不修改原持久计划和内容授权。

    :param saved (list[SkillResolutionChoice]): 保存选择
    :param replacement (SkillResolutionChoice): 本次拟议选择
    :param names (set[str]): 授权输入确定的原名称集合
    :return list[SkillResolutionChoice]: 仍需按原内容授权加载的选择
    """
    return [choice for choice in saved if not choices_overlap(choice, replacement, names)]


async def _calculate(work: partial[SkillMergeResult]) -> SkillMergeResult:
    """
    大清单纯计算在线程执行，不能阻塞异步网络和数据库任务。

    :param work (partial[SkillMergeResult]): 不持有数据库调用的纯清单算法
    :return SkillMergeResult: 元数据候选或剩余冲突
    """
    try:
        return await run_storage_io(work)
    except ValueError as error:
        raise SkillContentError("INVALID_RESOLUTION", str(error)) from error
