"""
通过真实 PostgreSQL 和节点 HTTP 验证十万文件的完整下载与原租约续期。
"""

import asyncio
import hashlib
import io
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import event
from sqlalchemy.engine import Connection, ExecutionContext
from test_skill_content_service import database as database
from test_skill_content_service import service
from test_skill_library import LibraryHarness
from test_skill_snapshots import prepared as prepared
from test_skill_snapshots import reserve
from test_skill_storage import file_entry
from test_skill_upload_capacity import capacity_client

from agent_remote_server.models import NodeTask
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, SkillCheckpoint
from agent_remote_server.schemas.skill_library import SkillRuleRequest
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_REMOTE_RUN_SKILL_DOWNLOAD_CAPACITY") != "1",
    reason="requires disposable PostgreSQL and all 100000 actual HTTP file downloads",
)


async def test_default_100000_file_downloads_over_http(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    完整源树真实落盘并正式预约，全部文件经节点授权下载及 SHA-256 验证。

    :param prepared (RuntimeHarness): 独立账户和准备会话夹具
    :param tmp_path (Path): 当前实际对象卷
    """
    count = 100_000
    values = [f"download-object-{index:06d}\n".encode() for index in range(count)]
    entries = tuple(
        file_entry(value, path=f"state-{index:06d}") for index, value in enumerate(values)
    )
    manifest = SkillTreeManifest(entries=entries)
    assert len({entry.sha256 for entry in entries}) == count
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    await library.execute(
        SkillRuleRequest(
            command="disable", skill="learning", idempotency_key=str(uuid4()), expected_generation=1
        )
    )
    start = time.monotonic()
    async with prepared.database.begin() as session:
        upload = await service(session, tmp_path).begin(
            prepared.owner, str(uuid4()), manifest, "account_directory"
        )
        upload_id = upload.id
    store = PrivateObjectStore(tmp_path / "objects")
    for entry, value in zip(entries, values, strict=True):
        assert await store.put_file(prepared.owner, entry, io.BytesIO(value))
    async with prepared.database.begin() as session:
        tree = await service(session, tmp_path).complete(prepared.owner, upload_id)
        checkpoint = SkillCheckpoint(
            id=uuid4(),
            user_id=prepared.owner,
            account_id=prepared.account,
            scope="directory",
            directory_epoch=1,
            content_digest=tree.digest,
            tree_digest=tree.digest,
        )
        session.add(checkpoint)
        await session.flush()
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert directory is not None
        directory.head_checkpoint_id = checkpoint.id
    snapshot = await reserve(prepared, tmp_path)
    prepared.snapshot = snapshot.id
    assert snapshot.tree_digest == tree.digest
    print(f"download_capacity_fixture_seconds={time.monotonic() - start:.3f}", flush=True)

    async with capacity_client(prepared, tmp_path) as client:
        async with prepared.database.begin() as session:
            task = await session.get(NodeTask, prepared.task)
            assert task is not None
            task.status = "leased"
            task.retry_count = 1
            task.lease_until = datetime.now(UTC) + timedelta(seconds=30)
            task.payload = {
                **task.payload,
                "skill_manager": {
                    "protocol_version": 1,
                    "manifest_version": 1,
                    "snapshot_id": str(snapshot.id),
                    "task_id": str(task.id),
                },
            }
            lease_attempt = task.retry_count
        base = f"/api/v1/node/skill-snapshots/{snapshot.id}"
        params = {"task_id": str(prepared.task)}
        response = await client.get(base, params=params)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["manifest"] == manifest.model_dump(mode="json")
        async with prepared.database() as session:
            engine = session.get_bind()
        manifest_reads = 0

        def observe(
            connection: Connection,
            cursor: object,
            statement: str,
            parameters: object,
            context: ExecutionContext,
            executemany: bool,
        ) -> None:
            """
            仅统计完整 JSON 查询次数，不记录模板参数、认证信息或内容。

            :param connection (Connection): 当前驱动连接
            :param cursor (object): 不读取的游标
            :param statement (str): SQL 模板
            :param parameters (object): 不读取的参数
            :param context (ExecutionContext): 当前执行上下文
            :param executemany (bool): 是否批量执行
            """
            nonlocal manifest_reads
            if (
                statement.lstrip().upper().startswith("SELECT")
                and "skill_stored_trees.manifest_json" in statement
            ):
                manifest_reads += 1

        event.listen(engine, "before_cursor_execute", observe)
        start = time.monotonic()
        renewed = start
        renewals = 0
        try:
            async with asyncio.timeout(3 * 60 * 60):
                for index, entry in enumerate(entries, 1):
                    if time.monotonic() - renewed >= 10:
                        renewal = await client.post(
                            base + "/lease", params=params, json={"lease_attempt": lease_attempt}
                        )
                        assert renewal.status_code == 200, renewal.text
                        renewed = time.monotonic()
                        renewals += 1
                    response = await client.get(base + "/files/" + entry.sha256, params=params)
                    assert response.status_code == 200, (index, response.text)
                    assert len(response.content) == entry.size
                    assert hashlib.sha256(response.content).hexdigest() == entry.sha256
                    assert response.headers["etag"] == '"' + entry.sha256 + '"'
                    if index % 1_000 == 0:
                        print(
                            f"download_capacity_files={index} "
                            f"elapsed_seconds={time.monotonic() - start:.3f}",
                            flush=True,
                        )
        finally:
            event.remove(engine, "before_cursor_execute", observe)
        assert manifest_reads == 0 and renewals > 0
        async with prepared.database.begin() as session:
            task = await session.get(NodeTask, prepared.task)
            assert task is not None
            task.status = "cancelled"
        response = await client.get(base + "/files/" + entries[0].sha256, params=params)
        assert response.status_code == 404
        print(
            f"download_capacity_seconds={time.monotonic() - start:.3f} "
            f"manifest_reads={manifest_reads} renewals={renewals}",
            flush=True,
        )
