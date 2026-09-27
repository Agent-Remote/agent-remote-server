"""
验证实际内容额度、系统路径排除及本地来源与用户库冲突。
"""

from uuid import uuid4

import pytest
from skill_takeover_support import TakeoverHarness, legacy_session, tree
from skill_takeover_support import takeover as takeover
from sqlalchemy import func, select
from test_skill_content_service import database as database
from test_skill_library import library as library

from agent_remote_server.models import NodeTask
from agent_remote_server.models.skill_local import AccountLocalSkill
from agent_remote_server.models.skill_state import AccountSkillDirectoryState, SkillCheckpoint
from agent_remote_server.models.skill_storage import SkillContentUpload
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.snapshots import SkillSnapshotService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


async def test_library_name_collision_preserves_manual_source_and_denies_snapshot(
    takeover: TakeoverHarness,
) -> None:
    """
    同名用户库安装不能夺走原手工目录，后续会话准备必须报告来源冲突。

    :param takeover (TakeoverHarness): 未接管账户
    """
    installed = await takeover.library.add(await takeover.library.candidate())
    files = {"learning/SKILL.md": b"# Manual instructions\n"}
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    receipt = await takeover.begin(receipt, takeover.capture(receipt, tree(files)))
    await takeover.transfer(receipt, files)
    receipt = await takeover.complete(receipt)
    session_id = await legacy_session(takeover, "starting")
    task_id = uuid4()
    async with takeover.library.database.begin() as session:
        session.add(
            NodeTask(
                id=task_id,
                node_id=takeover.node,
                task_id=str(task_id),
                task_type="create_tool_session",
                status="pending",
                payload={
                    "user_id": str(takeover.library.owner),
                    "tool_account_id": str(takeover.account),
                    "session_id": str(session_id),
                    "runtime_backend": "native",
                },
            )
        )
    with pytest.raises(SkillContentError) as error:
        async with takeover.library.database.begin() as session:
            await SkillSnapshotService(
                session,
                PrivateObjectStore(takeover.settings.skill_storage_root),
                takeover.settings.skill_storage_policy,
            ).reserve(takeover.library.owner, session_id, task_id, {})
    assert error.value.code == "SKILL_SOURCE_CONFLICT"
    async with takeover.library.database() as session:
        local = await session.scalar(
            select(AccountLocalSkill).where(AccountLocalSkill.account_id == takeover.account)
        )
        assert local is not None and local.source_checkpoint_id == receipt.checkpoint_id
    with pytest.raises(SkillContentError) as error:
        await takeover.library.info()
    assert error.value.code == "SKILL_SOURCE_CONFLICT"
    assert (await takeover.library.info(str(installed.data.skill_ids[0]))).name == "learning"


@pytest.mark.parametrize("reserved", ["ego-browser", "agent-remote-device"])
async def test_system_paths_cannot_enter_takeover_state(
    takeover: TakeoverHarness, reserved: str
) -> None:
    """
    首次接管不能把系统技能文件改成账户可写状态。

    :param takeover (TakeoverHarness): 未接管账户
    :param reserved (str): 系统保留根目录
    """
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    with pytest.raises(SkillContentError) as error:
        await takeover.begin(
            receipt, takeover.capture(receipt, tree({reserved + "/SKILL.md": b"x"}))
        )
    assert error.value.code == "SYSTEM_SKILL_IMMUTABLE"


async def test_persisted_invalid_skills_share_root_auxiliary_quota(
    takeover: TakeoverHarness,
) -> None:
    """
    无效说明不能在上传前冒充多个技能而拆分根级辅助数据额度。

    :param takeover (TakeoverHarness): 未接管账户
    """
    takeover.settings = takeover.settings.model_copy(
        update={"skill_storage_policy": SkillStoragePolicy(checkpoint_bytes=64)}
    )
    bad = b"---\nname: [invalid\n---\n" + b"x" * 25
    files = {"one/SKILL.md": bad, "two/SKILL.md": bad}
    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    receipt = await takeover.begin(receipt, takeover.capture(receipt, tree(files)))
    await takeover.transfer(receipt, files)
    with pytest.raises(SkillContentError) as error:
        await takeover.complete(receipt)
    assert error.value.code == "QUOTA_EXCEEDED"
    async with takeover.library.database() as session:
        directory = await session.get(AccountSkillDirectoryState, takeover.account)
        assert directory is not None and directory.mode == "migrating"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(SkillCheckpoint)
                .where(SkillCheckpoint.account_id == takeover.account)
            )
            == 0
        )
        upload = await session.get(SkillContentUpload, receipt.upload_id)
        assert upload is not None and upload.status == "staged"


async def test_lost_capability_before_completion_does_not_publish(
    takeover: TakeoverHarness,
) -> None:
    """
    预约时能力通过不能替代权威提交时的新鲜能力检查。

    :param takeover (TakeoverHarness): 未接管账户
    """
    from agent_remote_server.models import Node

    receipt = await takeover.reserve()
    await takeover.lease(receipt)
    receipt = await takeover.begin(receipt, takeover.capture(receipt, tree({})))
    async with takeover.library.database.begin() as session:
        node = await session.get(Node, takeover.node)
        assert node is not None
        node.runtime_capabilities = {}
    with pytest.raises(SkillContentError) as error:
        await takeover.complete(receipt)
    assert error.value.code == "SKILL_MANAGER_UNSUPPORTED"
