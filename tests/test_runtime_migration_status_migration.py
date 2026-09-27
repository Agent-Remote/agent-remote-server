"""
在真实 PostgreSQL 验证迁移中账户状态的无损升级与保护性降级。
"""

import runpy
from collections.abc import Callable
from typing import cast
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_gc_races import require_postgres
from test_skill_content_service import database as database


def migrate_status(connection: Connection, direction: str) -> None:
    """
    在隔离模式中执行正式状态约束迁移。

    :param connection (Connection): 当前真实连接
    :param direction (str): 正式迁移函数名
    """
    migration = runpy.run_path("migrations/versions/0056_runtime_migration_status.py")
    with Operations.context(MigrationContext.configure(connection)):
        cast(Callable[[], None], migration[direction])()


async def test_runtime_migration_status_upgrade_and_guarded_downgrade(
    database: async_sessionmaker[AsyncSession],
) -> None:
    """
    既有状态与行保持不变，存在迁移中账户时拒绝降级且保留新约束。

    :param database (async_sessionmaker[AsyncSession]): 独立数据库连接工厂
    """
    await require_postgres(database)
    previous = runpy.run_path("migrations/versions/0005_tool_account_binding.py")["NEW_STATES"]
    schema = "runtime_migration_" + uuid4().hex
    async with database() as session:
        connection = await session.connection()
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await connection.execute(
            text(
                "CREATE TABLE tool_accounts (id integer PRIMARY KEY, status text NOT NULL, "
                f"CONSTRAINT tool_accounts_status_ck CHECK (status in ({previous})))"
            )
        )
        await connection.execute(
            text("INSERT INTO tool_accounts VALUES (1, 'active'), (2, 'disabled')")
        )
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(text("INSERT INTO tool_accounts VALUES (3, 'migrating')"))
        await connection.run_sync(migrate_status, "upgrade")
        await connection.execute(text("INSERT INTO tool_accounts VALUES (3, 'migrating')"))
        rows = (
            await connection.execute(text("SELECT id, status FROM tool_accounts ORDER BY id"))
        ).all()
        with pytest.raises(RuntimeError, match="cannot downgrade"):
            await connection.run_sync(migrate_status, "downgrade")
        assert (
            await connection.execute(text("SELECT id, status FROM tool_accounts ORDER BY id"))
        ).all() == rows
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(text("INSERT INTO tool_accounts VALUES (4, 'invalid')"))
        await connection.execute(text("DELETE FROM tool_accounts WHERE id = 3"))
        await connection.run_sync(migrate_status, "downgrade")
        assert (
            await connection.execute(text("SELECT id, status FROM tool_accounts ORDER BY id"))
        ).all() == rows[:2]
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(text("INSERT INTO tool_accounts VALUES (3, 'migrating')"))
        await connection.run_sync(migrate_status, "upgrade")
        await connection.execute(text("INSERT INTO tool_accounts VALUES (3, 'migrating')"))
        assert (
            await connection.execute(text("SELECT id, status FROM tool_accounts ORDER BY id"))
        ).all() == rows
        await session.rollback()
