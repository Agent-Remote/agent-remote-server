"""
验证普通 HTTP 配置、节点轮询及原结果闭环，不预设尝试或部署任务状态。
"""

from pathlib import Path
from uuid import UUID, uuid4

from httpx import AsyncClient
from skill_deployment_scheduling_support import compatible
from skill_runtime_support import RuntimeHarness
from skill_takeover_support import TakeoverHarness, tree
from skill_takeover_support import takeover as takeover
from sqlalchemy import select
from test_skill_conflicts_api import token
from test_skill_conflicts_api import user_client as user_client
from test_skill_content_service import database as database
from test_skill_deployment_results import proposal
from test_skill_finalization import stopped as stopped
from test_skill_library import LibraryHarness
from test_skill_library import library as library
from test_skill_session_admission import capability
from test_skill_snapshots import prepared as prepared

from agent_remote_server.models import Node, ToolAccount
from agent_remote_server.models.skill_deployment_discovery import SkillDeploymentDiscovery
from agent_remote_server.models.skill_deployment_tasks import SkillDeploymentTask
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.schemas.nodes import NodeTaskEnvelope
from agent_remote_server.schemas.skill_library import SkillAddRequest
from agent_remote_server.services.nodes import NodeService
from agent_remote_server.services.skills.deployment_content import NodeDeploymentContent
from agent_remote_server.services.skills.library import SkillLibraryService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


async def test_http_acceptance_poll_content_confirmation_and_request_replay(
    user_client: AsyncClient, stopped: RuntimeHarness, tmp_path: Path
) -> None:
    """
    用户配置自然进入待执行，普通认证轮询生成任务，只有专用结果才变为就绪。

    :param user_client (AsyncClient): 真实用户认证客户端
    :param stopped (RuntimeHarness): 原账户内容
    :param tmp_path (Path): 私有卷
    """
    await compatible(stopped)
    library = LibraryHarness(stopped.database, tmp_path, stopped.owner)
    request = SkillAddRequest(
        idempotency_key=str(uuid4()),
        expected_generation=await library.generation(),
        items=(await library.candidate(name="notes"),),
    )
    accepted = await user_client.post(
        "/api/v1/skills/installations", json=request.model_dump(mode="json")
    )
    assert accepted.status_code == 200, accepted.text
    result = accepted.json()
    assert result["status"] == "preparing" and result["committed"]
    assert result["data"]["targets"][0]["readiness"] == "pending"
    node_headers = {"Authorization": "Bearer " + await token(stopped, stopped.owner, "node")}
    leased: list[NodeTaskEnvelope] = []
    for _ in range(3):
        polled = await user_client.post("/api/v1/node-api/tasks/poll", headers=node_headers)
        assert polled.status_code == 200, polled.text
        leased.extend(
            NodeTaskEnvelope.model_validate(task)
            for task in polled.json()["data"]["tasks"]
            if task["task_type"] == "prepare_account_skills"
        )
        if leased:
            break
    assert len(leased) == 1
    task = leased[0]
    attempt = result["data"]["targets"][0]["attempt_id"]
    path = f"/api/v1/node/skill-deployments/{attempt}"
    params = {"task_id": str(task.task_record_id), "lease_attempt": str(task.lease_attempt)}
    manifest = await user_client.get(path, headers=node_headers, params=params)
    assert manifest.status_code == 200 and manifest.json()["status"] == "prepared_input"
    async with stopped.database() as session:
        binding = await session.get(SkillDeploymentTask, UUID(attempt))
        assert binding is not None
    evidence = await proposal(stopped, tmp_path, binding)
    confirmed = await user_client.post(
        path + "/result",
        headers=node_headers,
        params={"task_id": str(task.task_record_id)},
        json=evidence.model_dump(mode="json"),
    )
    assert confirmed.status_code == 200 and confirmed.json()["data"]["accepted"], confirmed.text
    status = await user_client.get(f"/api/v1/skills/operations/{result['operation_id']}")
    assert status.status_code == 200 and status.json()["status"] == "ready"
    replay = await user_client.post(
        "/api/v1/skills/installations", json=request.model_dump(mode="json")
    )
    assert replay.json() == status.json()


async def test_ordinary_acceptance_takeover_discovery_then_poll_deployment(
    takeover: TakeoverHarness,
) -> None:
    """
    原受理触发首次接管，真实捕获追加手工来源，再次普通轮询生成完整部署。

    :param takeover (TakeoverHarness): 尚未管理的真实账户
    """
    async with takeover.library.database.begin() as session:
        node = await session.get(Node, takeover.node)
        account = await session.get(ToolAccount, takeover.account)
        assert node is not None and account is not None
        account.status = "active"
        node.supported_tool_types = [account.tool_type]
        node.runtime_capabilities = {
            "backends": ["native"],
            "skill_manager": {"native": capability() | {"deployment_protocol_version": 1}},
        }
    request = SkillAddRequest(
        idempotency_key=str(uuid4()),
        expected_generation=0,
        items=(await takeover.library.candidate(name="installed"),),
    )
    async with takeover.library.database.begin() as session:
        result = await SkillLibraryService(
            session, PrivateObjectStore(takeover.settings.skill_storage_root), takeover.settings
        ).execute(takeover.library.owner, request)
    assert result.status == "preparing"
    async with takeover.library.database() as session:
        node = await session.get(Node, takeover.node)
        assert node is not None
        tasks = await NodeService(session, takeover.settings).poll_tasks(node=node)
        assert len(tasks) == 1 and tasks[0].task_type == "takeover_tool_account_skills"
        receipt = await session.scalar(select(SkillAccountTakeover))
        assert receipt is not None and tasks[0].id == receipt.task_id
    files = {
        "manual/SKILL.md": (
            b"---\nname: manual\ndescription: Existing skill\n---\nSaved instructions\n"
        )
    }
    receipt = await takeover.begin(receipt, takeover.capture(receipt, tree(files)))
    await takeover.transfer(receipt, files)
    await takeover.complete(receipt)
    async with takeover.library.database() as session:
        node = await session.get(Node, takeover.node)
        assert node is not None
        tasks = await NodeService(session, takeover.settings).poll_tasks(node=node)
        assert len(tasks) == 1 and tasks[0].task_type == "prepare_account_skills"
        target = result.data.targets[0]
        assert target.attempt_id is not None
        content = await NodeDeploymentContent(session, takeover.settings).describe(
            takeover.node, tasks[0].id, target.attempt_id, tasks[0].retry_count
        )
        assert {source.name for source in content.plan.sources} == {"installed", "manual"}
        boundary = await session.scalar(select(SkillDeploymentDiscovery))
        assert boundary is not None and boundary.original_digest == target.plan_digest
        assert content.plan_digest == boundary.resolved_digest != boundary.original_digest
