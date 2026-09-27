"""
验证后台任务的启停/重试配置和持久化删除身份约束。
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_skill_content_service import database as database
from test_skill_content_service import user

from agent_remote_server.config import Settings
from agent_remote_server.models.skill_content_deletions import SkillContentDeletion
from agent_remote_server.services.skills.gc.lifecycle import run_skill_content_deletions
from agent_remote_server.services.skills.gc.worker import (
    DeletionOutcome,
    SkillContentDeletionWorker,
)


@pytest.mark.parametrize("enabled", [False, True])
async def test_deletion_lifecycle_feature_gate_retries_and_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    enabled: bool,
) -> None:
    """
    停用时完全不创建 worker；启用时有界重试且日志只包含异常类别。

    :param tmp_path (Path): 私有卷路径
    :param monkeypatch (pytest.MonkeyPatch): 生命周期注入
    :param caplog (pytest.LogCaptureFixture): 结构化日志观察
    :param enabled (bool): 功能开关
    """
    app = FastAPI()
    app.state.settings = Settings(
        skill_manager_enabled=enabled,
        skill_storage_root=tmp_path / "objects",
        skill_deletion_interval_seconds=1,
        skill_deletion_batch_size=7,
    )
    app.state.session_factory = None
    stop = asyncio.Event()
    calls: list[int] = []

    async def run(
        worker: SkillContentDeletionWorker, limit: int = 100
    ) -> tuple[tuple[UUID, DeletionOutcome], ...]:
        """
        第一轮失败，第二轮停止；调用参数必须保留发布配置。

        :param worker (SkillContentDeletionWorker): 本次 worker
        :param limit (int): 当前批量限制
        :return tuple[tuple[UUID, DeletionOutcome], ...]: 空任务处理结果
        """
        calls.append(limit)
        if len(calls) == 1:
            raise RuntimeError("private diagnostic must not appear")
        stop.set()
        return ()

    monkeypatch.setattr(SkillContentDeletionWorker, "run_once", run)
    if not enabled:
        stop.set()
    await asyncio.wait_for(run_skill_content_deletions(app, stop), timeout=5)
    assert calls == ([7, 7] if enabled else [])
    assert "private diagnostic" not in caplog.text
    assert not (tmp_path / "objects").exists()


@pytest.mark.parametrize("invalid", ["duplicate", "mask", "count", "complete_time", "owner"])
async def test_deletion_constraints_preserve_original_identity(
    database: async_sessionmaker[AsyncSession],
    invalid: str,
) -> None:
    """
    持久化状态必须有合法所有者、类别、计数和完成时间，一个摘要不能同时有两个待删除任务。

    :param database (async_sessionmaker[AsyncSession]): 真实外键数据库
    :param invalid (str): 被检验的约束
    """
    owner = await user(database)
    digest = "a" * 64
    if invalid == "duplicate":
        async with database.begin() as session:
            session.add(
                SkillContentDeletion(
                    user_id=owner,
                    digest=digest,
                    size=0,
                    category_mask=2,
                    status="pending",
                    attempts=0,
                    next_attempt_at=datetime.now(UTC),
                )
            )
    with pytest.raises(IntegrityError):
        async with database.begin() as session:
            session.add(
                SkillContentDeletion(
                    user_id=uuid4() if invalid == "owner" else owner,
                    digest=digest,
                    size=0,
                    category_mask=0 if invalid == "mask" else 2,
                    status="complete" if invalid == "complete_time" else "pending",
                    attempts=-1 if invalid == "count" else 0,
                    next_attempt_at=datetime.now(UTC),
                )
            )
