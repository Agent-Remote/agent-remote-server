"""
通过真实用户 HTTP 路由填满默认二 GiB 包存储及并发上传预留。
"""

import asyncio
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from skill_byte_capacity_support import GIB, disk_chunks, write_binary
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_snapshots import prepared as prepared
from test_skill_storage import file_entry
from test_skill_upload_capacity import capacity_client

from agent_remote_server.models.skill_storage import (
    SkillContentObject,
    SkillContentUpload,
    SkillStorageUsage,
)
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_REMOTE_RUN_SKILL_PACKAGE_BYTE_CAPACITY") != "1",
    reason="requires explicit disposable PostgreSQL and at least 5 GiB free disk space",
)


def package_fixtures(root: Path) -> list[SkillTreeManifest]:
    """
    生成总共二 GiB 的独立文件，各包及文件遵守五十和十 MiB 默认上限。

    :param root (Path): 本次独占的源文件根目录
    :return list[SkillTreeManifest]: 总字节数精确且摘要互异的四十一份包清单
    """
    root.mkdir()
    remaining = 2 * GIB
    manifests: list[SkillTreeManifest] = []
    while remaining:
        entries = []
        for index in range(5):
            if not remaining:
                break
            size = min(10 * 1024**2, remaining)
            name = f"package-{len(manifests):02d}-file-{index}"
            entries.append(write_binary(root / name, size, name))
            remaining -= size
        manifests.append(SkillTreeManifest(entries=tuple(entries)))
    assert sum(manifest.total_bytes for manifest in manifests) == 2 * GIB
    digests = [entry.sha256 for manifest in manifests for entry in manifest.entries]
    assert len(set(digests)) == len(digests) == 205
    return manifests


async def package_usage(state: RuntimeHarness, stored: int, reserved: int) -> None:
    """
    包字节从预留原子转为保存，同时不消耗运行状态额度。

    :param state (RuntimeHarness): 本次独立用户与事务工厂
    :param stored (int): 预期已保存包字节
    :param reserved (int): 预期未完成包预留
    """
    async with state.database() as session:
        usage = await session.get(SkillStorageUsage, state.owner)
        assert usage is not None
        assert (usage.package_bytes, usage.package_reserved) == (stored, reserved)
        assert (usage.state_bytes, usage.state_reserved) == (0, 0)
        total = await session.scalar(
            select(func.coalesce(func.sum(SkillContentObject.size), 0)).where(
                SkillContentObject.user_id == state.owner,
                SkillContentObject.category == "package",
            )
        )
        assert total == stored


async def package_rejected(client: AsyncClient, manifest: SkillTreeManifest, code: str) -> None:
    """
    包边界拒绝通过正式错误封套返回，不把受理失败当成完整保存。

    :param client (AsyncClient): 真实用户网络客户端
    :param manifest (SkillTreeManifest): 超出指定默认边界的清单
    :param code (str): 预期稳定错误码
    """
    response = await client.post(
        "/api/v1/skills/content/uploads",
        json={"idempotency_key": str(uuid4()), "manifest": manifest.model_dump(mode="json")},
    )
    assert response.status_code == (413 if code == "QUOTA_EXCEEDED" else 422), response.text
    assert response.json()["errors"][0]["code"] == code and not response.json()["committed"]


async def test_default_package_bytes_and_staging_over_http(
    prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    四十一份实际包同时预留二 GiB，全部传输并完整验证后继续拒绝一字节超额。

    :param prepared (RuntimeHarness): 提供隔离数据库与现有测试节点
    :param tmp_path (Path): 本次独占的实际内容卷
    """
    policy = SkillStoragePolicy()
    assert policy.user_package_bytes == policy.user_staging_bytes == 2 * GIB
    assert policy.package_file_bytes == 10 * 1024**2 and policy.package_bytes == 50 * 1024**2
    assert shutil.disk_usage(tmp_path).free >= 5 * GIB, "package acceptance needs 5 GiB free"
    state = replace(prepared, owner=await user(prepared.database))
    source_root = tmp_path / "package-source"
    start = time.monotonic()
    manifests = await asyncio.to_thread(package_fixtures, source_root)
    print(f"package_capacity_fixture_seconds={time.monotonic() - start:.3f}", flush=True)
    async with capacity_client(state, tmp_path, user_authenticated=True) as client:
        too_big = manifests[0].entries[0].model_copy(update={"size": 10 * 1024**2 + 1})
        await package_rejected(client, SkillTreeManifest(entries=(too_big,)), "INVALID_REQUEST")
        await package_rejected(
            client,
            SkillTreeManifest(entries=(*manifests[0].entries, file_entry(b"x", path="z"))),
            "INVALID_REQUEST",
        )
        uploads: list[UUID] = []
        for manifest in manifests:
            response = await client.post(
                "/api/v1/skills/content/uploads",
                json={
                    "idempotency_key": str(uuid4()),
                    "manifest": manifest.model_dump(mode="json"),
                },
            )
            assert response.status_code == 200, response.text
            assert not response.json()["committed"]
            uploads.append(UUID(response.json()["data"]["id"]))
        await package_usage(state, 0, 2 * GIB)
        excess = SkillTreeManifest(entries=(file_entry(b"x"),))
        await package_rejected(client, excess, "QUOTA_EXCEEDED")
        await package_usage(state, 0, 2 * GIB)
        stored = 0
        for upload_id, manifest in zip(uploads, manifests, strict=True):
            base = f"/api/v1/skills/content/uploads/{upload_id}"
            for entry in manifest.entries:
                response = await client.put(
                    base + "/files/" + entry.sha256,
                    content=disk_chunks(source_root / entry.path),
                    headers={"Content-Length": str(entry.size)},
                )
                assert response.status_code == 200, response.text
                assert not response.json()["committed"] and response.json()["data"]["created"]
            response = await client.post(base + "/complete")
            assert response.status_code == 200, response.text
            assert response.json()["committed"] and response.json()["status"] == "stored"
            stored += manifest.total_bytes
            await package_usage(state, stored, 2 * GIB - stored)
            print(f"package_capacity_stored={stored}", flush=True)
        await package_rejected(client, excess, "QUOTA_EXCEEDED")
        await package_usage(state, 2 * GIB, 0)
        async with state.database() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(SkillContentUpload)
                .where(SkillContentUpload.user_id == state.owner)
            )
            assert count == len(manifests)
        print(f"package_capacity_elapsed_seconds={time.monotonic() - start:.3f}", flush=True)
