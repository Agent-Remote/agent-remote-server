"""
把稳定旧目录和手工技能身份一起发布为账户唯一权威，不提升为用户库安装。
"""

from uuid import uuid4

from agent_remote_server.models.skill_state import (
    AccountSkillState,
    SkillCheckpoint,
    SkillDirectoryMember,
)
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.finalization_checkpoints import (
    valid_candidate_names,
    validate_finalization_limits,
)
from agent_remote_server.services.skills.local_candidates import LocalSkillCandidateService
from agent_remote_server.services.skills.takeover_context import TakeoverContext


async def publish_takeover(
    context: TakeoverContext, receipt: SkillAccountTakeover, manifest: SkillTreeManifest
) -> SkillCheckpoint:
    """
    调用方保存点内创建目录、本地原始版本和分支，最后执行完整目录 CAS。

    :param context (TakeoverContext): 当前用户锁及内容事务
    :param receipt (SkillAccountTakeover): 已验证完整捕获收据
    :param manifest (SkillTreeManifest): 全部实际字节验证通过的完整目录
    :return SkillCheckpoint: 首次权威完整目录
    """
    assert receipt.capture_digest is not None
    names = await valid_candidate_names(context.store, receipt.user_id, manifest)
    validate_finalization_limits(manifest, names, context.settings.skill_storage_policy)
    directory = SkillCheckpoint(
        id=uuid4(),
        user_id=receipt.user_id,
        account_id=receipt.account_id,
        scope="directory",
        directory_epoch=receipt.directory_epoch,
        content_digest=receipt.capture_digest,
        tree_digest=receipt.capture_digest,
        invalid_skill_format=False,
    )
    context.runtime.add(directory)
    await context.runtime.flush()
    candidates = LocalSkillCandidateService(
        context.session, context.store, context.settings.skill_storage_policy
    )
    for name in sorted(names):
        candidate = await candidates.register(
            receipt.user_id, receipt.account_id, directory.id, name
        )
        assert candidate.default_revision_id is not None
        branch = AccountSkillState(
            id=uuid4(),
            user_id=receipt.user_id,
            account_id=receipt.account_id,
            local_skill_id=candidate.id,
            local_revision_id=candidate.default_revision_id,
            installation_epoch=1,
            epoch=1,
            expired=False,
        )
        context.runtime.add(branch)
        await context.runtime.flush()
        item = SkillCheckpoint(
            id=uuid4(),
            user_id=receipt.user_id,
            account_id=receipt.account_id,
            scope="item",
            state_id=branch.id,
            state_epoch=1,
            backing_directory_id=directory.id,
            subtree_prefix=name,
            content_digest=receipt.capture_digest,
            tree_digest=receipt.capture_digest,
            invalid_skill_format=False,
        )
        context.runtime.add(item)
        await context.runtime.flush()
        branch.head_checkpoint_id = item.id
        candidate.status = "active"
        context.runtime.add(
            SkillDirectoryMember(
                user_id=receipt.user_id,
                account_id=receipt.account_id,
                directory_checkpoint_id=directory.id,
                entry_name=name,
                state_id=branch.id,
                checkpoint_id=item.id,
            )
        )
    await context.runtime.flush()
    if not await context.repository.publish(receipt, directory.id):
        raise SkillContentError("HEAD_CHANGED", "account directory changed before takeover commit")
    return directory
