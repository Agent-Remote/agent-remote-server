"""
通过真实配置受理及普通节点轮询驱动后台部署，不直接改写尝试状态。
"""

from pathlib import Path
from uuid import uuid4

from skill_runtime_support import RuntimeHarness
from test_skill_deployment_dispatch import settings
from test_skill_library import LibraryHarness
from test_skill_session_admission import capability, ready

from agent_remote_server.models import Node, NodeTask
from agent_remote_server.schemas.skill_library import SkillAddRequest
from agent_remote_server.schemas.skill_results import SkillMutationData, SkillResult
from agent_remote_server.services.nodes import NodeService
from agent_remote_server.services.skills.library import SkillLibraryService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


async def compatible(state: RuntimeHarness) -> None:
    """
    模拟已通过独立验证的测试能力，不修改真实 Node 广告。

    :param state (RuntimeHarness): 原账户环境
    """
    await ready(state)
    async with state.database.begin() as session:
        node = await session.get(Node, state.node)
        assert node is not None
        node.status = "healthy"
        node.runtime_capabilities = {
            "backends": ["native"],
            "skill_manager": {"native": capability() | {"deployment_protocol_version": 1}},
        }


async def accept(
    state: RuntimeHarness, root: Path, name: str = "notes"
) -> SkillResult[SkillMutationData]:
    """
    完成实际上传和配置保存，让生产服务自行分类首个目标状态。

    :param state (RuntimeHarness): 原用户账户
    :param root (Path): 私有内容卷
    :param name (str): 新来源名称
    :return SkillResult[SkillMutationData]: 原受理结果
    """
    library = LibraryHarness(state.database, root, state.owner)
    request = SkillAddRequest(
        idempotency_key=str(uuid4()),
        expected_generation=await library.generation(),
        items=(await library.candidate(name=name),),
    )
    async with state.database.begin() as session:
        return await SkillLibraryService(
            session, PrivateObjectStore(root / "objects"), settings(root)
        ).execute(state.owner, request)


async def poll(state: RuntimeHarness, root: Path) -> list[NodeTask]:
    """
    使用普通服务入口调度并租用原节点任务。

    :param state (RuntimeHarness): 认证节点环境
    :param root (Path): 私有内容卷
    :return list[NodeTask]: 本次租用中的部署和接管任务，其他旧任务仍由普通轮询处理
    """
    async with state.database() as session:
        node = await session.get(Node, state.node)
        assert node is not None
        tasks = await NodeService(session, settings(root)).poll_tasks(node=node, limit=10)
        return [
            task
            for task in tasks
            if task.task_type in {"prepare_account_skills", "takeover_tool_account_skills"}
        ]
