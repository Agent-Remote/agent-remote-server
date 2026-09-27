"""
验证部署下载授权依赖精确任务、租约、原计划及未失效状态输入。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from skill_runtime_support import RuntimeHarness
from sqlalchemy import func, select, update
from test_skill_content_service import database as database
from test_skill_deployment_dispatch import pending, settings
from test_skill_session_admission import capability
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import Node, NodeTask, NodeTaskResult, ToolAccount, User
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_library import SkillInstallation
from agent_remote_server.models.skill_state import (
    AccountSkillDirectoryState,
    AccountSkillState,
    SkillDirectoryMember,
)
from agent_remote_server.services.nodes import NodeService
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.deployment_authorization import authorize_deployment
from agent_remote_server.services.skills.deployment_dispatch import SkillDeploymentDispatch
from agent_remote_server.services.skills.runtime_capability import normalize_skill_capabilities


async def leased(state: RuntimeHarness, root: Path) -> SkillDeploymentTask:
    """
    使用实际预约服务固定输入，再模拟 Node 的精确任务领取。

    :param state (RuntimeHarness): 原账户
    :param root (Path): 私有卷
    :return SkillDeploymentTask: 已领取的真实持久绑定
    """
    operation_id, attempt_id = await pending(state, root)
    async with state.database.begin() as session:
        binding = await SkillDeploymentDispatch(session, settings(root)).reserve(
            state.owner, operation_id, state.account, attempt_id
        )
        assert isinstance(binding, SkillDeploymentTask)
        task = await session.get(NodeTask, binding.task_id)
        assert task is not None
        task.status = "leased"
        task.retry_count = 1
        task.lease_until = datetime.now(UTC) + timedelta(seconds=60)
        return binding


@pytest.mark.parametrize(
    "change,code",
    [
        ("foreign_node", "DEPLOYMENT_NOT_FOUND"),
        ("foreign_task", "DEPLOYMENT_NOT_FOUND"),
        ("foreign_attempt", "DEPLOYMENT_NOT_FOUND"),
        ("poll_attempt", "DEPLOYMENT_LEASE_CHANGED"),
        ("boolean_attempt", "DEPLOYMENT_LEASE_CHANGED"),
        ("expired", "DEPLOYMENT_LEASE_CHANGED"),
        ("cancelled", "DEPLOYMENT_LEASE_CHANGED"),
        ("type", "DEPLOYMENT_LEASE_CHANGED"),
        ("payload", "DEPLOYMENT_LEASE_CHANGED"),
        ("boolean_protocol", "DEPLOYMENT_LEASE_CHANGED"),
        ("binding", "DEPLOYMENT_BINDING_CHANGED"),
        ("inactive", "ACCOUNT_NOT_AVAILABLE"),
        ("owner", "AUTHORIZATION_DENIED"),
        ("selection", "DEPLOYMENT_PLAN_CHANGED"),
        ("directory_epoch", "DEPLOYMENT_INPUT_CHANGED"),
        ("state_epoch", "DEPLOYMENT_INPUT_CHANGED"),
        ("capability", "SKILL_MANAGER_UNSUPPORTED"),
        ("heartbeat", "SKILL_MANAGER_UNSUPPORTED"),
        ("disabled", "SKILL_MANAGER_DISABLED"),
    ],
)
async def test_each_authority_boundary_is_rechecked(
    prepared: RuntimeHarness, tmp_path: Path, change: str, code: str
) -> None:
    """
    先通过一次授权，再独立撤销每项权限，不能使用旧 ORM 状态继续下载。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param change (str): 单项撤销边界
    :param code (str): 预期稳定拒绝原因
    """
    binding = await leased(prepared, tmp_path)
    configured = settings(tmp_path)
    async with prepared.database.begin() as session:
        assert (
            await authorize_deployment(
                session, configured, prepared.node, binding.task_id, binding.attempt_id, 1
            )
        ).checkpoint_id == binding.checkpoint_id
        task = await session.get(NodeTask, binding.task_id)
        account = await session.get(ToolAccount, prepared.account)
        node = await session.get(Node, prepared.node)
        directory = await session.get(AccountSkillDirectoryState, prepared.account)
        assert (
            task is not None and account is not None and node is not None and directory is not None
        )
        if change == "expired":
            task.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "cancelled":
            task.status = "cancelled"
        elif change == "type":
            task.task_type = "health_check"
        elif change == "payload":
            task.payload = task.payload | {"checkpoint_id": str(uuid4())}
        elif change == "boolean_protocol":
            await session.execute(
                update(NodeTask)
                .where(NodeTask.id == task.id)
                .values(payload=task.payload | {"protocol_version": True})
                .execution_options(synchronize_session=False)
            )
        elif change == "binding":
            account.affinity_node_id = None
        elif change == "inactive":
            account.status = "disabled"
        elif change == "owner":
            owner = await session.get(User, prepared.owner)
            assert owner is not None
            owner.status = "disabled"
        elif change == "selection":
            item = await session.scalar(
                select(SkillInstallation).where(
                    SkillInstallation.user_id == prepared.owner, SkillInstallation.name == "notes"
                )
            )
            assert item is not None
            item.default_enabled = False
        elif change == "directory_epoch":
            directory.epoch += 1
        elif change == "state_epoch":
            member = await session.scalar(
                select(SkillDirectoryMember).where(
                    SkillDirectoryMember.directory_checkpoint_id == binding.checkpoint_id
                )
            )
            assert member is not None
            branch = await session.get(AccountSkillState, member.state_id)
            assert branch is not None
            branch.epoch += 1
        elif change == "capability":
            node.runtime_capabilities = {"backends": ["native"]}
        elif change == "heartbeat":
            node.last_heartbeat_at = datetime.now(UTC) - timedelta(days=1)
        elif change == "disabled":
            configured.skill_manager_enabled = False
        await session.flush()
        with pytest.raises(SkillContentError) as error:
            await authorize_deployment(
                session,
                configured,
                uuid4() if change == "foreign_node" else prepared.node,
                uuid4() if change == "foreign_task" else binding.task_id,
                uuid4() if change == "foreign_attempt" else binding.attempt_id,
                True if change == "boolean_attempt" else 2 if change == "poll_attempt" else 1,
            )
        assert error.value.code == code


@pytest.mark.parametrize("tampered", [False, True])
@pytest.mark.parametrize("outcome", ["complete", "fail"])
async def test_generic_result_cannot_end_or_forge_deployment(
    prepared: RuntimeHarness, tmp_path: Path, tampered: bool, outcome: str
) -> None:
    """
    即使任务类型与载荷被覆盖，持久绑定仍要求专用部署结果而非通用成功或失败。

    :param prepared (RuntimeHarness): 原账户
    :param tmp_path (Path): 私有卷
    :param tampered (bool): 是否覆盖可变任务标记
    :param outcome (str): 通用结果入口
    """
    binding = await leased(prepared, tmp_path)
    async with prepared.database.begin() as session:
        node = await session.get(Node, prepared.node)
        task = await session.get(NodeTask, binding.task_id)
        assert node is not None and task is not None
        if tampered:
            task.task_type, task.payload = "health_check", {}
        await session.flush()
        service = NodeService(session, settings(tmp_path))
        with pytest.raises(SkillContentError) as error:
            if outcome == "complete":
                await service.complete_task(
                    node=node, task_id=task.task_id, result={"status": "ready"}
                )
            else:
                await service.fail_task(node=node, task_id=task.task_id, error={"code": "FAILED"})
        assert error.value.code == "DEPLOYMENT_RESULT_REQUIRED"
        assert task.status == "leased"
        assert await session.scalar(select(func.count()).select_from(NodeTaskResult)) == 0


@pytest.mark.parametrize("version", [True, False, "1", 1.0, 2, None])
def test_invalid_optional_deployment_report_cannot_compare_equal_to_prior_authority(
    version: object,
) -> None:
    """
    无效可选字段必须产生不同数据库值，保留基本会话能力但不保留旧部署授权。

    :param version (object): 不受信可选部署版本
    """
    original = capability() | {"deployment_protocol_version": 1}
    result = normalize_skill_capabilities(
        {"native": original | {"deployment_protocol_version": version}}
    )
    assert result == {"native": capability()}
    assert result != {"native": original}
    assert normalize_skill_capabilities({"native": original}) == {"native": original}
