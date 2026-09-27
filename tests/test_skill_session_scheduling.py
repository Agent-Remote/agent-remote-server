"""
验证受管能力只筛选允许迁移的候选，活动账户仍固定原节点。
"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from test_skill_content_service import database as database
from test_skill_session_admission import capability, launch, ready
from test_skill_snapshots import prepared as prepared

from agent_remote_server.errors import ApiError
from agent_remote_server.models import Node, Session


@pytest.mark.parametrize("active", [False, True])
async def test_compatible_candidate_is_selected_only_when_no_active_session_pins_the_account(
    prepared: RuntimeHarness, tmp_path: Path, active: bool
) -> None:
    """
    空闲账户可选择具备技能能力的节点，活动会话存在时不得静默换节点。

    :param prepared (RuntimeHarness): 已接管账户
    :param tmp_path (Path): 内容卷
    :param active (bool): 原节点是否有活动会话
    """
    await ready(prepared)
    candidate = uuid4()
    async with prepared.database.begin() as session:
        original = await session.get(Node, prepared.node)
        assert original is not None
        original.runtime_capabilities = {"backends": ["native"]}
        if active:
            running = await session.get(Session, prepared.session)
            assert running is not None
            running.status = "running"
        session.add(
            Node(
                id=candidate,
                name="compatible",
                status="healthy",
                region_code=prepared.owner.hex,
                supported_tool_types=["claude"],
                allowed_runtime_backends=["native"],
                default_runtime_backend="native",
                runtime_capabilities={
                    "backends": ["native"],
                    "skill_manager": {"native": capability()},
                },
                last_heartbeat_at=datetime.now(UTC),
                version="test-release",
            )
        )
    if active:
        with pytest.raises(ApiError) as error:
            await launch(prepared, tmp_path)
        assert error.value.code == "SKILL_MANAGER_UNSUPPORTED"
    else:
        assert (await launch(prepared, tmp_path)).node_id == candidate
