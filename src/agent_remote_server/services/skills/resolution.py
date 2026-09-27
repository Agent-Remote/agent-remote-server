"""
原子保存用户解决计划、预览完整结果或发布已明确解决的目录。
"""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_resolution import (
    SkillResolutionOperation,
    SkillResolutionPlan,
)
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.repositories.skill_resolution import SkillResolutionRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.schemas.skill_conflicts import SkillResolutionView
from agent_remote_server.schemas.skill_resolution import SkillResolutionRequest
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.services.skills.local_candidates import LocalSkillCandidateService
from agent_remote_server.services.skills.publication import SkillPublicationService
from agent_remote_server.services.skills.publication_context import PublicationContext
from agent_remote_server.services.skills.resolution_apply import ResolutionApply
from agent_remote_server.services.skills.resolution_choices import (
    choice_row,
    choice_spec,
    load_choices,
)
from agent_remote_server.services.skills.resolution_context import ResolutionContext
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.resolution import (
    choices_overlap,
    resolve_directory_conflicts,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillResolutionService:
    """
    命令保存点保护所有引用，外层请求仅在结果返回后提交。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        共享存储锁、内容授权和已有发布路径。

        :param session (AsyncSession): 请求事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 部署额度
        """
        self._session = session
        self._store = store
        self._context = ResolutionContext(
            PublicationContext(
                SkillLibraryRepository(session),
                SkillPublicationRepository(session),
                SkillRuntimeRepository(session),
                SkillContentService(session, store, policy),
            ),
            SkillResolutionRepository(session),
            SkillLocalRepository(session),
        )
        self._apply = ResolutionApply(
            self._context, store, policy, LocalSkillCandidateService(session, store, policy)
        )
        self._publisher = SkillPublicationService(session, store, policy)

    async def execute(
        self, user_id: UUID, publication_id: UUID, request: SkillResolutionRequest
    ) -> SkillResolutionView:
        """
        幂等重放优先于计划版本检查；预览不保存选择、回执或替代尝试。

        :param user_id (UUID): 当前认证用户
        :param publication_id (UUID): 原始目录冲突身份
        :param request (SkillResolutionRequest): 明确范围和预期计划版本
        :return SkillResolutionView: 计划保存、预览、发布或目标重算结果
        """
        raw = {"publication_id": str(publication_id), "request": request.model_dump(mode="json")}
        digest = hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        async with retention_mutation(self._session, user_id, read_only=request.dry_run):
            await self._context.publication.library.lock_library(user_id)
            publication = await self._context.require(user_id, publication_id)
            if not request.dry_run:
                previous = await self._context.repository.operation(
                    user_id, request.idempotency_key
                )
                if previous is not None:
                    if previous.request_digest != digest:
                        raise SkillContentError(
                            "IDEMPOTENCY_CONFLICT", "key already belongs to another resolution"
                        )
                    return SkillResolutionView.model_validate(previous.response_json)
            if publication.content_retired_at is not None:
                raise SkillContentError("STATE_EXPIRED", "publication comparison has expired")
            invalidated = publication.status == "superseded" and publication.reason in {
                "state_reset",
                "state_restore",
            }
            if publication.status != "conflicted" and not invalidated:
                raise SkillContentError(
                    "CONFLICT_NOT_ACTIVE", "attempt is already published or superseded"
                )
            plan = await self._context.repository.plan(publication)
            revision = plan.revision if plan is not None else 0
            if revision != request.expected_revision:
                raise SkillContentError(
                    "PLAN_REVISION_CONFLICT",
                    "resolution plan changed",
                    details={"expected": request.expected_revision, "current": revision},
                )
            inputs = await self._context.load(publication)
            if invalidated:
                inputs.stale_reason = publication.reason
            saved = list(await self._context.repository.choices(publication))
            if inputs.stale_reason is not None:
                replacement = (
                    None
                    if request.dry_run
                    else await self._publisher.recompute(user_id, publication_id)
                )
                view = SkillResolutionView(
                    publication_id=publication_id,
                    operation_id=None,
                    status="preview" if request.dry_run else "superseded",
                    plan_revision=revision,
                    ready=False,
                    choices=[choice_spec(row) for row in saved],
                    remaining=inputs.conflicts,
                    result_tree_digest=None,
                    result_checkpoint_id=None,
                    replacement_id=replacement.id if replacement is not None else None,
                    stale_reason=inputs.stale_reason,
                )
            else:
                removed = [
                    row
                    for row in saved
                    if choices_overlap(choice_spec(row), request.choice, inputs.names)
                ]
                choices = [choice_spec(row) for row in saved if row not in removed] + [
                    request.choice
                ]
                try:
                    loaded = await load_choices(
                        self._context.publication.content, self._store, user_id, choices
                    )
                    result = resolve_directory_conflicts(
                        inputs.base,
                        inputs.current,
                        inputs.incoming,
                        inputs.names,
                        inputs.conflicts,
                        loaded,
                    )
                    targets = (
                        await self._apply.validate(inputs, result.merged, choices)
                        if result.merged is not None
                        else None
                    )
                except FileNotFoundError as error:
                    raise SkillContentError(
                        "CONTENT_INCOMPLETE", "resolution content is not available"
                    ) from error
                except ValueError as error:
                    if isinstance(error, SkillContentError):
                        raise
                    raise SkillContentError("INVALID_RESOLUTION", str(error)) from error
                checkpoint_id = None
                if not request.dry_run:
                    if plan is None:
                        plan = SkillResolutionPlan(
                            publication_id=publication_id,
                            user_id=user_id,
                            account_id=publication.account_id,
                            revision=0,
                        )
                        self._context.publication.runtime.add(plan)
                        await self._context.publication.runtime.flush()
                    for row in removed:
                        await self._context.repository.remove_choice(row)
                    await self._context.publication.runtime.flush()
                    self._context.publication.runtime.add(choice_row(publication, request.choice))
                    plan.revision += 1
                    revision = plan.revision
                    await self._context.publication.runtime.flush()
                    if result.merged is not None:
                        assert targets is not None
                        checkpoint = await self._apply.publish(
                            inputs, result.merged, targets, revision
                        )
                        checkpoint_id = checkpoint.id
                view = SkillResolutionView(
                    publication_id=publication_id,
                    operation_id=None,
                    status="preview"
                    if request.dry_run
                    else ("published" if checkpoint_id else "pending"),
                    plan_revision=revision,
                    ready=result.merged is not None,
                    choices=choices,
                    remaining=result.conflicts,
                    result_tree_digest=manifest_digest(result.merged)
                    if result.merged is not None
                    else None,
                    result_checkpoint_id=checkpoint_id,
                    replacement_id=None,
                    stale_reason=None,
                )
            if not request.dry_run:
                view.operation_id = uuid4()
                self._context.publication.runtime.add(
                    SkillResolutionOperation(
                        id=view.operation_id,
                        user_id=user_id,
                        account_id=publication.account_id,
                        publication_id=publication_id,
                        idempotency_key=request.idempotency_key,
                        request_digest=digest,
                        plan_revision=view.plan_revision,
                        response_json=view.model_dump(mode="json"),
                    )
                )
                await self._context.publication.runtime.flush()
            return view
