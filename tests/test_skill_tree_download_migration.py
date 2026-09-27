"""
在真实 PostgreSQL 验证不可用对象部分索引的无损升降级。
"""

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_tree_downloads import store_tree


def migrate_availability(connection: Connection, direction: str) -> None:
    """
    在隔离模式中执行正式索引迁移。

    :param connection (Connection): 当前真实连接
    :param direction (str): 正式升降级函数名
    """
    migration = runpy.run_path("migrations/versions/0055_skill_object_availability_index.py")
    with Operations.context(MigrationContext.configure(connection)):
        cast(Callable[[], None], migration[direction])()


async def test_postgresql_availability_index_preserves_objects(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """
    升降级仅改变索引，原始所有者、摘要、类别、大小及删除标记完全保留。

    :param database (async_sessionmaker[AsyncSession]): 隔离数据库工厂
    :param tmp_path (Path): 实际字节卷
    """
    async with database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires disposable PostgreSQL")
    owner = await user(database)
    await store_tree(database, tmp_path, owner, {"a": b"available", "b": b"retired"})
    async with database() as session:
        connection = await session.connection()
        schema = "tree_download_" + owner.hex
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await connection.execute(
            text(
                "CREATE TABLE skill_content_objects "
                "(LIKE public.skill_content_objects INCLUDING DEFAULTS INCLUDING CONSTRAINTS)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO skill_content_objects SELECT * FROM public.skill_content_objects "
                "WHERE user_id = :owner"
            ),
            {"owner": owner},
        )
        await connection.execute(
            text("UPDATE skill_content_objects SET status = 'deleting' WHERE size = 7")
        )
        original = (
            await connection.execute(text("SELECT * FROM skill_content_objects ORDER BY digest"))
        ).all()
        assert len(original) == 2 and {row.status for row in original} == {"available", "deleting"}
        await connection.run_sync(migrate_availability, "upgrade")
        definition = await connection.scalar(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = :schema "
                "AND indexname = 'skill_object_unavailable_idx'"
            ),
            {"schema": schema},
        )
        assert definition is not None and "WHERE" in definition and "available" in definition
        await connection.run_sync(migrate_availability, "downgrade")
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM pg_indexes WHERE schemaname = :schema"),
                {"schema": schema},
            )
            == 0
        )
        assert (
            await connection.execute(text("SELECT * FROM skill_content_objects ORDER BY digest"))
        ).all() == original
        await connection.run_sync(migrate_availability, "upgrade")
