"""
在真实 PostgreSQL 验证索引迁移保持原始清单并拒绝跨输入声明。
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_service import database as database
from test_skill_content_service import service, user
from test_skill_storage import file_entry

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest


def migrate_index(connection: Connection, direction: str) -> None:
    """
    使用当前真实事务运行正式索引迁移。

    :param connection (Connection): 隔离模式中的数据库连接
    :param direction (str): 迁移方向函数名
    """
    migration = runpy.run_path("migrations/versions/0054_skill_upload_object_index.py")
    with Operations.context(MigrationContext.configure(connection)):
        cast(Callable[[], None], migration[direction])()


async def test_postgresql_upload_index_migration_preserves_original_input(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """
    原上传在两次升降级后保持完整，复合外键拒绝另一额度类别或树摘要。

    :param database (async_sessionmaker[AsyncSession]): 独立数据库事务工厂
    :param tmp_path (Path): 私有内容卷
    """
    async with database() as session:
        if session.get_bind().dialect.name != "postgresql":
            pytest.skip("requires disposable PostgreSQL")
    owner = await user(database)
    entry = file_entry(b"original")
    async with database.begin() as session:
        upload = await service(session, tmp_path).begin(
            owner, "migration", SkillTreeManifest(entries=(entry,)), "state"
        )
        identity, digest = upload.id, upload.tree_digest
    async with database() as session:
        connection = await session.connection()
        schema = "upload_index_" + identity.hex
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        for table in ("skill_content_uploads", "skill_upload_objects"):
            await connection.execute(
                text(f"CREATE TABLE {table} (LIKE public.{table} INCLUDING ALL)")
            )
        await connection.execute(
            text(
                "INSERT INTO skill_content_uploads SELECT * FROM public.skill_content_uploads "
                "WHERE id = :id"
            ),
            {"id": identity},
        )
        await connection.run_sync(migrate_index, "downgrade")
        await connection.run_sync(migrate_index, "upgrade")
        assert (
            await connection.scalar(text("SELECT object_index_version FROM skill_content_uploads"))
            == 0
        )
        await connection.execute(
            text(
                "INSERT INTO skill_upload_objects "
                "(user_id, upload_id, digest, tree_digest, scope, entry_json) "
                "SELECT user_id, id, :digest, tree_digest, scope, manifest_json->'entries'->0 "
                "FROM skill_content_uploads"
            ),
            {"digest": entry.sha256},
        )
        for assignment in ("scope = 'package'", "tree_digest = repeat('a', 64)"):
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(text("UPDATE skill_upload_objects SET " + assignment))
        await connection.execute(
            text(
                "UPDATE skill_content_uploads SET object_index_version = 1, object_index_count = 1"
            )
        )
        await connection.run_sync(migrate_index, "downgrade")
        retained = (
            await connection.execute(
                text(
                    "SELECT tree_digest, manifest_json, status, reserved_bytes "
                    "FROM skill_content_uploads"
                )
            )
        ).one()
        assert retained.tree_digest == digest
        assert retained.manifest_json["entries"][0]["sha256"] == entry.sha256
        assert retained.status == "staged" and retained.reserved_bytes == entry.size
        await connection.run_sync(migrate_index, "upgrade")
        assert await connection.scalar(text("SELECT count(*) FROM skill_upload_objects")) == 0
