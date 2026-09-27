"""
绕过服务验证首次接管的复合归属、状态、摘要、范围和唯一性约束。
"""

from uuid import uuid4

import pytest
from skill_takeover_support import TakeoverHarness, tree
from skill_takeover_support import takeover as takeover
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from test_skill_content_service import database as database
from test_skill_content_service import user
from test_skill_library import library as library

from agent_remote_server.models import NodeTask, ToolAccount
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, SkillCheckpoint
from agent_remote_server.models.skill_takeover import SkillAccountTakeover


@pytest.mark.parametrize(
    "invalid",
    [
        "owner",
        "task_node",
        "missing_task",
        "backend",
        "zero_epoch",
        "negative_attempt",
        "unknown_status",
        "reserved_capture",
        "reserved_helper",
        "reserved_attempt",
        "uploading_empty",
        "committed_empty",
        "upload_scope",
        "checkpoint_scope",
    ],
)
async def test_reserved_receipt_constraints(takeover: TakeoverHarness, invalid: str) -> None:
    """
    无需服务检查，数据库也拒绝不一致的预约身份和阶段字段。

    :param takeover (TakeoverHarness): 未接管账户
    :param invalid (str): 被破坏的数据库不变量
    """
    receipt = await takeover.reserve()
    variants: dict[str, dict[str, object]] = {
        "owner": {"user_id": await user(takeover.library.database)},
        "task_node": {"node_id": uuid4()},
        "missing_task": {"task_id": uuid4()},
        "backend": {"runtime_backend": "unknown"},
        "zero_epoch": {"directory_epoch": 0},
        "negative_attempt": {"upload_attempt": -1},
        "unknown_status": {"status": "lost"},
        "reserved_capture": {"capture_digest": "0" * 64},
        "reserved_helper": {"helper_receipt_id": uuid4()},
        "reserved_attempt": {"upload_attempt": 1},
        "uploading_empty": {"status": "uploading"},
        "committed_empty": {"status": "committed"},
        "upload_scope": {"upload_scope": "package"},
        "checkpoint_scope": {"checkpoint_scope": "item"},
    }
    with pytest.raises(IntegrityError):
        async with takeover.library.database.begin() as session:
            await session.execute(
                update(SkillAccountTakeover)
                .where(SkillAccountTakeover.id == receipt.id)
                .values(**variants[invalid])
            )


@pytest.mark.parametrize(
    "invalid",
    [
        "foreign_upload",
        "upload_digest",
        "upload_scope",
        "foreign_checkpoint",
        "checkpoint_digest",
        "missing_helper",
    ],
)
async def test_capture_references_require_exact_owner_digest_scope(
    takeover: TakeoverHarness, invalid: str
) -> None:
    """
    已完成树也不能授权跨用户上传或其他账户的目录检查点。

    :param takeover (TakeoverHarness): 未接管账户
    :param invalid (str): 被替换的引用维度
    """
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    receipt = await takeover.begin(receipt, takeover.capture(receipt, tree({})))
    receipt = await takeover.complete(receipt)
    foreign_owner = await user(takeover.library.database)
    other_account = await takeover.library.account()
    async with takeover.library.database.begin() as session:
        content = takeover.service(session).context.content
        foreign = await content.begin(foreign_owner, "foreign", tree({}), "account_directory")
        wrong_scope = await content.begin(takeover.library.owner, "package", tree({}), "package")
        other_tree = await content.begin(
            takeover.library.owner, "other-tree", tree({"empty": b""}), "account_directory"
        )
        session.add(
            AccountSkillDirectoryState(
                user_id=takeover.library.owner, account_id=other_account, tool_type="claude"
            )
        )
        await session.flush()
        checkpoint = SkillCheckpoint(
            id=uuid4(),
            user_id=takeover.library.owner,
            account_id=other_account,
            scope="directory",
            content_digest=receipt.capture_digest,
            tree_digest=receipt.capture_digest,
        )
        session.add(checkpoint)
        await session.flush()
        variants: dict[str, dict[str, object]] = {
            "foreign_upload": {"upload_id": foreign.id},
            "upload_digest": {"upload_id": other_tree.id},
            "upload_scope": {"upload_id": wrong_scope.id},
            "foreign_checkpoint": {"checkpoint_id": checkpoint.id},
            "checkpoint_digest": {
                "capture_digest": other_tree.tree_digest,
                "upload_id": other_tree.id,
            },
            "missing_helper": {"helper_receipt_id": None},
        }
    with pytest.raises(IntegrityError):
        async with takeover.library.database.begin() as session:
            await session.execute(
                update(SkillAccountTakeover)
                .where(SkillAccountTakeover.id == receipt.id)
                .values(**variants[invalid])
            )


@pytest.mark.parametrize("invalid", ["account", "key", "task"])
async def test_only_one_receipt_per_account_key_and_task(
    takeover: TakeoverHarness, invalid: str
) -> None:
    """
    并行入口也不能共享请求键、精确任务或重复账户权威。

    :param takeover (TakeoverHarness): 未接管账户
    :param invalid (str): 被重复使用的身份
    """
    first = await takeover.reserve()
    account_id = await takeover.library.account()
    async with takeover.library.database.begin() as session:
        account = await session.get(ToolAccount, account_id)
        assert account is not None
        account.affinity_node_id, account.runtime_backend = takeover.node, "native"
    second_harness = TakeoverHarness(takeover.library, takeover.node, account_id, takeover.settings)
    second = await second_harness.reserve(key="second")
    variants: dict[str, dict[str, object]] = {
        "account": {"account_id": first.account_id},
        "key": {"idempotency_key": first.idempotency_key},
        "task": {"task_id": first.task_id},
    }
    with pytest.raises(IntegrityError):
        async with takeover.library.database.begin() as session:
            await session.execute(
                update(SkillAccountTakeover)
                .where(SkillAccountTakeover.id == second.id)
                .values(**variants[invalid])
            )
    with pytest.raises(IntegrityError):
        async with takeover.library.database.begin() as session:
            task = await session.get(NodeTask, first.task_id)
            assert task is not None
            await session.delete(task)
