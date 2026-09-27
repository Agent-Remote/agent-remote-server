"""
验证接管保留所有旧资源身份，控制面终态仅是静止证明的必要条件。
"""

from uuid import uuid4

import pytest
from skill_takeover_support import TakeoverHarness, legacy_session, tree, writer_task
from skill_takeover_support import takeover as takeover
from test_skill_content_service import database as database
from test_skill_library import library as library

from agent_remote_server.models import Node, NodeTask, Session
from agent_remote_server.services.skills.content import SkillContentError


async def test_historical_resources_survive_terminal_states_and_row_deletion(
    takeover: TakeoverHarness,
) -> None:
    """
    所有历史绑定、导入、迁移和会话都进入清单，未知原后端保持空值。

    :param takeover (TakeoverHarness): 未接管账户
    """
    tasks = [
        await writer_task(takeover, kind) for kind in ("binding", "import", "backend", "session")
    ]
    old_session = await legacy_session(takeover, "stopped")
    receipt = await takeover.reserve()
    assert len(receipt.inventory_json) == 5
    assert {item["kind"] for item in receipt.inventory_json} == {
        "session",
        "binding",
        "import",
        "backend",
    }
    assert all(
        item["runtime_backend"] is None for item in receipt.inventory_json if item["task_id"]
    )
    assert any(item["resource_id"] == str(old_session) for item in receipt.inventory_json)
    assert all("files" not in item and "path" not in item for item in receipt.inventory_json)
    async with takeover.library.database.begin() as session:
        task = await session.get(NodeTask, tasks[0].id)
        assert task is not None
        await session.delete(task)
    await takeover.lease(receipt)
    uploaded = await takeover.begin(receipt, takeover.capture(receipt, tree({})))
    done = await takeover.complete(uploaded)
    assert done.inventory_json == receipt.inventory_json


@pytest.mark.parametrize("kind", ["binding", "import", "backend", "session", "session_row"])
async def test_active_writer_blocks_capture_until_normal_terminal_state(
    takeover: TakeoverHarness, kind: str
) -> None:
    """
    预约不强停写入者，全部正常结束后才允许捕获并保持原清单。

    :param takeover (TakeoverHarness): 未接管账户
    :param kind (str): 控制面仍忙的资源类型
    """
    resource = (
        await legacy_session(takeover)
        if kind == "session_row"
        else (await writer_task(takeover, kind, "running")).id
    )
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    capture = takeover.capture(receipt, tree({}))
    with pytest.raises(SkillContentError) as error:
        await takeover.begin(receipt, capture)
    assert error.value.code == "STATE_WRITERS_ACTIVE"
    async with takeover.library.database.begin() as session:
        if kind == "session_row":
            old_session = await session.get(Session, resource)
            assert old_session is not None and old_session.status == "running"
            old_session.status = "stopped"
        else:
            task = await session.get(NodeTask, resource)
            assert task is not None and task.status == "running"
            task.status = "succeeded"
    uploaded = await takeover.begin(receipt, capture)
    assert (await takeover.complete(uploaded)).status == "committed"


async def test_new_writer_after_reservation_blocks_capture_and_completion(
    takeover: TakeoverHarness,
) -> None:
    """
    即使新增任务已取消，也不能跳过原清单之外的潜在旧进程。

    :param takeover (TakeoverHarness): 未接管账户
    """
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    capture = takeover.capture(receipt, tree({}))
    receipt = await takeover.begin(receipt, capture)
    await writer_task(takeover)
    for operation in (takeover.begin(receipt, capture), takeover.complete(receipt)):
        with pytest.raises(SkillContentError) as error:
            await operation
        assert error.value.code == "STATE_WRITERS_CHANGED"


async def test_plain_configuration_import_is_not_a_skill_writer(takeover: TakeoverHarness) -> None:
    """
    普通配置导入仍可继续，不将与技能目录无关的活跃任务当成阻塞者。

    :param takeover (TakeoverHarness): 未接管账户
    """
    old = await writer_task(takeover, "import", "running")
    async with takeover.library.database.begin() as session:
        task = await session.get(NodeTask, old.id)
        assert task is not None
        task.payload = {**task.payload, "files": [{"path": "~/.claude/settings.json"}]}
    receipt = await takeover.reserve()
    assert receipt.inventory_json == []
    await takeover.lease(receipt)
    receipt = await takeover.begin(receipt, takeover.capture(receipt, tree({})))
    assert (await takeover.complete(receipt)).status == "committed"


async def test_foreign_node_resource_requires_explicit_reconciliation(
    takeover: TakeoverHarness,
) -> None:
    """
    单节点捕获不能代表另一节点历史进程已静止。

    :param takeover (TakeoverHarness): 未接管账户
    """
    foreign = uuid4()
    async with takeover.library.database.begin() as session:
        session.add(Node(id=foreign, name="旧节点", status="offline", region_code="global"))
    await writer_task(takeover, node_id=foreign)
    with pytest.raises(SkillContentError) as error:
        await takeover.reserve()
    assert error.value.code == "STATE_WRITERS_UNKNOWN"


@pytest.mark.parametrize("kind", ["binding", "session_row"])
async def test_malformed_history_fails_closed_with_stable_error(
    takeover: TakeoverHarness, kind: str
) -> None:
    """
    无法验证的原资源身份不能被忽略或泄漏底层校验异常。

    :param takeover (TakeoverHarness): 未接管账户
    :param kind (str): 损坏的历史记录种类
    """
    if kind == "binding":
        old = await writer_task(takeover)
        async with takeover.library.database.begin() as session:
            task = await session.get(NodeTask, old.id)
            assert task is not None
            task.payload = {**task.payload, "binding_id": "/host/path"}
    else:
        old_session = await legacy_session(takeover, "stopped")
        async with takeover.library.database.begin() as session:
            record = await session.get(Session, old_session)
            assert record is not None
            record.runtime_backend = "unknown"
    with pytest.raises(SkillContentError) as error:
        await takeover.reserve()
    assert error.value.code == "STATE_WRITERS_UNKNOWN"
