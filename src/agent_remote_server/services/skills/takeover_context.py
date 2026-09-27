"""
核实接管的精确任务授权与历史旧写入者，保持 Server 状态和本地静止证明分离。
"""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.models import ToolAccount
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.repositories.skill_takeover import SkillTakeoverRepository
from agent_remote_server.schemas.skill_takeover import SkillTakeoverWriter
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.services.skills.runtime_capability import supports_managed_skills
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore

_TERMINAL_TASKS = {"succeeded", "failed", "cancelled", "expired"}


class TakeoverContext:
    """
    每次请求重新核对精确归属，不缓存跨租约授权。
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """
        共享外层用户锁、事务和内容卷。

        :param session (AsyncSession): 外层事务
        :param settings (Settings): 部署配置
        """
        self.session = session
        self.settings = settings
        self.repository = SkillTakeoverRepository(session)
        self.runtime = SkillRuntimeRepository(session)
        self.storage = SkillStorageRepository(session)
        self.store = PrivateObjectStore(settings.skill_storage_root)
        self.content = SkillContentService(session, self.store, settings.skill_storage_policy)

    async def supported(self, account: ToolAccount) -> None:
        """
        只有固定后端完整可用时才允许预约或提交目录权威。

        :param account (ToolAccount): 已授权账户
        """
        node = (
            await self.repository.node(account.affinity_node_id)
            if account.affinity_node_id
            else None
        )
        backend = account.runtime_backend
        available = node.runtime_capabilities.get("backends") if node is not None else None
        if (
            node is None
            or backend is None
            or backend not in {"native", "docker_sandbox"}
            or node.status not in {"healthy", "degraded"}
            or backend not in node.allowed_runtime_backends
            or not isinstance(available, list)
            or backend not in available
            or not supports_managed_skills(node, backend, self.settings)
        ):
            raise SkillContentError(
                "SKILL_MANAGER_UNSUPPORTED", "account node lacks verified backend support"
            )

    async def inventory(
        self, user_id: UUID, account_id: UUID
    ) -> tuple[list[SkillTakeoverWriter], bool]:
        """
        保留终态资源身份，但分别返回当前控制面是否仍有未完成写入者。

        :param user_id (UUID): 所有者身份
        :param account_id (UUID): 账户身份
        :return tuple[list[SkillTakeoverWriter], bool]: 稳定资源清单与控制面忙标记
        """
        writers: list[SkillTakeoverWriter] = []
        busy = False
        for session in await self.repository.sessions(user_id, account_id):
            try:
                writers.append(
                    SkillTakeoverWriter.model_validate(
                        {
                            "kind": "session",
                            "node_id": session.node_id,
                            "resource_id": str(session.id),
                            "runtime_backend": session.runtime_backend,
                        }
                    )
                )
            except ValidationError as error:
                raise SkillContentError(
                    "STATE_WRITERS_UNKNOWN", "legacy session identity is incomplete"
                ) from error
            busy |= session.status not in {"stopped", "interrupted", "failed"}
        for task in await self.repository.writer_tasks(user_id, account_id):
            if task.task_type == "import_tool_account_config" and not writes_skills(
                task.payload.get("files")
            ):
                continue
            kind, resource = {
                "create_tool_session": ("session", task.payload.get("session_id")),
                "create_binding_session": ("binding", task.payload.get("binding_id")),
                "import_tool_account_config": ("import", task.task_id),
                "migrate_tool_account_runtime": ("backend", task.task_id),
            }[task.task_type]
            try:
                writers.append(
                    SkillTakeoverWriter.model_validate(
                        {
                            "kind": kind,
                            "node_id": task.node_id,
                            "resource_id": resource,
                            "task_id": task.id,
                            "runtime_backend": task.payload.get("runtime_backend"),
                        }
                    )
                )
            except ValidationError as error:
                raise SkillContentError(
                    "STATE_WRITERS_UNKNOWN", "legacy resource identity is incomplete"
                ) from error
            busy |= task.status not in _TERMINAL_TASKS
        writers.sort(key=lambda writer: writer.model_dump_json())
        if len(writers) > 10000:
            raise SkillContentError(
                "STATE_WRITERS_UNKNOWN", "legacy writer inventory exceeds limit"
            )
        return writers, busy

    async def authorize(
        self, node_id: UUID, takeover_id: UUID, task_id: UUID
    ) -> SkillAccountTakeover:
        """
        先取用户锁再核实精确任务租约、目录纪元和当前账户绑定。

        :param node_id (UUID): 认证节点
        :param takeover_id (UUID): 接管身份
        :param task_id (UUID): 精确数据库任务身份
        :return SkillAccountTakeover: 本次有效授权的接管收据
        """
        receipt = await self.repository.on_node(node_id, takeover_id)
        if receipt is None:
            raise SkillContentError("TAKEOVER_NOT_FOUND", "account takeover not found")
        await self.storage.lock_usage(receipt.user_id)
        receipt = await self.repository.on_node(node_id, takeover_id)
        if receipt is None or receipt.task_id != task_id:
            raise SkillContentError("TAKEOVER_NOT_FOUND", "account takeover not found")
        task = await self.runtime.task(node_id, task_id)
        if (
            task is None
            or task.task_type != "takeover_tool_account_skills"
            or content_hash(task.payload) != content_hash(takeover_payload(receipt))
        ):
            raise SkillContentError("TAKEOVER_NOT_FOUND", "account takeover task binding changed")
        if receipt.status == "committed":
            return receipt
        if (
            task.status not in {"leased", "running"}
            or task.lease_until is None
            or task.lease_until.replace(tzinfo=task.lease_until.tzinfo or UTC) <= datetime.now(UTC)
        ):
            raise SkillContentError("TAKEOVER_NOT_FOUND", "account takeover task lease is inactive")
        account = await self.repository.account(receipt.user_id, receipt.account_id)
        directory = await self.runtime.directory(receipt.user_id, receipt.account_id)
        if (
            account is None
            or account.affinity_node_id != node_id
            or account.runtime_backend != receipt.runtime_backend
        ):
            raise SkillContentError("TAKEOVER_BINDING_CHANGED", "account runtime binding changed")
        if (
            directory is None
            or directory.mode != "migrating"
            or directory.epoch != receipt.directory_epoch
            or directory.head_checkpoint_id is not None
        ):
            raise SkillContentError("HEAD_CHANGED", "account directory changed during takeover")
        return receipt

    async def drained(self, receipt: SkillAccountTakeover) -> None:
        """
        不能遗漏预约后出现的写入者，也不能用服务器终态代替 Node 证明。

        :param receipt (SkillAccountTakeover): 已授权接管收据
        """
        current, busy = await self.inventory(receipt.user_id, receipt.account_id)
        original = {content_hash(item) for item in receipt.inventory_json}
        if any(content_hash(item.model_dump(mode="json")) not in original for item in current):
            raise SkillContentError("STATE_WRITERS_CHANGED", "legacy writer inventory changed")
        if busy:
            raise SkillContentError(
                "STATE_WRITERS_ACTIVE", "legacy writer tasks or sessions remain active"
            )


def writes_skills(files: object) -> bool:
    """
    已证明无技能路径的普通配置无需排空，畸形旧任务不能被忽略。

    :param files (object): 旧任务文件声明
    :return bool: 是否可能写入账户技能发现根
    """
    if not isinstance(files, list) or not files:
        return True
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return True
        path = item["path"].strip().replace("$HOME/", "~/").rstrip("/")
        if path == "~/.claude/skills" or path.startswith("~/.claude/skills/"):
            return True
    return False


def content_hash(value: object) -> str:
    """
    对稳定 JSON 身份计算不含字节正文的输入摘要。

    :param value (object): 可 JSON 编码的规范值
    :return str: SHA-256 摘要
    """
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def takeover_payload(receipt: SkillAccountTakeover) -> dict[str, object]:
    """
    任务只传固定身份和版本指针，不传原目录文件或宿主路径。

    :param receipt (SkillAccountTakeover): 已固定的接管记录
    :return dict[str, object]: 精确任务正文
    """
    return {
        "takeover_id": str(receipt.id),
        "user_id": str(receipt.user_id),
        "tool_account_id": str(receipt.account_id),
        "runtime_backend": receipt.runtime_backend,
        "directory_epoch": receipt.directory_epoch,
        "inventory_digest": receipt.inventory_digest,
        "protocol_version": 1,
        "manifest_version": 1,
    }
