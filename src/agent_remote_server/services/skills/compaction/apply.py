"""
创建等价完整目录与分支视图，并在调用方保存点中一次交换全部当前引用。
"""

from uuid import UUID, uuid4

from agent_remote_server.models.skill_state import SkillCheckpoint, SkillDirectoryMember
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.services.skills.compaction.plan import CompactionPlan, CompactionResult
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService


async def publish_compaction(
    plan: CompactionPlan,
    index: RetentionIndex,
    runtime: SkillRuntimeRepository,
    publication: SkillPublicationRepository,
    content: SkillContentService,
) -> CompactionResult:
    """
    先创建所有目录和单项身份，再写成员和执行 CAS，不删除或就地改写任何旧历史。

    :param plan (CompactionPlan): 本次用户锁内重新验证的完整计划
    :param index (RetentionIndex): 同一事务引用索引
    :param runtime (SkillRuntimeRepository): checkpoint 与成员持久化仓储
    :param publication (SkillPublicationRepository): 保留纪元的 head CAS 仓储
    :param content (SkillContentService): 已有对象验证和完整树登记入口
    :return CompactionResult: 实际创建和交换的身份，不是释放空间回执
    """
    changed = plan.changed_directories()
    directory_ids = {identity: uuid4() for identity in sorted(changed)}
    head_ids = {
        head.checkpoint_id: uuid4() for head in plan.heads if head.backing_directory_id in changed
    }
    digests: dict[UUID, str] = {}
    for directory in plan.directories:
        identity = directory.checkpoint_id
        if identity not in changed:
            continue
        upload = await content.begin(
            plan.user_id,
            f"compaction:{directory_ids[identity]}",
            directory.result,
            "account_directory",
        )
        tree = await content.complete(plan.user_id, upload.id)
        digests[identity] = tree.digest
        runtime.add(
            SkillCheckpoint(
                id=directory_ids[identity],
                user_id=plan.user_id,
                account_id=plan.account_id,
                scope="directory",
                directory_epoch=plan.directory_epoch,
                content_digest=tree.digest,
                tree_digest=tree.digest,
                parent_id=identity,
                invalid_skill_format=directory.invalid_skill_format,
            )
        )
    await runtime.flush()
    for head in plan.heads:
        if head.checkpoint_id not in head_ids:
            continue
        backing = head.backing_directory_id
        assert backing is not None
        runtime.add(
            SkillCheckpoint(
                id=head_ids[head.checkpoint_id],
                user_id=plan.user_id,
                account_id=plan.account_id,
                scope="item",
                state_id=head.state_id,
                state_epoch=head.state_epoch,
                subtree_prefix=head.subtree_prefix,
                content_digest=digests[backing],
                tree_digest=digests[backing],
                backing_directory_id=directory_ids[backing],
                parent_id=head.checkpoint_id,
                invalid_skill_format=head.invalid_skill_format,
            )
        )
    await runtime.flush()
    for directory in plan.directories:
        if directory.checkpoint_id not in changed:
            continue
        removed = {member.checkpoint_id for member in directory.removed}
        for member in directory.members:
            if member.checkpoint_id in removed:
                continue
            runtime.add(
                SkillDirectoryMember(
                    user_id=plan.user_id,
                    account_id=plan.account_id,
                    directory_checkpoint_id=directory_ids[directory.checkpoint_id],
                    entry_name=member.entry_name,
                    state_id=member.state_id,
                    checkpoint_id=head_ids.get(member.checkpoint_id, member.checkpoint_id),
                )
            )
    await runtime.flush()
    branches = {row.id: row for row in index.branches}
    for head in plan.heads:
        if head.checkpoint_id in head_ids and not await publication.advance_branch(
            branches[head.state_id], head_ids[head.checkpoint_id]
        ):
            raise SkillContentError("HEAD_CHANGED", "branch changed during directory compaction")
    directory_head = directory_ids.get(plan.directory_head_id, plan.directory_head_id)
    if directory_head != plan.directory_head_id:
        state = next(row for row in index.directories if row.account_id == plan.account_id)
        if not await publication.advance_directory(state, directory_head):
            raise SkillContentError("HEAD_CHANGED", "directory changed during compaction")
    return CompactionResult(
        directory_head_id=directory_head,
        directory_replacements=tuple(sorted(directory_ids.items())),
        head_replacements=tuple(sorted(head_ids.items())),
    )
