"""
以真实 PostgreSQL 阻塞关系验证迁移档案与新账户写入共用同一用户事务锁。
"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_gc_races import require_postgres
from test_skill_content_service import database as database
from test_skill_content_service import user

from agent_remote_server.errors import ApiError
from agent_remote_server.models import ToolAccount, ToolAccountProfile
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.services.runtime_migrations import require_runtime_migration_settled


@pytest.mark.parametrize("commit", [True, False])
async def test_migration_admission_waits_and_refreshes_cached_profile(
    database: async_sessionmaker[AsyncSession], commit: bool
) -> None:
    """
    缓存的旧成功不能绕过已提交的新迁移，回滚则不留下永久准入阻断。

    :param database (async_sessionmaker[AsyncSession]): 独立 PostgreSQL 事务工厂
    :param commit (bool): 第一连接是否提交新的迁移档案
    """
    await require_postgres(database)
    owner, account_id = await user(database), uuid4()
    async with database.begin() as session:
        session.add(
            ToolAccount(
                id=account_id,
                user_id=owner,
                tool_type="claude",
                display_name="迁移锁验证",
                status="active",
                region_code="US",
                timezone="UTC",
                locale="en-US",
                preferred_node_tags=[],
                runtime_backend="native",
            )
        )
        await session.flush()
        session.add(
            ToolAccountProfile(
                tool_account_id=account_id,
                tool_type="claude",
                profile_json={"runtime_migration": {"status": "succeeded"}},
            )
        )
    async with database() as reader, database() as writer:
        cached = await reader.scalar(
            select(ToolAccountProfile).where(
                ToolAccountProfile.tool_account_id == account_id,
            )
        )
        assert cached is not None
        await reader.commit()
        await SkillStorageRepository(writer).lock_usage(owner)
        profile = await writer.scalar(
            select(ToolAccountProfile).where(
                ToolAccountProfile.tool_account_id == account_id,
            )
        )
        assert profile is not None
        profile.profile_json = {"runtime_migration": {"status": "pending"}}
        await writer.flush()
        ready = asyncio.Event()
        process: list[int] = []

        async def admit() -> None:
            """
            用旧 ORM 缓存和新的真实连接事务请求账户写入准入。
            """
            process.append(int(await reader.scalar(select(func.pg_backend_pid())) or 0))
            ready.set()
            await require_runtime_migration_settled(reader, owner, account_id)

        contender = asyncio.create_task(admit())
        try:
            await asyncio.wait_for(ready.wait(), 3)
            async with database() as observer:
                blocked = False
                for _ in range(100):
                    if await observer.scalar(select(func.pg_blocking_pids(process[0]))):
                        blocked = True
                        break
                    await asyncio.sleep(0.01)
                assert blocked and not contender.done()
            if commit:
                await writer.commit()
                with pytest.raises(ApiError) as error:
                    await asyncio.wait_for(contender, 3)
                assert error.value.code == "RUNTIME_MIGRATION_PENDING"
                assert cached.profile_json == {"runtime_migration": {"status": "pending"}}
            else:
                await writer.rollback()
                await asyncio.wait_for(contender, 3)
                assert cached.profile_json == {"runtime_migration": {"status": "succeeded"}}
        finally:
            await writer.rollback()
            if not contender.done():
                contender.cancel()
            await asyncio.gather(contender, return_exceptions=True)
            await reader.rollback()
