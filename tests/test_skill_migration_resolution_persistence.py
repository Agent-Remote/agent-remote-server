"""
验证迁移计划版本交换、人工树精确授权及数据库复合归属约束。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy.exc import IntegrityError
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_content_service import service, user
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_migration import migrate, request
from test_skill_migration_conflicts import BASE, pending
from test_skill_migration_resolution_content import begin
from test_skill_snapshots import prepared as prepared
from test_skill_storage import file_entry

from agent_remote_server.db import Base
from agent_remote_server.models.skill_migration_resolution import (
    SkillMigrationResolutionChoice,
    SkillMigrationResolutionContent,
    SkillMigrationResolutionOperation,
    SkillMigrationResolutionPlan,
    SkillMigrationResolutionUpload,
)
from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.repositories.skill_library import SkillLibraryRepository
from agent_remote_server.repositories.skill_migration_resolution import (
    SkillMigrationResolutionRepository,
)
from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.schemas.skill_resolution import SkillResolutionChoice
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_resolution_choices import (
    choice_row,
    choice_spec,
    load_choices,
)
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


async def test_plan_cas_and_late_constraint_failure_preserve_original_choices(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    旧版本不改计划，最后选择外键失败也回滚版本递增和旧选择删除。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    saved = await pending(stopped, tmp_path)
    identity = saved.operation_id
    assert identity is not None
    current = SkillResolutionChoice(path="learning/SKILL.md", use="current")
    incoming = SkillResolutionChoice(path="learning/SKILL.md", use="incoming")
    async with stopped.database.begin() as session:
        await SkillLibraryRepository(session).lock_library(stopped.owner)
        migration = await session.get(SkillBranchPreparation, identity)
        assert migration is not None
        repository = SkillMigrationResolutionRepository(session)
        assert not await repository.replace_choices(migration, 1, [])
        assert await repository.plan(migration) is None
        assert await repository.replace_choices(migration, 0, [choice_row(migration, current)])
        assert not await repository.replace_choices(migration, 0, [choice_row(migration, incoming)])
        assert not await repository.replace_choices(migration, 2**63 - 1, [])
        assert not await repository.replace_choices(migration, -1, [])
    async with stopped.database.begin() as session:
        await SkillLibraryRepository(session).lock_library(stopped.owner)
        migration = await session.get(SkillBranchPreparation, identity)
        assert migration is not None
        repository = SkillMigrationResolutionRepository(session)
        bad = SkillResolutionChoice(path="learning/SKILL.md", file_tree_digest=saved.current_digest)
        with pytest.raises(IntegrityError):
            await repository.replace_choices(migration, 1, [choice_row(migration, bad)])
        plan = await repository.plan(migration)
        assert plan is not None and plan.revision == 1
        assert [choice_spec(row) for row in await repository.choices(migration)] == [current]
        assert await repository.replace_choices(migration, 1, [choice_row(migration, incoming)])
    result = (await user_client.get(f"{BASE}/{identity}/plan")).json()["data"]
    assert result["revision"] == 2 and result["choices"] == [incoming.model_dump(mode="json")]
    async with stopped.database() as session:
        migration = await session.get(SkillBranchPreparation, identity)
        assert (
            migration is not None
            and migration.status == "conflicted"
            and migration.migration_sequence is None
        )


async def test_custom_choices_require_this_migrations_completed_content(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    相同用户另一迁移的完整内容必须重新显式上传授权，摘要相同不代替归属。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
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
    upload = await begin(user_client, saved.operation_id)
    path = f"{BASE}/{saved.operation_id}/uploads/{upload}"
    assert (
        await user_client.put(
            path + "/files/" + file_entry(b"resolved").sha256, content=b"resolved"
        )
    ).status_code == 200
    completed = await user_client.post(path + "/complete")
    digest = completed.json()["data"]["tree_digest"]
    selection = SkillResolutionChoice(path="learning/SKILL.md", file_tree_digest=digest)
    async with stopped.database.begin() as session:
        await SkillLibraryRepository(session).lock_library(stopped.owner)
        first = await session.get(SkillBranchPreparation, saved.operation_id)
        second = await session.get(SkillBranchPreparation, other.operation_id)
        assert first is not None and second is not None
        repository = SkillMigrationResolutionRepository(session)
        loaded = await load_choices(
            service(session, tmp_path),
            PrivateObjectStore(tmp_path / "objects"),
            first,
            repository,
            [selection],
        )
        assert loaded[0].tree is not None and loaded[0].tree.entries == (file_entry(b"resolved"),)
        assert await repository.replace_choices(first, 0, [choice_row(first, selection)])
        with pytest.raises(SkillContentError) as error:
            await load_choices(
                service(session, tmp_path),
                PrivateObjectStore(tmp_path / "objects"),
                second,
                repository,
                [selection],
            )
        assert error.value.code == "RESOLUTION_CONTENT_NOT_FOUND"
        with pytest.raises(IntegrityError):
            await repository.replace_choices(second, 0, [choice_row(second, selection)])
        assert await repository.plan(second) is None
        with pytest.raises(ValueError, match="does not belong"):
            await repository.replace_choices(second, 0, [choice_row(first, selection)])
    replacement = await begin(user_client, other.operation_id)
    assert (
        await user_client.post(f"{BASE}/{other.operation_id}/uploads/{replacement}/complete")
    ).status_code == 200
    async with stopped.database.begin() as session:
        await SkillLibraryRepository(session).lock_library(stopped.owner)
        second = await session.get(SkillBranchPreparation, other.operation_id)
        assert second is not None
        repository = SkillMigrationResolutionRepository(session)
        assert await repository.replace_choices(second, 0, [choice_row(second, selection)])


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_completed_custom_choices_revalidate_bytes(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
    damage: str,
) -> None:
    """
    完成授权不是实际字节存在证明，计划加载再次验证缺失和损坏对象。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param damage (str): 对象损坏方式
    """
    saved = await pending(stopped, tmp_path)
    assert saved.operation_id is not None
    upload = await begin(user_client, saved.operation_id)
    path = f"{BASE}/{saved.operation_id}/uploads/{upload}"
    entry = file_entry(b"resolved")
    assert (
        await user_client.put(path + "/files/" + entry.sha256, content=b"resolved")
    ).status_code == 200
    result = await user_client.post(path + "/complete")
    digest = result.json()["data"]["tree_digest"]
    file = tmp_path / "objects" / str(stopped.owner) / entry.sha256[:2] / entry.sha256
    if damage == "missing":
        file.unlink()
    else:
        file.chmod(0o600)
        file.write_bytes(b"broken")
    async with stopped.database.begin() as session:
        migration = await session.get(SkillBranchPreparation, saved.operation_id)
        assert migration is not None
        with pytest.raises(SkillContentError) as error:
            await load_choices(
                service(session, tmp_path),
                PrivateObjectStore(tmp_path / "objects"),
                migration,
                SkillMigrationResolutionRepository(session),
                [SkillResolutionChoice(path="learning/SKILL.md", file_tree_digest=digest)],
            )
        assert error.value.code == (
            "CONTENT_INCOMPLETE" if damage == "missing" else "CONTENT_INVALID"
        )
    assert (await user_client.post(path + "/complete")).json()["errors"][0][
        "code"
    ] == error.value.code


@pytest.mark.parametrize(
    "case",
    [
        "plan_owner",
        "plan_account",
        "plan_revision",
        "content_account",
        "content_category",
        "upload_owner",
        "upload_digest",
        "upload_scope",
        "choice_category",
        "choice_file_path",
        "choice_directory_path",
        "operation_owner",
        "operation_account",
        "operation_revision",
    ],
)
async def test_migration_resolution_constraints_reject_inconsistent_ownership(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
    case: str,
) -> None:
    """
    实际数据库拒绝混合用户、账户、范围和人工选择，不依赖仅有服务校验。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param case (str): 待破坏约束
    """
    saved = await pending(stopped, tmp_path)
    identity = saved.operation_id
    assert identity is not None
    other_owner = await user(stopped.database)
    other_account = await LibraryHarness(stopped.database, tmp_path, stopped.owner).account()
    upload_id = await begin(user_client, identity)
    upload_path = f"{BASE}/{identity}/uploads/{upload_id}"
    upload = (await user_client.get(upload_path)).json()["data"]
    owner = other_owner if case.endswith("owner") else stopped.owner
    account = other_account if case.endswith("account") else stopped.account
    if case.startswith("plan"):
        record: Base = SkillMigrationResolutionPlan(
            user_id=owner,
            account_id=account,
            migration_id=identity,
            revision=-1 if case.endswith("revision") else 0,
        )
    elif case.startswith("content"):
        record = SkillMigrationResolutionContent(
            user_id=owner,
            account_id=account,
            migration_id=identity,
            category="package" if case.endswith("category") else "state",
            tree_digest=saved.current_digest,
        )
    elif case.startswith("upload"):
        async with stopped.database.begin() as session:
            original = await session.get(SkillMigrationResolutionUpload, upload_id)
            assert original is not None
            await session.delete(original)
        record = SkillMigrationResolutionUpload(
            user_id=owner,
            account_id=account,
            migration_id=identity,
            upload_id=upload_id,
            tree_digest="0" * 64 if case.endswith("digest") else upload["tree_digest"],
            scope="state" if case.endswith("scope") else "account_directory",
        )
    elif case.startswith("choice"):
        async with stopped.database.begin() as session:
            session.add(
                SkillMigrationResolutionPlan(
                    user_id=stopped.owner,
                    account_id=stopped.account,
                    migration_id=identity,
                    revision=0,
                )
            )
            session.add(
                SkillMigrationResolutionContent(
                    user_id=stopped.owner,
                    account_id=stopped.account,
                    migration_id=identity,
                    tree_digest=saved.current_digest,
                )
            )
        record = SkillMigrationResolutionChoice(
            user_id=owner,
            account_id=account,
            migration_id=identity,
            selector_key="test",
            kind="file" if case.endswith("file_path") else "directory",
            path="learning/SKILL.md" if case.endswith("directory_path") else None,
            tree_digest=saved.current_digest,
            category="package" if case.endswith("category") else "state",
        )
    else:
        record = SkillMigrationResolutionOperation(
            user_id=owner,
            account_id=account,
            migration_id=identity,
            idempotency_key=str(uuid4()),
            request_digest="0" * 64,
            plan_revision=-1 if case.endswith("revision") else 0,
            response_json={},
        )
    with pytest.raises(IntegrityError):
        async with stopped.database.begin() as session:
            session.add(record)
            await session.flush()


async def test_operation_keys_are_user_scoped_and_do_not_follow_plan_changes(
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    操作回执查询恢复原结果，计划推进不能重解释旧选择或向其他用户泄露键。

    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    saved = await pending(stopped, tmp_path)
    assert saved.operation_id is not None
    original = {"status": "pending", "plan_revision": 1}
    async with stopped.database.begin() as session:
        session.add(
            SkillMigrationResolutionOperation(
                user_id=stopped.owner,
                account_id=stopped.account,
                migration_id=saved.operation_id,
                idempotency_key="saved-resolution",
                request_digest="0" * 64,
                plan_revision=1,
                response_json=original,
            )
        )
        session.add(
            SkillMigrationResolutionPlan(
                user_id=stopped.owner,
                account_id=stopped.account,
                migration_id=saved.operation_id,
                revision=2,
            )
        )
    async with stopped.database() as session:
        repository = SkillMigrationResolutionRepository(session)
        receipt = await repository.operation(stopped.owner, "saved-resolution")
        assert (
            receipt is not None and receipt.response_json == original and receipt.plan_revision == 1
        )
        assert await repository.operation(uuid4(), "saved-resolution") is None
    with pytest.raises(IntegrityError):
        async with stopped.database.begin() as session:
            session.add(
                SkillMigrationResolutionOperation(
                    user_id=stopped.owner,
                    account_id=stopped.account,
                    migration_id=saved.operation_id,
                    idempotency_key="saved-resolution",
                    request_digest="1" * 64,
                    plan_revision=2,
                    response_json={},
                )
            )
            await session.flush()
