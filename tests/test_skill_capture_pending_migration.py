"""
在真实 PostgreSQL 事务中验证捕获失败迁移和拒绝有损降级。
"""

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from skill_runtime_support import RuntimeHarness
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from test_skill_content_service import database as database
from test_skill_snapshots import prepared as prepared
from test_skill_snapshots import reserve


def migrate(connection: Connection, direction: str) -> None:
    """
    在当前真实连接运行正式迁移的指定方向。

    :param connection (Connection): 已开启事务的 PostgreSQL 连接
    :param direction (str): 正式迁移入口名
    """
    migration = runpy.run_path("migrations/versions/0053_skill_capture_pending.py")
    with Operations.context(MigrationContext.configure(connection)):
        cast(Callable[[], None], migration[direction])()


async def test_postgresql_capture_migration_preserves_frozen_and_pending(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    保留旧冻结摘要，拒绝不完整状态与有损降级，允许恢复完成后回退。

    :param prepared (RuntimeHarness): 独立数据库中的原始快照
    :param tmp_path (Path): 当前测试内容卷
    """
    prepared.snapshot = (await reserve(prepared, tmp_path)).id
    async with prepared.database() as session:
        if session.bind is None or session.bind.dialect.name != "postgresql":
            pytest.skip("requires disposable PostgreSQL")
        connection = await session.connection()
        # 私有事务模式避免其他用例已保留的停止记录阻止本用例恢复旧表结构。
        schema = "capture_migration_" + prepared.snapshot.hex
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await connection.execute(
            text(
                "CREATE TABLE skill_snapshot_terminations "
                "(LIKE public.skill_snapshot_terminations INCLUDING ALL)"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE skill_snapshot_terminations ADD FOREIGN KEY (snapshot_id) "
                "REFERENCES public.session_skill_snapshots(id)"
            )
        )
        await connection.run_sync(migrate, "downgrade")
        values = {"snapshot": prepared.snapshot, "digest": "a" * 64}
        await connection.execute(
            text(
                "INSERT INTO skill_snapshot_terminations "
                "(snapshot_id, incoming_digest, unclean, created_at, updated_at) "
                "VALUES (:snapshot, :digest, false, now(), now())"
            ),
            values,
        )
        await connection.run_sync(migrate, "upgrade")
        row = (
            await connection.execute(
                text(
                    "SELECT incoming_digest, capture_error, unclean "
                    "FROM skill_snapshot_terminations "
                    "WHERE snapshot_id = :snapshot"
                ),
                values,
            )
        ).one()
        assert tuple(row) == ("a" * 64, None, False)
        for digest, code in [
            (None, None),
            ("a" * 64, "quota_exceeded"),
            (None, "unknown"),
            ("short", None),
        ]:
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "UPDATE skill_snapshot_terminations SET incoming_digest = :digest, "
                            "capture_error = :code WHERE snapshot_id = :snapshot"
                        ),
                        {"snapshot": prepared.snapshot, "digest": digest, "code": code},
                    )
        await connection.execute(
            text(
                "UPDATE skill_snapshot_terminations SET incoming_digest = NULL, "
                "capture_error = 'quota_exceeded' WHERE snapshot_id = :snapshot"
            ),
            values,
        )
        with pytest.raises(RuntimeError, match="cannot downgrade"):
            async with connection.begin_nested():
                await connection.run_sync(migrate, "downgrade")
        row = (
            await connection.execute(
                text(
                    "SELECT incoming_digest, capture_error, unclean "
                    "FROM skill_snapshot_terminations "
                    "WHERE snapshot_id = :snapshot"
                ),
                values,
            )
        ).one()
        assert tuple(row) == (None, "quota_exceeded", False)
        await connection.execute(
            text(
                "UPDATE skill_snapshot_terminations SET incoming_digest = :digest, "
                "capture_error = NULL WHERE snapshot_id = :snapshot"
            ),
            values,
        )
        await connection.run_sync(migrate, "downgrade")
        assert (
            await connection.scalar(
                text(
                    "SELECT incoming_digest FROM skill_snapshot_terminations "
                    "WHERE snapshot_id = :snapshot"
                ),
                values,
            )
            == "a" * 64
        )
        await connection.run_sync(migrate, "upgrade")
