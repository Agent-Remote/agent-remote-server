"""
通过真实守护进程和 PostgreSQL 验证默认容量的发布、重启及下一会话继承。
"""

import asyncio
import json
import os
import re
import signal
import socket
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from skill_first_use_live_support import private_fixture, serve_first_use
from skill_lifecycle_live_support import LifecycleReports, lifecycle_app
from skill_pipeline_observation import observe_pipeline
from skill_takeover_support import TakeoverHarness
from skill_takeover_support import takeover as takeover
from sqlalchemy import select, update
from test_skill_content_service import database as database
from test_skill_library import library as library
from test_skill_lifecycle_live import prepare_fixture

from agent_remote_server.models import AuthToken, Session
from agent_remote_server.models.skill_publications import SkillPublication
from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_storage import SkillStoredTree
from agent_remote_server.schemas.skill_library import SkillAddRequest
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_REMOTE_RUN_SKILL_PIPELINE_CAPACITY") != "1",
    reason="requires disposable PostgreSQL/systemd and hours of default-capacity transfer",
)


async def run_capacity_daemons(repo: Path, root: Path, values: dict[str, str]) -> None:
    """
    拥有完整隔离进程组，逐行排空有界诊断并只输出固定阶段和数值进度。

    :param repo (Path): 相邻 Node 仓库
    :param root (Path): 本次私有协调目录
    :param values (dict[str, str]): 一次性连接及原始业务身份
    """
    __tracebackhide__ = True
    root.mkdir(mode=0o700)
    control = root / "control"
    control.mkdir(mode=0o700)
    fixture = root / "fixture.json"
    with open(fixture, "x", encoding="utf-8", opener=private_fixture) as output:
        json.dump(values, output)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process: asyncio.subprocess.Process | None = None
    try:
        script = repo / "tests/linux_skill_ssh_export_test.sh"
        # 长时间运行只读取启动时脚本，后续工作区编辑不能改变其退出和清理逻辑。
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            script.read_text(encoding="utf-8"),
            str(script),
            cwd=repo,
            env=os.environ
            | {
                "AGENT_REMOTE_TEST_SKILL_SSH_EXPORT_FIXTURE": str(fixture),
                "AGENT_REMOTE_TEST_SKILL_SSH_EXPORT_CONTROL": str(control),
                "AGENT_REMOTE_TEST_SKILL_SSH_EXPORT_PORT": str(port),
                "AGENT_REMOTE_TEST_SKILL_SSH_EXPORT_MODE": "capacity",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        assert process.stdout is not None
        lines: deque[str] = deque(maxlen=60)
        passed = False
        async with asyncio.timeout(6 * 60 * 60 + 120):
            while raw := await process.stdout.readline():
                text = raw.decode("utf-8", errors="replace")
                passed |= "--- PASS: TestNativePipelineCapacity" in text
                progress = re.search(
                    r"capacity_phase=(finalization|materialization|reclamation) "
                    r"(elapsed_seconds|complete_seconds)=[0-9]+\.[0-9]+",
                    text,
                )
                if progress:
                    print(progress.group(), flush=True)
                observation = re.search(
                    r"capacity_observation=[a-z_]+ disk_known=(?:true|false) "
                    r"free_bytes=[0-9]+ free_inodes=[0-9]+",
                    text,
                )
                if observation:
                    print(observation.group(), flush=True)
                for value in values.values():
                    if value:
                        text = text.replace(value, "<fixture>")
                lines.append(text[-2000:])
            await process.wait()
        assert process.returncode == 0 and passed, "".join(lines)[-10000:]
    finally:
        try:
            if process is not None and process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 40)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
        finally:
            fixture.unlink(missing_ok=True)


async def test_default_capacity_publish_restart_and_inherit(
    takeover: TakeoverHarness, tmp_path: Path
) -> None:
    """
    由受管工具写满默认容量，正式 Worker 收尾发布并为重启后的独立会话逐文件恢复。

    :param takeover (TakeoverHarness): 未接管账户及真实独立数据库
    :param tmp_path (Path): 本次真实内容卷和私有协调目录
    """
    byte_capacity = os.environ.get("AGENT_REMOTE_RUN_SKILL_PIPELINE_BYTES") == "1"
    combined = os.environ.get("AGENT_REMOTE_RUN_SKILL_PIPELINE_COMBINED") == "1"
    assert not combined or byte_capacity, "combined pipeline requires byte capacity"
    print(f"pipeline_byte_capacity={byte_capacity} combined={combined}", flush=True)
    async with takeover.library.database() as session:
        assert session.get_bind().dialect.name == "postgresql", "requires disposable PostgreSQL"
    values = await prepare_fixture(takeover, tmp_path)
    async with takeover.library.database.begin() as session:
        await session.execute(
            update(AuthToken)
            .where(AuthToken.user_id == takeover.library.owner)
            .values(expires_at=datetime.now(UTC) + timedelta(hours=8))
        )
    candidate = await takeover.library.candidate(
        name="learning",
        content=b"---\nname: learning\ndescription: Capacity test.\n---\n",
    )
    reports = LifecycleReports()
    async with (
        serve_first_use(takeover, False, lifecycle_app(takeover, reports)) as server,
        AsyncClient(
            base_url=server.local_url,
            headers={"Authorization": "Bearer " + values["user_token"]},
            timeout=1200,
        ) as client,
        observe_pipeline(takeover, reports),
    ):
        response = await client.post(
            "/api/v1/skills/installations",
            json=SkillAddRequest(
                idempotency_key=str(uuid4()), expected_generation=0, items=(candidate,)
            ).model_dump(mode="json"),
        )
        assert response.status_code == 200
        operation = response.json()
        assert operation["committed"] and operation["status"] == "preparing"
        await run_capacity_daemons(
            Path(__file__).resolve().parents[2] / "agent-remote-node",
            tmp_path / "daemons",
            values | {"url": server.worker_url, "operation_id": operation["operation_id"]},
        )
    assert reports.heartbeats > 1 and reports.reconciliations > 0
    async with takeover.library.database() as session:
        sessions = list(await session.scalars(select(Session)))
        snapshots = list(await session.scalars(select(SessionSkillSnapshot)))
        receipts = list(await session.scalars(select(SkillFinalization)))
        publications = list(await session.scalars(select(SkillPublication)))
        assert len(sessions) == len(snapshots) == len(receipts) == len(publications) == 2
        assert all(row.status == "stopped" for row in sessions)
        assert all(row.status == "published" and not row.unclean for row in receipts)
        assert all(row.status == "published" for row in publications)
        assert len({row.tree_digest for row in receipts}) == 1
        tree = await session.scalar(
            select(SkillStoredTree).where(
                SkillStoredTree.user_id == takeover.library.owner,
                SkillStoredTree.category == "state",
                SkillStoredTree.digest == receipts[0].tree_digest,
            )
        )
        assert tree is not None
        manifest = SkillTreeManifest.model_validate(tree.manifest_json)
        expected_entries = 30 if byte_capacity and not combined else 100_000
        expected_files = (99_990 if combined else 20) if byte_capacity else 99_999
        assert len(manifest.entries) == expected_entries
        files = [entry for entry in manifest.entries if entry.kind == "file"]
        assert len(files) == expected_files
        assert len({entry.sha256 for entry in files}) == expected_files
        if byte_capacity:
            assert sum(entry.size for entry in files) == 10 * (1 << 30)
