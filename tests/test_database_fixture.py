"""
验证数据库模板保留完整结构、真实外键和跨用例隔离。
"""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_service import database, user

from agent_remote_server.db import Base
from agent_remote_server.models import User

__all__ = ["database"]


@pytest.mark.parametrize("iteration", [1, 2])
async def test_database_copies_start_empty_and_keep_foreign_keys(
    database: async_sessionmaker[AsyncSession], iteration: int
) -> None:
    """
    每个副本均从空库开始，并在独立连接中启用真实外键。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param iteration (int): 重复用例编号
    """
    if os.environ.get("SKILL_TEST_DATABASE_URL"):
        pytest.skip("SQLite template regression requires a private fixture")
    async with database() as session:
        if session.bind is None or session.bind.dialect.name != "sqlite":
            pytest.skip("SQLite template regression")
        assert await session.scalar(text("PRAGMA foreign_keys")) == 1
        assert await session.scalar(select(func.count()).select_from(User)) == 0
    await user(database)
    async with database() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 1
        await session.execute(text(f"CREATE TABLE isolated_{iteration} (id INTEGER PRIMARY KEY)"))
        await session.commit()


def test_schema_template_remains_empty_and_complete(sqlite_schema: Path) -> None:
    """
    模板包含全部模型表，且任何测试副本的写入均不污染模板。

    :param sqlite_schema (Path): 会话空数据库模板
    """
    with closing(sqlite3.connect(sqlite_schema)) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert tables == set(Base.metadata.tables)
        for table in tables:
            assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (0,)
