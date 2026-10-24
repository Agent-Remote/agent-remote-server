"""
使用独立数据库连接验证审核投影在真实原用户锁之后比较。
"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_runtime_migration_recovery_races import seed, wait_blocked
from test_skill_content_gc_races import require_postgres
from test_skill_content_service import database as database

from agent_remote_server.errors import ApiError
from agent_remote_server.models import NodeTask, User
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.reviewed_runtime_recovery import ReviewedRecoveryRequest
from agent_remote_server.services.reviewed_runtime_recovery import ReviewedRuntimeRecoveryService


@pytest.mark.parametrize("same_key", [True, False])
async def test_reviewed_parallel_acceptance_preserves_one_task(
    database: async_sessionmaker[AsyncSession], same_key: bool
) -> None:
    """
    两个已审核请求争用原用户锁，同键重放而不同键不能覆盖当前恢复。

    :param database (async_sessionmaker[AsyncSession]): 独立连接工厂
    :param same_key (bool): 是否复用同一请求键
    """
    await require_postgres(database)
    owner, account, _, original = await seed(database)
    async with database() as first, database() as second:
        actor = await first.get(User, owner)
        other = await second.get(User, owner)
        assert actor is not None and other is not None
        service = ReviewedRuntimeRecoveryService(first)
        projection = (await service.inspect(account, original)).original
        request = ReviewedRecoveryRequest(
            schema_version=1,
            request_id=str(uuid4()),
            action="verify_target",
            expected_original=projection,
        )
        accepted = await service.submit(actor, account, request)
        competing = request if same_key else request.model_copy(update={"request_id": str(uuid4())})
        process = int(await second.scalar(select(func.pg_backend_pid())) or 0)
        contender = asyncio.create_task(
            ReviewedRuntimeRecoveryService(second).submit(other, account, competing)
        )
        try:
            await wait_blocked(database, process)
            await first.commit()
            if same_key:
                assert await asyncio.wait_for(contender, 3) == accepted
            else:
                with pytest.raises(ApiError) as error:
                    await asyncio.wait_for(contender, 3)
                assert error.value.code == "RUNTIME_MIGRATION_RECOVERY_CONFLICT"
        finally:
            await first.rollback()
            if not contender.done():
                contender.cancel()
            await asyncio.gather(contender, return_exceptions=True)
            await second.rollback()


async def test_reviewed_original_identity_refreshes_after_writer_lock(
    database: async_sessionmaker[AsyncSession],
) -> None:
    """
    预览后原记录身份变化在锁内重新读取，只返回审核变化且不创建新任务。

    :param database (async_sessionmaker[AsyncSession]): 独立连接工厂
    """
    await require_postgres(database)
    owner, account, _, original_id = await seed(database)
    async with database() as reviewer, database() as writer:
        service = ReviewedRuntimeRecoveryService(reviewer)
        projection = (await service.inspect(account, original_id)).original
        actor = await reviewer.get(User, owner)
        assert actor is not None
        await reviewer.commit()
        await SkillStorageRepository(writer).lock_usage(owner)
        original = await writer.scalar(select(NodeTask).where(NodeTask.task_id == original_id))
        assert original is not None
        original.id = uuid4()
        await writer.flush()
        request = ReviewedRecoveryRequest(
            schema_version=1,
            request_id=str(uuid4()),
            action="repair_source",
            expected_original=projection,
        )
        process = int(await reviewer.scalar(select(func.pg_backend_pid())) or 0)
        contender = asyncio.create_task(service.submit(actor, account, request))
        try:
            await wait_blocked(database, process)
            await writer.commit()
            with pytest.raises(ApiError) as error:
                await asyncio.wait_for(contender, 3)
            assert error.value.code == "RUNTIME_MIGRATION_RECOVERY_REVIEW_CHANGED"
        finally:
            await writer.rollback()
            if not contender.done():
                contender.cancel()
            await asyncio.gather(contender, return_exceptions=True)
            await reviewer.rollback()
        async with database() as observer:
            assert (
                await observer.scalar(
                    select(func.count())
                    .select_from(NodeTask)
                    .where(
                        NodeTask.task_type == "recover_tool_account_runtime",
                        NodeTask.task_id.like(f"recover_tool_account_runtime:{account}:%"),
                    )
                )
                == 0
            )
