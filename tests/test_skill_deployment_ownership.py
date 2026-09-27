"""
验证配置计划的数据库归属隔离、账户本地来源和历史无计划边界。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from deployment_attempt_support import legacy_attempts, observe
from skill_runtime_support import RuntimeHarness
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_deployment_plans import inspect, plans
from test_skill_library import LibraryHarness
from test_skill_library import library as library
from test_skill_local import activate, register, source
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models.skill_deployment import SkillDeploymentEntry, SkillDeploymentTarget
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.repositories.skill_deployment import SkillDeploymentRepository
from agent_remote_server.schemas.skill_library import SkillRuleRequest, SkillScope


@pytest.mark.parametrize("kind", ["owner", "source", "epoch", "empty_revision"])
async def test_database_rejects_invalid_package_plan_references(
    library: LibraryHarness,
    kind: str,
) -> None:
    """
    直接绕过服务的写入仍受所有者、来源、安装纪元和非空版本约束。

    :param library (LibraryHarness): 当前所有者
    :param kind (str): 越权或无效引用类型
    """
    await library.account()
    accepted = await library.add(await library.candidate(), await library.candidate("other"))
    assert accepted.operation_id is not None
    other = await library.info("other")
    stranger = LibraryHarness(library.database, library.root, await user(library.database))
    await stranger.add(await stranger.candidate())
    foreign = await stranger.info()
    with pytest.raises(IntegrityError):
        async with library.database.begin() as session:
            _, entries = await SkillDeploymentRepository(session).rows(
                library.owner, accepted.operation_id
            )
            entry = next(row for row in entries if row.name == "learning")
            if kind == "owner":
                entry.source_id = foreign.id
                entry.installation_id = foreign.id
                entry.package_revision_id = foreign.default_revision_id
            elif kind == "source":
                entry.package_revision_id = other.default_revision_id
            elif kind == "epoch":
                entry.installation_epoch = 999
            else:
                entry.package_revision_id = None
            await session.flush()
    assert len((await plans(library, accepted.operation_id))[0].sources) == 2


async def test_local_plan_keeps_exact_account_and_disabled_source(
    prepared: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    本地配置计划同时包含当前库和本地来源，跨账户本地版本引用由外键拒绝。

    :param prepared (RuntimeHarness): 已准备的运行账户
    :param tmp_path (Path): 内容卷
    """
    local = await register(prepared, tmp_path, await source(prepared, tmp_path))
    await activate(prepared, local)
    library = LibraryHarness(prepared.database, tmp_path, prepared.owner)
    second = await library.account()
    accepted = await library.execute(
        SkillRuleRequest(
            command="disable",
            skill=str(local.id),
            scope=SkillScope(account_id=prepared.account),
            idempotency_key=str(uuid4()),
            expected_generation=await library.generation(),
        )
    )
    assert accepted.operation_id is not None
    original = await plans(library, accepted.operation_id)
    assert len(original) == 1 and original[0].account_id == prepared.account
    selection = next(row for row in original[0].sources if row.origin == "account_local")
    assert selection.source_id == local.id and selection.revision_id == local.default_revision_id
    assert not selection.enabled
    assert any(row.origin == "library" for row in original[0].sources)
    async with library.database.begin() as session:
        operation = await session.get(SkillOperation, accepted.operation_id)
        assert operation is not None
        await observe(session, operation, "failed", retryable=True, error_code="TRANSFER_FAILED")
    assert "pending_operation" in (await inspect(library)).reasons(
        "local_revision", selection.revision_id
    )
    with pytest.raises(IntegrityError):
        async with library.database.begin() as session:
            session.add(
                SkillDeploymentTarget(
                    user_id=library.owner,
                    operation_id=accepted.operation_id,
                    account_id=second,
                    node_id=None,
                    tool_type="claude",
                    runtime_backend=None,
                    plan_digest="a" * 64,
                )
            )
            await session.flush()
            session.add(
                SkillDeploymentEntry(
                    user_id=library.owner,
                    operation_id=accepted.operation_id,
                    account_id=second,
                    origin="account_local",
                    source_id=local.id,
                    name=local.name,
                    enabled=False,
                    content_digest=selection.content_digest,
                    local_skill_id=local.id,
                    local_revision_id=selection.revision_id,
                )
            )
            await session.flush()


async def test_legacy_operation_does_not_reconstruct_plan(library: LibraryHarness) -> None:
    """
    历史回执保留原有版本保活语义，不根据当前账户集合补造配置计划。

    :param library (LibraryHarness): 当前所有者
    """
    await library.account()
    accepted = await library.add(await library.candidate())
    assert accepted.operation_id is not None
    async with library.database.begin() as session:
        operation = await session.get(SkillOperation, accepted.operation_id)
        assert operation is not None
        await legacy_attempts(session, operation)
        await session.execute(
            delete(SkillDeploymentEntry).where(
                SkillDeploymentEntry.operation_id == accepted.operation_id,
            )
        )
        await session.execute(
            delete(SkillDeploymentTarget).where(
                SkillDeploymentTarget.operation_id == accepted.operation_id,
            )
        )
        operation = await session.get(SkillOperation, accepted.operation_id)
        assert operation is not None
        operation.plan_version = None
        operation.status = "pending"
        operation.result_json = {
            **operation.result_json,
            "targets": [
                target.model_copy(
                    update={"plan_digest": None, "attempt_id": None, "attempt_number": None}
                ).model_dump(mode="json")
                for target in accepted.data.targets
            ],
        }
    await library.account()
    assert await plans(library, accepted.operation_id) == ()
    assert "pending_operation" in (await inspect(library)).reasons(
        "revision", accepted.data.revision_ids[0]
    )
    async with library.database.begin() as session:
        operation = await session.get(SkillOperation, accepted.operation_id)
        assert operation is not None
        operation.result_json = {**operation.result_json, "revision_ids": []}
    with pytest.raises(ValueError, match="no fixed"):
        await inspect(library)
