"""
预约首次目录接管、绑定稳定捕获上传并原子切换账户权威。
"""

from datetime import UTC, datetime, timedelta
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.config import Settings
from agent_remote_server.models import NodeTask
from agent_remote_server.models.skill_state import AccountSkillDirectoryState
from agent_remote_server.models.skill_takeover import SkillAccountTakeover
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry
from agent_remote_server.schemas.skill_takeover import (
    SkillTakeoverCapture,
    SkillTakeoverLease,
    SkillTakeoverLeaseRequest,
    SkillTakeoverRequest,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.deployment_discovery import resolve_takeover_discoveries
from agent_remote_server.services.skills.finalization_checkpoints import (
    candidate_skill_names,
    validate_finalization_limits,
)
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.services.skills.takeover_context import (
    TakeoverContext,
    content_hash,
    takeover_payload,
)
from agent_remote_server.services.skills.takeover_publication import publish_takeover
from agent_remote_server.skill_manager.manifest import manifest_digest


class SkillAccountTakeoverService:
    """
    共享首次接管事务，普通会话预约与精确节点传输分别授权。
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        """
        共享调用方事务及部署额度，提交归调用方负责。

        :param session (AsyncSession): 当前请求事务
        :param settings (Settings): 部署配置
        """
        self.context = TakeoverContext(session, settings)

    async def reserve(
        self, user_id: UUID, account_id: UUID, request: SkillTakeoverRequest
    ) -> SkillAccountTakeover:
        """
        预约不强停已有写入者，模式、固定资源清单和任务共同提交。

        :param user_id (UUID): 认证用户
        :param account_id (UUID): 目标账户
        :param request (SkillTakeoverRequest): 原始幂等键与比较纪元
        :return SkillAccountTakeover: 同一持久化接管收据
        """
        context = self.context
        if await context.repository.account(user_id, account_id) is None:
            raise SkillContentError("ACCOUNT_NOT_FOUND", "account not found")
        await context.storage.lock_usage(user_id)
        digest = content_hash({"account_id": str(account_id), **request.model_dump(mode="json")})
        previous = await context.repository.by_key(user_id, request.idempotency_key)
        if previous is not None:
            if previous.request_digest != digest:
                raise SkillContentError("IDEMPOTENCY_CONFLICT", "takeover key has different input")
            return previous
        if not context.settings.skill_manager_enabled:
            raise SkillContentError("SKILL_MANAGER_DISABLED", "skill takeover is disabled")
        async with context.session.begin_nested():
            account = await context.repository.account(user_id, account_id)
            if account is None or account.tool_type != "claude":
                raise SkillContentError("ACCOUNT_NOT_FOUND", "account not found")
            await context.supported(account)
            assert account.affinity_node_id is not None and account.runtime_backend is not None
            directory = await context.runtime.directory(user_id, account_id)
            epoch = directory.epoch if directory is not None else 0
            if (
                epoch != request.expected_directory_epoch
                or directory is not None
                and (directory.mode != "legacy" or directory.head_checkpoint_id is not None)
            ):
                raise SkillContentError(
                    "HEAD_CHANGED", "account is no longer the expected legacy directory"
                )
            if await context.repository.for_account(user_id, account_id) is not None:
                raise SkillContentError(
                    "MIGRATION_PENDING", "account already has a takeover receipt"
                )
            inventory, _ = await context.inventory(user_id, account_id)
            if any(writer.node_id != account.affinity_node_id for writer in inventory):
                raise SkillContentError(
                    "STATE_WRITERS_UNKNOWN",
                    "legacy resources on another node require reconciliation",
                )
            if directory is None:
                directory = AccountSkillDirectoryState(
                    account_id=account_id,
                    user_id=user_id,
                    tool_type="claude",
                    epoch=epoch + 1,
                    mode="migrating",
                )
                context.runtime.add(directory)
            else:
                directory.mode, directory.epoch = "migrating", epoch + 1
            await context.runtime.flush()
            records = [writer.model_dump(mode="json") for writer in inventory]
            receipt = SkillAccountTakeover(
                id=uuid4(),
                user_id=user_id,
                account_id=account_id,
                node_id=account.affinity_node_id,
                task_id=uuid4(),
                runtime_backend=account.runtime_backend,
                directory_epoch=epoch + 1,
                idempotency_key=request.idempotency_key,
                request_digest=digest,
                inventory_json=records,
                inventory_digest=content_hash(records),
                status="reserved",
                upload_attempt=0,
            )
            context.runtime.add(
                NodeTask(
                    id=receipt.task_id,
                    node_id=receipt.node_id,
                    task_id=f"takeover_tool_account_skills:{receipt.id}",
                    task_type="takeover_tool_account_skills",
                    status="pending",
                    payload=takeover_payload(receipt),
                    retry_count=0,
                )
            )
            await context.runtime.flush()
            context.runtime.add(receipt)
            await context.runtime.flush()
        return receipt

    async def get(self, node_id: UUID, takeover_id: UUID, task_id: UUID) -> SkillAccountTakeover:
        """
        排空前也可取得固定清单，查询不会创建捕获或续期上传。

        :param node_id (UUID): 认证节点
        :param takeover_id (UUID): 接管身份
        :param task_id (UUID): 精确任务身份
        :return SkillAccountTakeover: 已授权的原始收据
        """
        return await self.context.authorize(node_id, takeover_id, task_id)

    async def renew_lease(
        self, node_id: UUID, takeover_id: UUID, task_id: UUID, request: SkillTakeoverLeaseRequest
    ) -> SkillTakeoverLease:
        """
        只延长本次领取的活动租约，旧轮次、已过期或已提交状态不能复活。

        :param node_id (UUID): 认证节点
        :param takeover_id (UUID): 原始接管身份
        :param task_id (UUID): 精确任务身份
        :param request (SkillTakeoverLeaseRequest): 本次领取序号
        :return SkillTakeoverLease: 服务器选择的短期截止时间
        """
        context = self.context
        receipt = await context.authorize(node_id, takeover_id, task_id)
        if receipt.status == "committed":
            raise SkillContentError("TAKEOVER_COMMITTED", "takeover is already committed")
        task = await context.runtime.task(node_id, task_id)
        assert task is not None
        if task.retry_count != request.lease_attempt:
            raise SkillContentError(
                "TAKEOVER_LEASE_CHANGED", "task belongs to another poll attempt"
            )
        now = datetime.now(UTC)
        if (
            task.lease_until is None
            or task.lease_until.replace(tzinfo=task.lease_until.tzinfo or UTC) <= now
        ):
            raise SkillContentError("TAKEOVER_NOT_FOUND", "account takeover task lease is inactive")
        duration = min(context.settings.node_task_lease_seconds, 300)
        if duration <= 0:
            raise SkillContentError("TAKEOVER_LEASE_UNAVAILABLE", "task lease duration is invalid")
        task.lease_until = now + timedelta(seconds=duration)
        await context.runtime.flush()
        return SkillTakeoverLease(
            takeover_id=receipt.id,
            task_id=task.id,
            node_id=node_id,
            lease_attempt=task.retry_count,
            server_time=now,
            lease_until=task.lease_until,
            renew_after_milliseconds=max(1, duration * 1000 // 3),
        )

    async def begin_capture(
        self, node_id: UUID, takeover_id: UUID, task_id: UUID, request: SkillTakeoverCapture
    ) -> SkillAccountTakeover:
        """
        只有精确租约和静止声明可以固定输入，过期上传只续传相同树。

        :param node_id (UUID): 认证节点
        :param takeover_id (UUID): 接管身份
        :param task_id (UUID): 精确任务身份
        :param request (SkillTakeoverCapture): Helper 固定的完整输入
        :return SkillAccountTakeover: 当前捕获及上传绑定
        """
        context = self.context
        receipt = await context.authorize(node_id, takeover_id, task_id)
        digest = manifest_digest(request.manifest)
        if (
            request.writers_quiescent is not True
            or request.helper_receipt_id.int == 0
            or request.directory_epoch != receipt.directory_epoch
            or request.inventory_digest != receipt.inventory_digest
        ):
            raise SkillContentError(
                "TAKEOVER_CAPTURE_INVALID", "capture does not prove the reserved writer fence"
            )
        if receipt.capture_digest is not None and (
            receipt.capture_digest != digest
            or receipt.helper_receipt_id != request.helper_receipt_id
        ):
            raise SkillContentError("IDEMPOTENCY_CONFLICT", "takeover capture input is immutable")
        if receipt.status == "committed":
            return receipt
        await context.drained(receipt)
        async with context.session.begin_nested():
            if receipt.upload_id is not None:
                upload = await context.content.get(receipt.user_id, receipt.upload_id)
                if upload.status != "expired":
                    return receipt
            validate_finalization_limits(
                request.manifest,
                candidate_skill_names(request.manifest),
                context.settings.skill_storage_policy,
            )
            attempt = receipt.upload_attempt + 1
            if attempt >= 2**63:
                raise SkillContentError(
                    "GENERATION_EXHAUSTED", "takeover upload attempts are exhausted"
                )
            upload = await context.content.begin(
                receipt.user_id,
                f"takeover:{receipt.id}:{attempt}",
                request.manifest,
                "account_directory",
            )
            receipt.capture_digest, receipt.helper_receipt_id = digest, request.helper_receipt_id
            receipt.upload_id, receipt.upload_attempt, receipt.status = (
                upload.id,
                attempt,
                "uploading",
            )
            await context.runtime.flush()
        return receipt

    async def prepare_file(
        self, node_id: UUID, takeover_id: UUID, task_id: UUID, upload_id: UUID, digest: str
    ) -> SkillTreeEntry:
        """
        在接收流之前核对本次有效上传与单文件上限。

        :param node_id (UUID): 认证节点
        :param takeover_id (UUID): 接管身份
        :param task_id (UUID): 精确任务身份
        :param upload_id (UUID): 当前上传尝试
        :param digest (str): 清单文件摘要
        :return SkillTreeEntry: 可接收的文件声明
        """
        receipt = await self.context.authorize(node_id, takeover_id, task_id)
        _require_upload(receipt, upload_id)
        return await self.context.content.prepare_file(receipt.user_id, upload_id, digest)

    async def put_file(
        self,
        node_id: UUID,
        takeover_id: UUID,
        task_id: UUID,
        upload_id: UUID,
        digest: str,
        source: BinaryIO,
    ) -> bool:
        """
        每个文件重新核对任务，未绑定的相同摘要不能扩大上传权限。

        :param node_id (UUID): 认证节点
        :param takeover_id (UUID): 接管身份
        :param task_id (UUID): 精确任务身份
        :param upload_id (UUID): 当前上传尝试
        :param digest (str): 文件摘要
        :param source (BinaryIO): 有界私有暂存流
        :return bool: 是否新保存内容对象
        """
        receipt = await self.context.authorize(node_id, takeover_id, task_id)
        _require_upload(receipt, upload_id)
        return await self.context.content.put_file(receipt.user_id, upload_id, digest, source)

    async def complete(
        self, node_id: UUID, takeover_id: UUID, task_id: UUID, upload_id: UUID
    ) -> SkillAccountTakeover:
        """
        全部内容与来源身份、分支和目录权威在一个保存点内提交。

        :param node_id (UUID): 认证节点
        :param takeover_id (UUID): 接管身份
        :param task_id (UUID): 精确任务身份
        :param upload_id (UUID): 当前完整上传尝试
        :return SkillAccountTakeover: 原始权威提交收据
        """
        context = self.context
        receipt = await context.authorize(node_id, takeover_id, task_id)
        if receipt.upload_id != upload_id:
            raise SkillContentError("UPLOAD_SUPERSEDED", "takeover upload attempt is not current")
        if receipt.status == "committed":
            return receipt
        _require_upload(receipt, upload_id)
        await context.drained(receipt)
        async with retention_mutation(context.session, receipt.user_id):
            account = await context.repository.account(receipt.user_id, receipt.account_id)
            assert account is not None
            await context.supported(account)
            tree = await context.content.complete(receipt.user_id, upload_id)
            manifest = await context.content.read_tree(
                receipt.user_id, "account_directory", tree.digest
            )
            directory = await publish_takeover(context, receipt, manifest)
            receipt.checkpoint_id, receipt.status = directory.id, "committed"
            await context.runtime.flush()
            await resolve_takeover_discoveries(context.session, receipt)
        return receipt


def _require_upload(receipt: SkillAccountTakeover, upload_id: UUID) -> None:
    """
    已提交输入不再接收写入，旧尝试不能借相同内容摘要复活。

    :param receipt (SkillAccountTakeover): 已授权收据
    :param upload_id (UUID): 请求选择的上传尝试
    """
    if receipt.status != "uploading" or receipt.upload_id != upload_id:
        raise SkillContentError("UPLOAD_SUPERSEDED", "takeover upload attempt is not writable")
