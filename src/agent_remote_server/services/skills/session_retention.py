"""
在删除会话展示之前验证运行内容已完整保留，并只释放关系引用。
"""

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models import Session
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.retention.clocks import retention_mutation


async def release_retained_session_reference(session: AsyncSession, tool_session: Session) -> None:
    """
    待上传或待发布输入阻止删除；保留历史始终保存来源身份和内容引用。

    :param session (AsyncSession): 外层删除事务
    :param tool_session (Session): 已通过既有删除资格检查的会话
    """
    storage = SkillStorageRepository(session)
    if await storage.lock_existing_usage(tool_session.user_id) is None:
        return
    async with retention_mutation(session, tool_session.user_id):
        await storage.lock_usage(tool_session.user_id)
        runtime = SkillRuntimeRepository(session)
        locked = await runtime.session(tool_session.user_id, tool_session.id)
        if locked is None or locked.status not in {"stopped", "interrupted", "failed"}:
            raise SkillContentError(
                "SESSION_STATE_CHANGED", "session state changed before deletion"
            )
        snapshot = await runtime.snapshot_for_session(tool_session.user_id, tool_session.id)
        if snapshot is None:
            return
        finalization = await runtime.finalization(snapshot)
        if (
            snapshot.status != "retained"
            or finalization is None
            or finalization.status not in {"published", "conflicted", "detached"}
            or finalization.tree_digest is None
            or finalization.checkpoint_id is None
        ):
            raise SkillContentError("STATE_PENDING", "session skill state is not fully retained")
        snapshot.session_id = None
        await runtime.flush()
