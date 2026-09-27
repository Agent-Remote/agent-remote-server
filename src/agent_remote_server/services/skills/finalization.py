"""
保存节点完整收尾输入，严格区分上传租约、持久化与账户发布。
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot, SkillFinalization
from agent_remote_server.models.skill_transfers import SkillFinalizationTransfer
from agent_remote_server.repositories.skill_finalization import SkillFinalizationRepository
from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.schemas.skill_finalizations import (
    SkillFinalizationRequest,
    SkillFinalizationView,
)
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.schemas.skill_publications import SkillPublicationView
from agent_remote_server.schemas.skill_reclamation import SkillReclamationAuthorization
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.services.skills.finalization_checkpoints import (
    candidate_skill_names,
    retain_incoming_checkpoint,
    validate_finalization_limits,
    validate_persisted_scopes,
)
from agent_remote_server.services.skills.finalization_context import FinalizationContext
from agent_remote_server.services.skills.node_reclamation import authorize_reclamation
from agent_remote_server.services.skills.publication import SkillPublicationService
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


class SkillFinalizationService:
    """
    请求成功由调用方提交，任何失败必须回滚原子内容和引用变更。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        将授权、内容与运行态绑定到同一事务。

        :param session (AsyncSession): 请求事务
        :param store (PrivateObjectStore): 私有内容卷
        :param policy (SkillStoragePolicy): 配额策略
        """
        self._session = session
        self._context = FinalizationContext(
            SkillFinalizationRepository(session),
            SkillRuntimeRepository(session),
            SkillStorageRepository(session),
            SkillContentService(session, store, policy),
        )
        self._publication = SkillPublicationService(session, store, policy)
        self._store = store
        self._policy = policy

    async def begin(
        self, node_id: UUID, snapshot_id: UUID, request: SkillFinalizationRequest
    ) -> SkillFinalizationView:
        """
        原子固定输入与上传额度；相同请求仅可替换已过期的传输尝试。

        :param node_id (UUID): 认证节点
        :param snapshot_id (UUID): Server 原始快照
        :param request (SkillFinalizationRequest): 节点不可变完整输入
        :return SkillFinalizationView: 当前收尾与有效尝试
        """
        context = self._context
        snapshot = await context.snapshot(node_id, snapshot_id)
        async with retention_mutation(self._session, snapshot.user_id):
            if snapshot.session_reference_id != request.session_id:
                raise SkillContentError(
                    "SNAPSHOT_BINDING_MISMATCH", "finalization session differs from snapshot"
                )
            digest = manifest_digest(request.manifest)
            termination = await context.repository.termination(snapshot.id)
            if termination is not None and (
                termination.incoming_digest is not None
                and termination.incoming_digest != digest
                or termination.unclean != request.unclean
            ):
                raise SkillContentError(
                    "IDEMPOTENCY_CONFLICT", "finalization differs from retained termination"
                )
            request_digest = _request_digest(snapshot_id, request, digest)
            receipt = await context.runtime.finalization(snapshot)
            by_key = await context.repository.by_key(snapshot.user_id, request.idempotency_key)
            if receipt is not None:
                if (
                    receipt.idempotency_key != request.idempotency_key
                    or receipt.request_digest != request_digest
                ):
                    raise SkillContentError(
                        "IDEMPOTENCY_CONFLICT", "snapshot already has another finalization input"
                    )
                transfer, upload = await context.transfer(receipt)
                if receipt.status == "upload_pending" and upload.status == "expired":
                    await self._validate(snapshot, request.manifest)
                    await self._replace_transfer(receipt, request.manifest, transfer)
                return await context.view(receipt)
            if by_key is not None:
                raise SkillContentError(
                    "IDEMPOTENCY_CONFLICT", "key already belongs to another finalization"
                )
            if snapshot.status == "retained":
                raise SkillContentError(
                    "SNAPSHOT_NOT_ACTIVE", "retained snapshot cannot accept a new input"
                )
            await self._validate(snapshot, request.manifest)
            receipt = SkillFinalization(
                id=uuid4(),
                user_id=snapshot.user_id,
                account_id=snapshot.account_id,
                node_id=node_id,
                snapshot_id=snapshot.id,
                idempotency_key=request.idempotency_key,
                request_digest=request_digest,
                incoming_digest=digest,
                unclean=request.unclean,
                status="upload_pending",
            )
            context.repository.add(receipt)
            await context.repository.flush()
            await self._replace_transfer(receipt, request.manifest, None)
            snapshot.status = "finalizing"
            await context.repository.flush()
            return await context.view(receipt)

    async def get(self, node_id: UUID, finalization_id: UUID) -> SkillFinalizationView:
        """
        查询不会自动续期，也不把完整保存误报为已发布。

        :param node_id (UUID): 认证节点
        :param finalization_id (UUID): 收尾身份
        :return SkillFinalizationView: 当前原始输入回执
        """
        receipt, _ = await self._context.receipt(node_id, finalization_id)
        return await self._context.view(receipt)

    async def publish(self, node_id: UUID, finalization_id: UUID) -> SkillPublicationView:
        """
        重新验证原节点和终态后发布其已保存输入，租约到期不改变该权限。

        :param node_id (UUID): 当前认证节点
        :param finalization_id (UUID): 原始收尾身份
        :return SkillPublicationView: 完整事务结果的最小回执
        """
        receipt, _ = await self._context.receipt(node_id, finalization_id)
        result = await self._publication.publish(receipt.user_id, receipt.id)
        return SkillPublicationView.model_validate(
            {
                "id": result.id,
                "finalization_id": result.finalization_id,
                "attempt": result.attempt,
                "status": result.status,
                "reason": result.reason,
                "result_checkpoint_id": result.result_checkpoint_id,
                "conflict_count": len(result.conflicts_json),
            }
        )

    async def authorize_reclamation(
        self, node_id: UUID, finalization_id: UUID, request_id: UUID
    ) -> SkillReclamationAuthorization:
        """
        重新验证完整远端内容，为原节点独立回收提供短期身份绑定。

        :param node_id (UUID): 当前认证节点
        :param finalization_id (UUID): 原始收尾身份
        :param request_id (UUID): 当前请求的随机挑战
        :return SkillReclamationAuthorization: 当前完整内容的短期核验
        """
        return await authorize_reclamation(
            self._context,
            SkillPublicationRepository(self._session),
            self._store,
            node_id,
            finalization_id,
            request_id,
        )

    async def prepare_file(
        self, node_id: UUID, finalization_id: UUID, upload_id: UUID, digest: str
    ) -> SkillTreeEntry:
        """
        在接收网络流之前确定当前尝试清单内的精确文件上限。

        :param node_id (UUID): 认证节点
        :param finalization_id (UUID): 收尾身份
        :param upload_id (UUID): 当前上传尝试
        :param digest (str): 原始清单内的文件摘要
        :return SkillTreeEntry: 可接收的文件声明
        """
        receipt, _ = await self._context.receipt(node_id, finalization_id)
        await self._context.transfer(receipt, upload_id, metadata_only=True)
        return await self._context.content.prepare_file(receipt.user_id, upload_id, digest)

    async def put_file(
        self, node_id: UUID, finalization_id: UUID, upload_id: UUID, digest: str, source: BinaryIO
    ) -> bool:
        """
        按当前不可变输入接收文件，不允许节点选择其他用户或路径。

        :param node_id (UUID): 认证节点
        :param finalization_id (UUID): 收尾身份
        :param upload_id (UUID): 当前上传尝试
        :param digest (str): 原始清单内的文件摘要
        :param source (BinaryIO): 已有界暂存的网络流
        :return bool: 是否新增磁盘内容
        """
        receipt, _ = await self._context.receipt(node_id, finalization_id)
        await self._context.transfer(receipt, upload_id, metadata_only=True)
        return await self._context.content.put_file(receipt.user_id, upload_id, digest, source)

    async def complete(
        self, node_id: UUID, finalization_id: UUID, upload_id: UUID
    ) -> SkillFinalizationView:
        """
        所有内容、目录与成员引用一次提交，账户和分支 head 始终不变。

        :param node_id (UUID): 认证节点
        :param finalization_id (UUID): 收尾身份
        :param upload_id (UUID): 当前上传尝试
        :return SkillFinalizationView: 完整持久化或幂等重试回执
        """
        context = self._context
        receipt, snapshot = await context.receipt(node_id, finalization_id)
        async with retention_mutation(self._session, receipt.user_id):
            _, upload = await context.transfer(receipt, upload_id)
            if receipt.status != "upload_pending":
                return await context.view(receipt)
            manifest = SkillTreeManifest.model_validate(upload.manifest_json)
            try:
                tree = await context.content.complete(receipt.user_id, upload_id)
                await validate_persisted_scopes(
                    context.runtime, self._store, snapshot, manifest, self._policy
                )
                checkpoint = await retain_incoming_checkpoint(
                    context.runtime, self._store, snapshot, manifest
                )
            except FileNotFoundError as error:
                raise SkillContentError(
                    "CONTENT_INCOMPLETE", "some finalization files are missing"
                ) from error
            except ValueError as error:
                if isinstance(error, SkillContentError):
                    raise
                raise SkillContentError(
                    "CONTENT_INVALID", "finalization content verification failed"
                ) from error
            receipt.persisted_at = datetime.now(UTC)
            receipt.tree_digest = tree.digest
            receipt.checkpoint_id = checkpoint.id
            receipt.status = "persisted_unclean" if receipt.unclean else "persisted"
            snapshot.status = "retained"
            await context.repository.flush()
            return await context.view(receipt)

    async def _validate(self, snapshot: SessionSkillSnapshot, manifest: SkillTreeManifest) -> None:
        """
        使用 Server 固定身份划分额度，不能把已知条目降格为匿名数据。

        :param snapshot (SessionSkillSnapshot): 原始授权快照
        :param manifest (SkillTreeManifest): 完整冻结目录
        """
        items = await self._context.runtime.snapshot_items(snapshot)
        validate_finalization_limits(
            manifest,
            {item.entry_name for item in items} | candidate_skill_names(manifest),
            self._policy,
        )

    async def _replace_transfer(
        self,
        receipt: SkillFinalization,
        manifest: SkillTreeManifest,
        previous: SkillFinalizationTransfer | None,
    ) -> None:
        """
        建立新上传额度后原子替换已过期尝试，不改变收尾身份。

        :param receipt (SkillFinalization): 不可变收尾输入
        :param manifest (SkillTreeManifest): 同一原始清单
        :param previous (SkillFinalizationTransfer | None): 可续期的旧绑定
        """
        attempt = 1 if previous is None else previous.attempt + 1
        if attempt > 2**63 - 1:
            raise SkillContentError(
                "GENERATION_EXHAUSTED", "finalization upload attempts exhausted"
            )
        upload = await self._context.content.begin(
            receipt.user_id, f"finalization:{receipt.id}:{attempt}", manifest, "account_directory"
        )
        if previous is None:
            self._context.repository.add(
                SkillFinalizationTransfer(
                    finalization_id=receipt.id,
                    user_id=receipt.user_id,
                    incoming_digest=receipt.incoming_digest,
                    upload_id=upload.id,
                    attempt=attempt,
                )
            )
        else:
            previous.upload_id = upload.id
            previous.attempt = attempt
        await self._context.repository.flush()


def _request_digest(snapshot_id: UUID, request: SkillFinalizationRequest, tree_digest: str) -> str:
    """
    用固定快照、来源会话和终止分类约束同一幂等请求。

    :param snapshot_id (UUID): 精确快照
    :param request (SkillFinalizationRequest): 完整原始请求
    :param tree_digest (str): 已计算的规范目录摘要
    :return str: 不含文件正文的请求摘要
    """
    encoded = json.dumps(
        {
            "snapshot_id": str(snapshot_id),
            "session_id": str(request.session_id),
            "tree_digest": tree_digest,
            "unclean": request.unclean,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
