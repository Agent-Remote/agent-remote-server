"""
验证初始目录提交和通用任务确认分离，失败、篡改与重放不绕过持久凭据。
"""

import pytest
from skill_takeover_support import TakeoverHarness, tree
from skill_takeover_support import takeover as takeover
from sqlalchemy import select
from test_skill_content_service import database as database
from test_skill_library import library as library

from agent_remote_server.models import Node, NodeTask, NodeTaskResult
from agent_remote_server.services.nodes import NodeService
from agent_remote_server.services.skills.content import SkillContentError


async def test_takeover_result_requires_commit_and_exact_replay(takeover: TakeoverHarness) -> None:
    """
    上传前不能确认完成，上传后只能回报不可变的原始六字段凭据。

    :param takeover (TakeoverHarness): 真实账户与内容卷
    """
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    task_id = f"takeover_tool_account_skills:{receipt.id}"
    for failure in (False, True):
        async with takeover.library.database() as session:
            node = await session.get(Node, takeover.node)
            assert node is not None
            service = NodeService(session, takeover.settings)
            with pytest.raises(SkillContentError):
                if failure:
                    await service.fail_task(node=node, task_id=task_id, error={"code": "failed"})
                else:
                    await service.complete_task(node=node, task_id=task_id, result={})
            await session.rollback()
    capture = takeover.capture(receipt, tree({}))
    uploading = await takeover.begin(receipt, capture)
    committed = await takeover.complete(uploading)
    result: dict[str, object] = {
        "status": "committed",
        "takeover_id": str(committed.id),
        "task_record_id": str(committed.task_id),
        "tool_account_id": str(committed.account_id),
        "checkpoint_id": str(committed.checkpoint_id),
        "capture_digest": committed.capture_digest,
    }
    for field in result:
        async with takeover.library.database() as session:
            node = await session.get(Node, takeover.node)
            assert node is not None
            with pytest.raises(SkillContentError, match="differs"):
                await NodeService(session, takeover.settings).complete_task(
                    node=node, task_id=task_id, result={**result, field: "changed"}
                )
    for _ in range(2):
        async with takeover.library.database() as session:
            node = await session.get(Node, takeover.node)
            assert node is not None
            await NodeService(session, takeover.settings).complete_task(
                node=node, task_id=task_id, result=result
            )
    async with takeover.library.database() as session:
        task = await session.get(NodeTask, receipt.task_id)
        assert task is not None and task.status == "succeeded"
        saved = (
            await session.scalars(
                select(NodeTaskResult).where(NodeTaskResult.node_task_id == receipt.task_id)
            )
        ).all()
        assert len(saved) == 1 and saved[0].result == result
        node = await session.get(Node, takeover.node)
        assert node is not None
        with pytest.raises(SkillContentError):
            await NodeService(session, takeover.settings).fail_task(
                node=node, task_id=task_id, error={}
            )


async def test_takeover_result_persistent_binding_survives_type_tampering(
    takeover: TakeoverHarness,
) -> None:
    """
    持久预约识别原任务，改掉任务类型也无法进入旧完成通道。

    :param takeover (TakeoverHarness): 真实账户与内容卷
    """
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    async with takeover.library.database.begin() as session:
        task = await session.get(NodeTask, receipt.task_id)
        assert task is not None
        task.task_type = "reconcile_state"
    async with takeover.library.database() as session:
        node = await session.get(Node, takeover.node)
        assert node is not None
        with pytest.raises(SkillContentError):
            await NodeService(session, takeover.settings).complete_task(
                node=node, task_id=f"takeover_tool_account_skills:{receipt.id}", result={}
            )


@pytest.mark.parametrize("change", ["owner", "payload", "cancelled", "result"])
async def test_takeover_completion_rechecks_original_authority(
    takeover: TakeoverHarness, change: str
) -> None:
    """
    原始接管已提交也不能绕过所有者撤销、任务替换、终态或已保存结果冲突。

    :param takeover (TakeoverHarness): 真实账户与内容卷
    :param change (str): 提交后篡改的授权边界
    """
    from agent_remote_server.models import User

    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    uploading = await takeover.begin(receipt, takeover.capture(receipt, tree({})))
    committed = await takeover.complete(uploading)
    result: dict[str, object] = {
        "status": "committed",
        "takeover_id": str(committed.id),
        "task_record_id": str(committed.task_id),
        "tool_account_id": str(committed.account_id),
        "checkpoint_id": str(committed.checkpoint_id),
        "capture_digest": committed.capture_digest,
    }
    task_id = f"takeover_tool_account_skills:{receipt.id}"
    if change == "result":
        async with takeover.library.database() as session:
            node = await session.get(Node, takeover.node)
            assert node is not None
            await NodeService(session, takeover.settings).complete_task(
                node=node, task_id=task_id, result=result
            )
    async with takeover.library.database.begin() as session:
        task = await session.get(NodeTask, receipt.task_id)
        assert task is not None
        if change == "owner":
            owner = await session.get(User, takeover.library.owner)
            assert owner is not None
            owner.status = "disabled"
        elif change == "payload":
            task.payload = {**task.payload, "inventory_digest": "0" * 64}
        elif change == "cancelled":
            task.status = "cancelled"
        else:
            saved = await session.scalar(
                select(NodeTaskResult).where(NodeTaskResult.node_task_id == receipt.task_id)
            )
            assert saved is not None
            saved.result = {"status": "changed"}
    async with takeover.library.database() as session:
        node = await session.get(Node, takeover.node)
        assert node is not None
        with pytest.raises(SkillContentError):
            await NodeService(session, takeover.settings).complete_task(
                node=node, task_id=task_id, result=result
            )
