"""
验证迁移冲突的真实侧身份、精确导出、只读行为及用户授权边界。
"""

import base64
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from skill_publication_support import ingest, new_session, publish
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from test_skill_conflicts_api import token
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_migration import migrate, request, versions
from test_skill_preparation import prepare
from test_skill_preparation import request as preparation_request
from test_skill_snapshots import prepared as prepared
from test_skill_state_commands import command
from test_skill_state_commands import execute as state_execute

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.models.skill_resolution import SkillResolutionPlan
from agent_remote_server.models.skill_state import AccountSkillState, SkillCheckpoint
from agent_remote_server.models.skill_storage import SkillContentUpload
from agent_remote_server.schemas.skill_library import SkillRemoveRequest
from agent_remote_server.schemas.skill_migration import SkillMigrationView
from agent_remote_server.schemas.skill_preparation import SkillPreparationView

BASE = "/api/v1/skills/state/migration/conflicts"


async def pending(
    state: RuntimeHarness,
    root: Path,
    mode: str = "incremental",
    linked: bool = False,
) -> SkillMigrationView | SkillPreparationView:
    """
    从真实收尾和原始版本构造冲突，不伪造迁移记录。

    :param state (RuntimeHarness): 原账户与会话
    :param root (Path): 私有内容卷
    :param mode (str): 首次向前或显式迁移
    :param linked (bool): 是否保留跨根链接上下文
    :return SkillMigrationView | SkillPreparationView: 已提交原始冲突
    """
    changes: dict[str, bytes | None] = (
        {"aux": b"context", "unrelated": b"extra"}
        if linked
        else {
            "learning/SKILL.md": b"local edit",
            "learning/one": b"learned",
        }
    )
    await publish(
        state,
        root,
        await ingest(
            state,
            root,
            changes,
            links={"learning/link": "../aux"} if linked else None,
        ),
    )
    source, target = await versions(state, root)
    result = (
        await prepare(state, root, await preparation_request(state, root))
        if mode == "forward"
        else await migrate(state, root, await request(state, root, source, target))
    )
    assert result.status == "conflicted" and result.operation_id is not None
    return result


async def counts(state: RuntimeHarness) -> tuple[int | None, ...]:
    """
    对只读请求检查没有新增分支、上传、计划、检查点或迁移。

    :param state (RuntimeHarness): 真实数据库工厂
    :return tuple[int | None, ...]: 关键持久化对象数量
    """
    async with state.database() as session:
        return tuple(
            [
                await session.scalar(select(func.count()).select_from(model))
                for model in (
                    AccountSkillState,
                    SkillCheckpoint,
                    SkillContentUpload,
                    SkillResolutionPlan,
                    SkillBranchPreparation,
                )
            ]
        )


@pytest.mark.parametrize("mode", ["forward", "incremental"])
async def test_saved_sides_and_exports_are_read_only(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
    mode: str,
) -> None:
    """
    首次和显式迁移保留不同真实侧标签，未发布目标的创建不误报漂移。

    :param user_client (AsyncClient): 用户认证客户端
    :param stopped (RuntimeHarness): 已保留会话
    :param tmp_path (Path): 内容卷
    :param mode (str): 原始迁移模式
    """
    saved = await pending(stopped, tmp_path, mode)
    path = f"{BASE}/{saved.operation_id}"
    before = await counts(stopped)
    state = (await command(stopped, tmp_path)).expected
    response = await user_client.get(path)
    assert response.status_code == 200, response.text
    info = response.json()["data"]
    assert info["original"] == saved.model_dump(mode="json")
    assert info["live"]["recomputation_reasons"] == []
    assert info["target_state_id"] == info["live"]["target"]["state_id"]
    assert info["base"]["source"] == "old_original"
    assert info["current"]["source"] == ("new_original" if mode == "forward" else "target_original")
    assert info["incoming"]["source"] == (
        "old_published" if mode == "forward" else "source_published"
    )
    assert info["base"]["revision_id"] == info["incoming"]["revision_id"]
    assert info["current"]["revision_id"] != info["base"]["revision_id"]
    for side in ("base", "current", "incoming", "directory"):
        tree = (await user_client.get(path + "/trees/" + side)).json()["data"]
        assert tree["input"] == info[side]
        assert tree["target_root"] == "learning"
    entry = next(entry for entry in tree["manifest"]["entries"] if entry["path"] == "learning/one")
    digest = entry["sha256"]
    download = await user_client.get(path + "/trees/incoming/files/" + digest)
    assert download.content == b"learned" and download.headers["etag"] == f'"{digest}"'
    assert (await user_client.get(path + "/trees/current/files/" + digest)).status_code == 404
    assert (await user_client.get(path + "/trees/unknown")).status_code == 422
    diff = (await user_client.get(path + "/diff")).json()["data"]
    assert diff["comparison"] == "saved_inputs"
    assert {entry["path"] for entry in diff["items"]} == {"learning/SKILL.md", "learning/one"}
    page = (await user_client.get(BASE, params={"account_id": str(stopped.account)})).json()["data"]
    assert [item["id"] for item in page["items"]] == [str(saved.operation_id)]
    assert before == await counts(stopped)
    assert state == (await command(stopped, tmp_path)).expected


@pytest.mark.parametrize("mode", ["forward", "incremental"])
async def test_linked_export_keeps_context_and_original_paths(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
    mode: str,
) -> None:
    """
    关联冲突原树保留独立上下文根，导出不裁剪或重写外部链接。

    :param user_client (AsyncClient): 认证客户端
    :param stopped (RuntimeHarness): 原会话
    :param tmp_path (Path): 内容卷
    :param mode (str): 迁移模式
    """
    saved = await pending(stopped, tmp_path, mode, linked=True)
    path = f"{BASE}/{saved.operation_id}"
    tree = (await user_client.get(path + "/trees/incoming")).json()["data"]
    assert tree["extra_roots"] == ["aux", "unrelated"]
    entries = {entry["path"]: entry for entry in tree["manifest"]["entries"]}
    assert entries["learning/link"]["target"] == "../aux"
    exported = await user_client.get(path + "/trees/incoming/files/" + entries["aux"]["sha256"])
    assert exported.content == b"context"
    assert saved.conflicts[0].unit == (".", "learning")


async def test_bound_diff_cursor_and_stable_list_pagination(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    同三侧的不同受理仍不能共用差异游标，列表游标也必须属于所选范围。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    """
    first = await pending(stopped, tmp_path)
    assert isinstance(first, SkillMigrationView)
    second = await migrate(
        stopped,
        tmp_path,
        await request(
            stopped,
            tmp_path,
            first.before.source.revision_id,
            first.before.target.revision_id,
        ),
    )
    path = f"{BASE}/{first.operation_id}/diff"
    page = (await user_client.get(path, params={"limit": 1})).json()["data"]
    assert len(page["items"]) == 1 and page["next_cursor"]
    next_page = (
        await user_client.get(path, params={"limit": 1, "cursor": page["next_cursor"]})
    ).json()["data"]
    assert next_page["next_cursor"] is None
    assert page["items"][0]["path"] != next_page["items"][0]["path"]
    assert (
        await user_client.get(
            f"{BASE}/{second.operation_id}/diff", params={"cursor": page["next_cursor"]}
        )
    ).status_code == 422
    bad = json.loads(base64.urlsafe_b64decode(page["next_cursor"]))
    bad[4] = "missing"
    invalid_path = base64.urlsafe_b64encode(json.dumps(bad).encode()).decode()
    for cursor in ("garbage", invalid_path, base64.b64encode(b"[" * 2000).decode()):
        assert (await user_client.get(path, params={"cursor": cursor})).status_code == 422
    params: dict[str, str | int] = {"account_id": str(stopped.account), "limit": 1}
    listed = (await user_client.get(BASE, params=params)).json()["data"]
    assert listed["items"][0]["id"] == str(second.operation_id)
    more = (await user_client.get(BASE, params={**params, "cursor": listed["next_cursor"]})).json()[
        "data"
    ]
    assert more["items"][0]["id"] == str(first.operation_id) and more["next_cursor"] is None
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    account = await library.account()
    assert (
        await user_client.get(
            BASE, params={"account_id": str(account), "cursor": str(first.operation_id)}
        )
    ).status_code == 404
    await library.add(await library.candidate("other", "other-source"))
    assert (
        await user_client.get(
            BASE, params={**params, "skill": "other", "cursor": str(first.operation_id)}
        )
    ).status_code == 404
    assert (await user_client.get(BASE, params={**params, "limit": 201})).status_code == 422


async def test_superseded_and_archived_sources_preserve_original_exports(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    重置及移除只改变实时诊断，稳定身份仍能查看原冲突及原始内容。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    """
    saved = await pending(stopped, tmp_path)
    path = f"{BASE}/{saved.operation_id}"
    tree = (await user_client.get(path + "/trees/incoming")).json()
    await state_execute(stopped, tmp_path, await command(stopped, tmp_path))
    result = (await user_client.get(path)).json()["data"]
    assert result["status"] == "superseded" and result["original"] == saved.model_dump(mode="json")
    assert {
        "superseded",
        "target_epoch_changed",
        "target_head_changed",
        "directory_head_changed",
    } <= set(result["live"]["recomputation_reasons"])
    assert (await user_client.get(path + "/trees/incoming")).json() == tree
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    await library.execute(
        SkillRemoveRequest(
            skill="learning",
            expected_generation=await library.generation(),
            idempotency_key=str(uuid4()),
        )
    )
    current = (await user_client.get(path)).json()["data"]
    assert "installation_removed" in current["live"]["recomputation_reasons"]
    assert "library_generation_changed" in current["live"]["recomputation_reasons"]
    page = await user_client.get(
        BASE, params={"account_id": str(stopped.account), "skill": result["skill_id"]}
    )
    assert page.json()["data"]["items"][0]["id"] == str(saved.operation_id)


@pytest.mark.parametrize("kind", ["device", "node", "other-user"])
async def test_all_conflict_read_routes_reject_other_authorities(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
    kind: str,
) -> None:
    """
    已知原始身份、摘要及账户也不能扩大非所有者凭据的读取权限。

    :param user_client (AsyncClient): 认证客户端
    :param stopped (RuntimeHarness): 目标账户
    :param tmp_path (Path): 私有卷
    :param kind (str): 未授权凭据类别
    """
    saved = await pending(stopped, tmp_path)
    owner = await user(stopped.database) if kind == "other-user" else stopped.owner
    headers = {
        "Authorization": "Bearer "
        + await token(stopped, owner, "user" if kind == "other-user" else kind)
    }
    path = f"{BASE}/{saved.operation_id}"
    for route in (
        BASE,
        path,
        path + "/diff",
        path + "/trees/incoming",
        path + "/trees/incoming/files/" + "0" * 64,
    ):
        response = await user_client.get(
            route, params={"account_id": str(stopped.account)}, headers=headers
        )
        assert response.status_code == (
            404 if kind == "other-user" else 403 if kind == "device" else 401
        )


@pytest.mark.parametrize("damage", ["missing", "corrupt", "directory_expired"])
async def test_unavailable_content_fails_before_streaming(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
    damage: str,
) -> None:
    """
    缺失、损坏文件和过期目录明确失败，不能返回部分内容或当前目录替代品。

    :param user_client (AsyncClient): 用户客户端
    :param stopped (RuntimeHarness): 原账户
    :param tmp_path (Path): 内容卷
    :param damage (str): 故障类型
    """
    saved = await pending(stopped, tmp_path)
    path = f"{BASE}/{saved.operation_id}"
    tree = (await user_client.get(path + "/trees/directory")).json()["data"]
    if damage == "directory_expired":
        async with stopped.database.begin() as session:
            checkpoint = await session.get(SkillCheckpoint, UUID(tree["input"]["checkpoint_id"]))
            assert checkpoint is not None
            checkpoint.retained = False
            checkpoint.tree_digest = None
        response = await user_client.get(path + "/trees/directory")
        assert response.json()["errors"][0]["code"] == "STATE_EXPIRED"
        assert (await user_client.get(path + "/trees/incoming")).status_code == 200
        return
    digest = next(
        entry["sha256"] for entry in tree["manifest"]["entries"] if entry["path"] == "learning/one"
    )
    target = tmp_path / "objects" / str(stopped.owner) / digest[:2] / digest
    if damage == "missing":
        target.unlink()
    else:
        target.chmod(0o600)
        target.write_bytes(b"damaged")
    response = await user_client.get(path + "/trees/incoming/files/" + digest)
    assert response.json()["errors"][0]["code"] == (
        "CONTENT_INCOMPLETE" if damage == "missing" else "CONTENT_INVALID"
    )


async def test_incremental_baseline_labels_and_late_source_are_independent(
    user_client: AsyncClient,
    stopped: RuntimeHarness,
    tmp_path: Path,
) -> None:
    """
    后续冲突标记上次迁移基线与目标自身 head，来源新增不会重写旧输入或冒充重置。

    :param user_client (AsyncClient): 认证客户端
    :param stopped (RuntimeHarness): 旧版账户
    :param tmp_path (Path): 私有卷
    """
    await publish(stopped, tmp_path, await ingest(stopped, tmp_path, {"learning/memory": b"one"}))
    late = await new_session(stopped, tmp_path)
    later = await new_session(stopped, tmp_path)
    source, target = await versions(stopped, tmp_path)
    first = await migrate(stopped, tmp_path, await request(stopped, tmp_path, source, target))
    newer = await new_session(stopped, tmp_path)
    await publish(newer, tmp_path, await ingest(newer, tmp_path, {"learning/memory": b"target"}))
    await publish(late, tmp_path, await ingest(late, tmp_path, {"learning/memory": b"source"}))
    saved = await migrate(stopped, tmp_path, await request(stopped, tmp_path, source, target))
    path = f"{BASE}/{saved.operation_id}"
    info = (await user_client.get(path)).json()["data"]
    assert info["base"]["source"] == "last_migrated"
    assert info["base"]["checkpoint_id"] == str(first.before.source.checkpoint_id)
    assert info["current"]["source"] == "target_published"
    assert info["current"]["checkpoint_id"] == str(saved.before.target.checkpoint_id)
    assert info["live"]["recomputation_reasons"] == []
    await publish(
        later, tmp_path, await ingest(later, tmp_path, {"learning/late": b"newer source"})
    )
    advanced = (await user_client.get(path)).json()["data"]
    assert advanced["live"]["source_head_advanced"]
    assert advanced["live"]["recomputation_reasons"] == ["directory_head_changed"]
    assert advanced["incoming"] == info["incoming"]
    assert advanced["original"] == info["original"]
    assert (await user_client.get(f"{BASE}/{first.operation_id}")).status_code == 404
