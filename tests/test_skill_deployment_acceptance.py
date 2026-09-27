"""
验证真实配置按能力受理待执行目标，离线不伪装为就绪或不支持。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from skill_deployment_scheduling_support import accept, compatible
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified
from test_skill_content_service import database as database
from test_skill_library import LibraryHarness
from test_skill_session_admission import capability
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import Node, NodeTask, ToolAccount
from agent_remote_server.models.skill_library import SkillOperation
from agent_remote_server.repositories.skill_deployment_attempts import (
    SkillDeploymentAttemptRepository,
)
from agent_remote_server.services.skills.deployment_attempts import current_attempts


@pytest.mark.parametrize("availability", ["healthy", "offline", "stale"])
async def test_compatible_acceptance_records_pending_without_task(
    prepared: RuntimeHarness, tmp_path: Path, availability: str
) -> None:
    """
    已知协议兼容的离线目标仍持久等待，受理不创建执行任务。

    :param prepared (RuntimeHarness): 原账户环境
    :param tmp_path (Path): 私有卷
    :param availability (str): 当前心跳可用性
    """
    await compatible(prepared)
    async with prepared.database.begin() as session:
        node = await session.get(Node, prepared.node)
        assert node is not None
        if availability == "offline":
            node.status = "offline"
        if availability != "healthy":
            node.last_heartbeat_at = datetime.now(UTC) - timedelta(days=1)
        count = await session.scalar(select(func.count()).select_from(NodeTask))
    result = await accept(prepared, tmp_path)
    assert result.status == "preparing" and result.committed and not result.retryable
    assert result.data.targets[0].readiness == "pending"
    assert result.data.targets[0].error_code is None
    async with prepared.database() as session:
        operation = await session.get(SkillOperation, result.operation_id)
        assert operation is not None
        current = current_attempts(
            operation,
            await SkillDeploymentAttemptRepository(session).attempts(prepared.owner, operation.id),
        )
        assert current[prepared.account].status == "pending"
        assert await session.scalar(select(func.count()).select_from(NodeTask)) == count
        observed = (
            await LibraryHarness(prepared.database, tmp_path, prepared.owner)
            .service(session)
            .status(prepared.owner, operation.id)
        )
        assert observed == result


@pytest.mark.parametrize(
    "boundary", ["report", "boolean", "backend", "policy", "tool", "heartbeat", "disabled"]
)
async def test_incompatible_acceptance_is_terminal_unsupported(
    prepared: RuntimeHarness, tmp_path: Path, boundary: str
) -> None:
    """
    缺少完整能力的旧节点不能通过无限等待伪装成已接入。

    :param prepared (RuntimeHarness): 原账户环境
    :param tmp_path (Path): 私有卷
    :param boundary (str): 被撤回的能力边界
    """
    await compatible(prepared)
    async with prepared.database.begin() as session:
        node = await session.get(Node, prepared.node)
        account = await session.get(ToolAccount, prepared.account)
        assert node is not None and account is not None
        if boundary == "report":
            node.runtime_capabilities = {"backends": ["native"]}
        elif boundary == "boolean":
            node.runtime_capabilities = {
                "backends": ["native"],
                "skill_manager": {"native": capability() | {"deployment_protocol_version": True}},
            }
            # 故意保存未归一化旧报告；普通心跳会移除该字段，而 ORM 默认视 True 与 1 相等。
            flag_modified(node, "runtime_capabilities")
        elif boundary == "backend":
            account.runtime_backend = "docker_sandbox"
        elif boundary == "policy":
            node.allowed_runtime_backends = ["docker_sandbox"]
        elif boundary == "tool":
            node.supported_tool_types = []
        elif boundary == "heartbeat":
            node.last_heartbeat_at = None
        else:
            node.status = "disabled"
    result = await accept(prepared, tmp_path)
    assert result.status == "failed" and result.committed
    assert result.data.targets[0].readiness == "unsupported"
    assert result.data.targets[0].error_code == "SKILL_MANAGER_UNSUPPORTED"
