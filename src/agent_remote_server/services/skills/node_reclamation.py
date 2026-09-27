"""
在原始用户事务锁内核验收尾引用及每个对象，避免恢复不一致时删除节点唯一副本。
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from agent_remote_server.repositories.skill_publication import SkillPublicationRepository
from agent_remote_server.schemas.skill_reclamation import SkillReclamationAuthorization
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.finalization_context import FinalizationContext
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


async def authorize_reclamation(
    context: FinalizationContext,
    publications: SkillPublicationRepository,
    store: PrivateObjectStore,
    node_id: UUID,
    finalization_id: UUID,
    request_id: UUID,
) -> SkillReclamationAuthorization:
    """
    只输出绑定原输入的短期核验，不新增内容租约或执行任何本地删除。

    :param context (FinalizationContext): 原始收尾授权及用户锁
    :param publications (SkillPublicationRepository): 同事务发布读取
    :param store (PrivateObjectStore): 私有对象卷
    :param node_id (UUID): 认证节点身份
    :param finalization_id (UUID): 原始收尾身份
    :param request_id (UUID): 当前读取的随机挑战
    :return SkillReclamationAuthorization: 完整检查后的短期核验结果
    """
    receipt, snapshot = await context.receipt(node_id, finalization_id)
    terminal = {"published", "conflicted", "detached"}
    if (
        receipt.status not in terminal
        or receipt.content_retired_at is not None
        or receipt.checkpoint_id is None
        or receipt.tree_digest != receipt.incoming_digest
        or snapshot.status != "retained"
    ):
        raise SkillContentError(
            "STATE_RECLAMATION_UNAVAILABLE", "reclamation requires retained terminal input"
        )
    checkpoint = await context.runtime.checkpoint(
        receipt.user_id, receipt.account_id, receipt.checkpoint_id
    )
    publication = await publications.latest(receipt)
    if (
        checkpoint is None
        or not checkpoint.retained
        or checkpoint.scope != "directory"
        or checkpoint.subtree_prefix != ""
        or checkpoint.tree_digest != receipt.incoming_digest
        or publication is None
        or publication.status not in terminal
        or publication.status != receipt.status
        or publication.content_retired_at is not None
    ):
        raise SkillContentError(
            "STATE_RECLAMATION_UNAVAILABLE",
            "original retained checkpoint or publication is unavailable",
        )
    try:
        manifest = await context.content.read_tree(
            receipt.user_id, "state", receipt.incoming_digest
        )
        if manifest_digest(manifest) != receipt.incoming_digest:
            raise ValueError("stored manifest identity changed")
        files = {entry.sha256: entry for entry in manifest.entries if entry.kind == "file"}
        objects = await context.storage.objects(receipt.user_id, "state", set(files))
        if set(objects) != set(files) or any(
            row.status != "available" or row.size != files[digest].size
            for digest, row in objects.items()
        ):
            raise ValueError("stored object metadata is unavailable")
        await store.verify_manifest(receipt.user_id, manifest)
    except (OSError, ValueError) as error:
        raise SkillContentError(
            "STATE_RECLAMATION_UNAVAILABLE", "complete remote input could not be verified"
        ) from error
    verified = datetime.now(UTC)
    return SkillReclamationAuthorization.model_validate(
        {
            "version": 1,
            "request_id": request_id,
            "node_id": snapshot.node_id,
            "user_id": receipt.user_id,
            "account_id": receipt.account_id,
            "session_id": snapshot.session_reference_id,
            "snapshot_id": snapshot.id,
            "finalization_id": receipt.id,
            "checkpoint_id": checkpoint.id,
            "tree_digest": receipt.incoming_digest,
            "unclean": receipt.unclean,
            "publication_id": publication.id,
            "publication_attempt": publication.attempt,
            "publication_status": publication.status,
            "verified_at": verified,
            "expires_at": verified + timedelta(seconds=60),
        }
    )
