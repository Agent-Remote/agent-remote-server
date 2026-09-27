"""
验证人工迁移上传真实范围、完整字节、原子授权和不发布的接口语义。
"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_conflicts_api import token
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_content_service import service, user
from test_skill_finalization import stopped as stopped
from test_skill_migration import migrate, request
from test_skill_migration_conflicts import BASE, pending
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command
from test_skill_state_commands import execute as state_execute
from test_skill_storage import file_entry

from agent_remote_server.models.skill_migration_resolution import (
    SkillMigrationResolutionContent,
    SkillMigrationResolutionPlan,
    SkillMigrationResolutionUpload,
)
from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_storage import SkillContentUpload
from agent_remote_server.repositories.skill_migration_resolution import (
    SkillMigrationResolutionRepository,
)
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.schemas.skill_resolution import SkillResolutionUploadRequest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_resolution_content import (
    SkillMigrationResolutionContentService,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def begin(client: AsyncClient, migration_id: UUID, content: bytes = b"resolved") -> UUID:
    """
    通过真实用户接口受理一份独立人工文件清单。

    :param client (AsyncClient): 认证客户端
    :param migration_id (UUID): 已保留迁移身份
    :param content (bytes): 测试内容
    :return UUID: 已受理上传身份
    """
    response = await client.post(
        f"{BASE}/{migration_id}/uploads",
        json={
            "idempotency_key": str(uuid4()),
            "manifest": SkillTreeManifest(entries=(file_entry(content),)).model_dump(mode="json"),
        },
    )
    assert response.status_code == 200, response.text
    return UUID(response.json()["data"]["id"])


async def test_custom_upload_completes_scope_without_changing_migration(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    重复及并发完成只建立一份人工授权，分支、基线和计划均不改变。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 已保留冲突的账户
    :param tmp_path (Path): 内容卷
    """
    saved = await pending(stopped, tmp_path)
    assert saved.operation_id is not None
    path = f"{BASE}/{saved.operation_id}"
    before = (await command(stopped, tmp_path)).expected
    upload = await begin(user_client, saved.operation_id)
    upload_path = f"{path}/uploads/{upload}"
    incomplete = await user_client.post(upload_path + "/complete")
    assert incomplete.json()["errors"][0]["code"] == "CONTENT_INCOMPLETE"
    entry = file_entry(b"resolved")
    assert (
        await user_client.put(upload_path + "/files/" + entry.sha256, content=b"corrupt!")
    ).status_code == 422
    assert (
        await user_client.put(upload_path + "/files/" + entry.sha256, content=b"resolved")
    ).status_code == 200
    first, second = await asyncio.gather(
        user_client.post(upload_path + "/complete"), user_client.post(upload_path + "/complete")
    )
    assert first.status_code == 200 and first.json() == second.json()
    assert first.json()["status"] == "stored"
    assert (await user_client.get(upload_path)).json()["data"]["status"] == "committed"
    repeat = await begin(user_client, saved.operation_id)
    assert (await user_client.post(f"{path}/uploads/{repeat}/complete")).json() == first.json()
    plan = (await user_client.get(path + "/plan")).json()
    assert not plan["committed"] and plan["data"]["revision"] == 0 and plan["data"]["choices"] == []
    async with stopped.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillMigrationResolutionContent)
                .where(SkillMigrationResolutionContent.migration_id == saved.operation_id)
            )
        ) == 1
        assert await session.get(SkillMigrationResolutionPlan, saved.operation_id) is None
        migration = await session.get(SkillBranchPreparation, saved.operation_id)
        assert (
            migration is not None
            and migration.status == "conflicted"
            and migration.migration_sequence is None
        )
        assert migration.response_json == saved.model_dump(mode="json")
    assert (await command(stopped, tmp_path)).expected == before


async def test_cross_migration_and_forged_prefix_uploads_are_not_authorized(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    同用户另一迁移或普通上传伪造保留键前缀也不能绕过实际绑定。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    saved = await pending(stopped, tmp_path)
    assert isinstance(saved, SkillMigrationView) and saved.operation_id is not None
    other = await migrate(
        stopped,
        tmp_path,
        await request(
            stopped, tmp_path, saved.before.source.revision_id, saved.before.target.revision_id
        ),
    )
    assert other.operation_id is not None
    upload = await begin(user_client, other.operation_id)
    async with stopped.database.begin() as session:
        forged = await service(session, tmp_path).begin(
            stopped.owner,
            f"migration-resolve:{saved.operation_id}:upload:" + "a" * 64,
            SkillTreeManifest(entries=(file_entry(b"resolved"),)),
            "account_directory",
        )
        forged_id = forged.id
    for identity in (upload, forged_id):
        path = f"{BASE}/{saved.operation_id}/uploads/{identity}"
        assert (await user_client.get(path)).status_code == 404
        assert (
            await user_client.put(
                path + "/files/" + file_entry(b"resolved").sha256, content=b"resolved"
            )
        ).status_code == 404
        assert (await user_client.post(path + "/complete")).status_code == 404
    async with stopped.database() as session:
        assert await session.get(SkillMigrationResolutionUpload, forged_id) is None


async def test_superseded_migration_can_recover_existing_upload_but_not_start_new(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    重置后完成已有用户编辑只保活内容，不恢复旧计划或成功迁移序号。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    saved = await pending(stopped, tmp_path)
    assert saved.operation_id is not None
    upload = await begin(user_client, saved.operation_id)
    path = f"{BASE}/{saved.operation_id}"
    await state_execute(stopped, tmp_path, await command(stopped, tmp_path))
    new = await user_client.post(
        path + "/uploads", json={"idempotency_key": "new", "manifest": {"entries": []}}
    )
    assert new.json()["errors"][0]["code"] == "CONFLICT_NOT_ACTIVE"
    upload_path = f"{path}/uploads/{upload}"
    assert (await user_client.get(upload_path)).status_code == 200
    assert (
        await user_client.put(
            upload_path + "/files/" + file_entry(b"resolved").sha256, content=b"resolved"
        )
    ).status_code == 200
    assert (await user_client.post(upload_path + "/complete")).status_code == 200
    info = (await user_client.get(path)).json()["data"]
    assert info["status"] == "superseded" and info["original"] == saved.model_dump(mode="json")
    assert (await user_client.get(path + "/plan")).json()["data"]["current_status"] == "superseded"


@pytest.mark.parametrize("kind", ["device", "node", "other-user"])
async def test_migration_custom_content_rejects_other_credentials(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
    kind: str,
) -> None:
    """
    已知迁移和租约不能授权其他用户、设备或节点查看计划和上传编辑。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 目标账户
    :param tmp_path (Path): 内容卷
    :param kind (str): 无权限凭据类型
    """
    saved = await pending(stopped, tmp_path)
    assert saved.operation_id is not None
    upload = await begin(user_client, saved.operation_id)
    owner = await user(stopped.database) if kind == "other-user" else stopped.owner
    headers = {
        "Authorization": "Bearer "
        + await token(stopped, owner, "user" if kind == "other-user" else kind)
    }
    path = f"{BASE}/{saved.operation_id}"
    calls = [
        ("GET", path + "/plan"),
        ("POST", path + "/uploads"),
        ("GET", f"{path}/uploads/{upload}"),
        ("POST", f"{path}/uploads/{upload}/complete"),
        ("PUT", f"{path}/uploads/{upload}/files/" + "0" * 64),
    ]
    for method, route in calls:
        response = await user_client.request(method, route, headers=headers)
        assert response.status_code == (
            404 if kind == "other-user" else 403 if kind == "device" else 401
        )


async def test_expired_lease_replacement_preserves_exact_binding(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    过期租约不借用新身份，新的受理键获得独立绑定且旧租约仍可解释。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    saved = await pending(stopped, tmp_path)
    assert saved.operation_id is not None
    upload = await begin(user_client, saved.operation_id)
    async with stopped.database.begin() as session:
        row = await session.get(SkillContentUpload, upload)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    path = f"{BASE}/{saved.operation_id}/uploads/{upload}"
    assert (await user_client.get(path)).json()["data"]["status"] == "expired"
    assert (
        await user_client.put(
            path + "/files/" + file_entry(b"resolved").sha256, content=b"resolved"
        )
    ).status_code == 409
    fresh = await begin(user_client, saved.operation_id)
    assert fresh != upload
    async with stopped.database() as session:
        assert await session.get(SkillMigrationResolutionUpload, upload) is not None
        assert await session.get(SkillMigrationResolutionUpload, fresh) is not None


async def test_completion_grant_failure_rolls_back_content_commit(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    最后授权失败时外层即使提交也不会留下已完成租约或人工内容引用。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param monkeypatch (pytest.MonkeyPatch): 注入授权保存故障
    """
    saved = await pending(stopped, tmp_path)
    assert saved.operation_id is not None
    upload = await begin(user_client, saved.operation_id)
    path = f"{BASE}/{saved.operation_id}/uploads/{upload}"
    assert (
        await user_client.put(
            path + "/files/" + file_entry(b"resolved").sha256, content=b"resolved"
        )
    ).status_code == 200

    async def reject(
        self: SkillMigrationResolutionRepository, migration: SkillBranchPreparation, digest: str
    ) -> None:
        """
        模拟完整树提交后的最后引用存储故障。

        :param migration (SkillBranchPreparation): 原始受理
        :param digest (str): 已验证人工树
        """
        raise SkillContentError("HEAD_CHANGED", "injected grant failure")

    with monkeypatch.context() as patch:
        patch.setattr(SkillMigrationResolutionRepository, "retain_content", reject)
        async with stopped.database.begin() as session:
            content = SkillMigrationResolutionContentService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            )
            with pytest.raises(SkillContentError, match="injected grant failure"):
                await content.complete(stopped.owner, saved.operation_id, upload)
    assert (await user_client.get(path)).json()["data"]["status"] == "staged"
    assert (await user_client.post(path + "/complete")).status_code == 200


async def test_upload_key_replay_and_manifest_drift_are_atomic(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    同键并发复用同一租约，清单变化失败而不改变原绑定。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有内容卷
    """
    saved = await pending(stopped, tmp_path)
    assert saved.operation_id is not None
    payload = SkillResolutionUploadRequest(
        idempotency_key="durable-key", manifest=SkillTreeManifest(entries=())
    )

    async def accept(payload: SkillResolutionUploadRequest) -> UUID:
        """
        在独立请求事务中恢复原始用户上传。

        :param payload (SkillResolutionUploadRequest): 原始清单请求
        :return UUID: 受理上传身份
        """
        assert saved.operation_id is not None
        async with stopped.database.begin() as session:
            content = SkillMigrationResolutionContentService(
                session, PrivateObjectStore(tmp_path / "objects"), SkillStoragePolicy()
            )
            return (await content.begin(stopped.owner, saved.operation_id, payload)).id

    first, second = await asyncio.gather(accept(payload), accept(payload))
    assert first == second
    await state_execute(stopped, tmp_path, await command(stopped, tmp_path))
    assert await accept(payload) == first
    with pytest.raises(SkillContentError) as error:
        await accept(
            payload.model_copy(
                update={"manifest": SkillTreeManifest(entries=(file_entry(b"new"),))}
            )
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    async with stopped.database() as session:
        row = await session.get(SkillContentUpload, first)
        assert row is not None
        assert (
            row.idempotency_key
            == f"migration-resolve:{saved.operation_id}:upload:"
            + hashlib.sha256(b"durable-key").hexdigest()
        )
