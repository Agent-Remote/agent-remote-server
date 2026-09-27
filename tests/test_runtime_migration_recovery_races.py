"""
以独立 PostgreSQL 连接验证恢复受理串行和租约授权刷新。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_gc_races import require_postgres
from test_skill_content_service import database as database
from test_skill_content_service import user

from agent_remote_server.errors import ApiError
from agent_remote_server.models import Node, NodeTask, ToolAccount, ToolAccountProfile, User
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.runtime_recovery import (
    RuntimeRecoveryAuthorization,
    RuntimeRecoveryRequest,
)
from agent_remote_server.services.runtime_recovery import RuntimeRecoveryService


async def seed(database: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID, UUID, str]:
    """
    建立隔离的终态原迁移，后续恢复全部通过生产服务受理。

    :param database (async_sessionmaker[AsyncSession]): 独立连接工厂
    :return tuple[UUID, UUID, UUID, str]: 用户、账户、节点和原逻辑任务
    """
    owner, account_id, node_id = await user(database), uuid4(), uuid4()
    original = f"migrate_tool_account_runtime:{account_id}:{uuid4()}"
    async with database.begin() as session:
        session.add(Node(id=node_id, name=str(node_id), status="healthy", region_code="US"))
        await session.flush()
        session.add(
            ToolAccount(
                id=account_id,
                user_id=owner,
                tool_type="claude",
                display_name="测试",
                status="migrating",
                region_code="US",
                timezone="UTC",
                locale="en-US",
                affinity_node_id=node_id,
                runtime_backend="docker_sandbox",
            )
        )
        await session.flush()
        session.add(
            NodeTask(
                node_id=node_id,
                task_id=original,
                task_type="migrate_tool_account_runtime",
                status="failed",
                retry_count=1,
                payload={
                    "user_id": str(owner),
                    "tool_account_id": str(account_id),
                    "tool_type": "claude",
                    "source_runtime_backend": "docker_sandbox",
                    "target_runtime_backend": "native",
                },
            )
        )
        session.add(
            ToolAccountProfile(
                tool_account_id=account_id,
                tool_type="claude",
                profile_json={
                    "runtime_migration": {
                        "task_id": original,
                        "status": "recovery_required",
                        "previous_status": "active",
                        "source_runtime_backend": "docker_sandbox",
                        "target_runtime_backend": "native",
                    }
                },
            )
        )
    return owner, account_id, node_id, original


async def wait_blocked(database: async_sessionmaker[AsyncSession], process: int) -> None:
    """
    观察真实锁等待，避免用固定睡眠推测并发交错。

    :param database (async_sessionmaker[AsyncSession]): 观察者连接工厂
    :param process (int): 被观察的数据库后端进程
    """
    async with database() as observer:
        for _ in range(100):
            if await observer.scalar(select(func.pg_blocking_pids(process))):
                return
            await asyncio.sleep(0.01)
    raise AssertionError("recovery did not wait for the original user lock")


@pytest.mark.parametrize("same_key", [True, False])
async def test_parallel_recovery_acceptance_preserves_one_original_task(
    database: async_sessionmaker[AsyncSession], same_key: bool
) -> None:
    """
    同键得到已提交原任务，不同键等待后必须拒绝而非覆盖。

    :param database (async_sessionmaker[AsyncSession]): 独立连接工厂
    :param same_key (bool): 并发请求是否使用相同键
    """
    await require_postgres(database)
    owner, account, _, original = await seed(database)
    key = uuid4()
    async with database() as first, database() as second:
        actor = await first.get(User, owner)
        other_actor = await second.get(User, owner)
        assert actor is not None and other_actor is not None
        accepted = await RuntimeRecoveryService(first).submit(
            actor, account, RuntimeRecoveryRequest(original_task_id=original, request_id=key)
        )
        process = int(await second.scalar(select(func.pg_backend_pid())) or 0)
        contender = asyncio.create_task(
            RuntimeRecoveryService(second).submit(
                other_actor,
                account,
                RuntimeRecoveryRequest(
                    original_task_id=original, request_id=key if same_key else uuid4()
                ),
            )
        )
        try:
            await wait_blocked(database, process)
            assert not contender.done()
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


@pytest.mark.parametrize("commit", [True, False])
@pytest.mark.parametrize("renew", [False, True])
async def test_recovery_authorization_refreshes_profile_after_lock(
    database: async_sessionmaker[AsyncSession], commit: bool, renew: bool
) -> None:
    """
    缓存授权等待后读取新的档案，提交撤销拒绝、回滚继续允许。

    :param database (async_sessionmaker[AsyncSession]): 独立连接工厂
    :param commit (bool): 是否提交另一事务的原档案变更
    :param renew (bool): 是否同时尝试延长原恢复租约
    """
    await require_postgres(database)
    owner, account, node, original = await seed(database)
    async with database.begin() as session:
        actor = await session.get(User, owner)
        assert actor is not None
        accepted = await RuntimeRecoveryService(session).submit(
            actor, account, RuntimeRecoveryRequest(original_task_id=original, request_id=uuid4())
        )
        task = await session.get(NodeTask, accepted.binding.task_record_id)
        assert task is not None
        task.status, task.retry_count = "leased", 1
        task.lease_until = datetime.now(UTC) + timedelta(minutes=5)
    async with database() as reader, database() as writer:
        service = RuntimeRecoveryService(reader)
        grant = await service.authorize(node, accepted.binding.task_id)
        await reader.commit()
        await SkillStorageRepository(writer).lock_usage(owner)
        profile = await writer.scalar(
            select(ToolAccountProfile).where(ToolAccountProfile.tool_account_id == account)
        )
        assert profile is not None
        profile.profile_json = {"runtime_migration": {"task_id": "newer", "status": "pending"}}
        await writer.flush()
        process = int(await reader.scalar(select(func.pg_backend_pid())) or 0)

        async def check() -> RuntimeRecoveryAuthorization:
            """
            通过只读授权或续租再次校验锁后的原档案。

            :return RuntimeRecoveryAuthorization: 校验成功的完整原授权
            """
            if renew:
                lease = await service.renew(node, accepted.binding.task_id, grant, 30)
                return lease.authorization
            return await service.authorize(node, accepted.binding.task_id)

        contender = asyncio.create_task(check())
        try:
            await wait_blocked(database, process)
            if commit:
                await writer.commit()
                with pytest.raises(ApiError) as error:
                    await asyncio.wait_for(contender, 3)
                assert error.value.code == "RUNTIME_MIGRATION_RECOVERY_CONFLICT"
            else:
                await writer.rollback()
                assert await asyncio.wait_for(contender, 3) == grant
        finally:
            await writer.rollback()
            if not contender.done():
                contender.cancel()
            await asyncio.gather(contender, return_exceptions=True)
            await reader.rollback()
