"""
保存后台准备完整目录并验证重试输入，不改变账户当前 head 或有效使用。
"""

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_state import SkillCheckpoint, SkillDirectoryMember
from agent_remote_server.repositories.skill_deployment_tasks import SkillDeploymentTaskRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.services.skills.account_materialization import AccountMaterialization
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_context import DeploymentAuthority


async def save_input(
    session: AsyncSession, authority: DeploymentAuthority, materialization: AccountMaterialization
) -> SkillCheckpoint:
    """
    独立保存完整物化树和成员，调用方将其与任务绑定原子提交。

    :param session (AsyncSession): 用户锁内保存点
    :param authority (DeploymentAuthority): 原始配置授权
    :param materialization (AccountMaterialization): 完整目录组装服务
    :return SkillCheckpoint: 非当前 head 的独立部署输入
    """
    prepared = await materialization.prepare(authority.account)
    identity = uuid4()
    upload = await materialization.content.begin(
        authority.account.user_id, f"deployment:{identity}", prepared.manifest, "account_directory"
    )
    stored = await materialization.content.complete(authority.account.user_id, upload.id)
    checkpoint = SkillCheckpoint(
        id=identity,
        user_id=authority.account.user_id,
        account_id=authority.account.id,
        scope="directory",
        directory_epoch=prepared.epoch,
        parent_id=prepared.head.id,
        content_digest=stored.digest,
        tree_digest=stored.digest,
    )
    session.add(checkpoint)
    await session.flush()
    for item in prepared.selected:
        session.add(
            SkillDirectoryMember(
                user_id=authority.account.user_id,
                account_id=authority.account.id,
                directory_checkpoint_id=identity,
                entry_name=item.name,
                state_id=item.state.id,
                checkpoint_id=item.checkpoint.id,
            )
        )
    await session.flush()
    return checkpoint


async def validate_input(
    session: AsyncSession, authority: DeploymentAuthority, binding: SkillDeploymentTask
) -> SkillCheckpoint:
    """
    当前 head 可前进，但目录纪元、分支纪元与原始选择不能被新值代替。

    :param session (AsyncSession): 已持有用户锁的事务
    :param authority (DeploymentAuthority): 当前有效原计划
    :param binding (SkillDeploymentTask): 保存的精确输入
    :return SkillCheckpoint: 仍保留且未被显式状态命令失效的输入
    """
    user_id, account_id = authority.account.user_id, authority.account.id
    runtime = SkillRuntimeRepository(session)
    checkpoint = await runtime.checkpoint(user_id, account_id, binding.checkpoint_id)
    directory = await runtime.directory(user_id, account_id)
    if (
        binding.user_id != user_id
        or binding.account_id != account_id
        or binding.operation_id != authority.operation.id
        or binding.node_id != authority.node.id
        or binding.plan_digest != authority.plan.digest()
        or checkpoint is None
        or not checkpoint.retained
        or checkpoint.scope != "directory"
        or checkpoint.tree_digest != binding.content_digest
        or directory is None
        or directory.mode != "managed_v1"
        or checkpoint.directory_epoch != directory.epoch
    ):
        raise SkillContentError("DEPLOYMENT_INPUT_CHANGED", "deployment directory input is invalid")
    sources = {source.name: source for source in authority.plan.sources if source.enabled}
    members = await runtime.members(checkpoint)
    if {member.entry_name for member in members} != sources.keys():
        raise SkillContentError("DEPLOYMENT_INPUT_CHANGED", "deployment member inventory changed")
    repository = SkillDeploymentTaskRepository(session)
    for member in members:
        source = sources[member.entry_name]
        branch = await repository.branch(user_id, account_id, member.state_id)
        item = await runtime.checkpoint(user_id, account_id, member.checkpoint_id)
        if (
            branch is None
            or branch.expired
            or item is None
            or not item.retained
            or item.tree_digest is None
            or item.state_id != member.state_id
            or item.state_epoch != branch.epoch
            or (
                source.origin == "library"
                and (
                    branch.installation_id != source.source_id
                    or branch.base_revision_id != source.revision_id
                    or branch.installation_epoch != source.installation_epoch
                )
            )
            or (
                source.origin == "account_local"
                and (
                    branch.local_skill_id != source.source_id
                    or branch.local_revision_id != source.revision_id
                )
            )
        ):
            raise SkillContentError(
                "DEPLOYMENT_INPUT_CHANGED", "deployment branch input is invalid"
            )
    return checkpoint
