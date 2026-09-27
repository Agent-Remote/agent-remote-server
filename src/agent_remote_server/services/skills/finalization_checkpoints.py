"""
保留完整收尾树和已有条目身份，格式错误仅记录诊断。
"""

from uuid import UUID, uuid4

from agent_remote_server.models.skill_snapshots import SessionSkillSnapshot
from agent_remote_server.models.skill_state import SkillCheckpoint, SkillDirectoryMember
from agent_remote_server.repositories.skill_runtime import SkillRuntimeRepository
from agent_remote_server.schemas.skill_library import validate_skill_name
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.library_context import _metadata, _skill_document
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy


def validate_finalization_limits(
    manifest: SkillTreeManifest, item_names: set[str], policy: SkillStoragePolicy
) -> None:
    """
    同时限制完整目录、各技能以及全部匿名根级辅助数据。

    :param manifest (SkillTreeManifest): 完整冻结目录
    :param item_names (set[str]): 本次额度检查认定的独立条目名称
    :param policy (SkillStoragePolicy): 部署额度
    """
    if any(
        entry.path.split("/", 1)[0] in {"ego-browser", "agent-remote-device"}
        for entry in manifest.entries
    ):
        raise SkillContentError(
            "SYSTEM_SKILL_IMMUTABLE", "system skill paths cannot enter runtime state"
        )
    totals: dict[str, int] = {}
    for entry in manifest.entries:
        root = entry.path.split("/", 1)[0]
        scope = root if root in item_names else ""
        totals[scope] = totals.get(scope, 0) + entry.size
    try:
        policy.validate_manifest(manifest, "account_directory")
    except ValueError as error:
        raise SkillContentError("QUOTA_EXCEEDED", "complete directory exceeds its quota") from error
    if any(size > policy.checkpoint_bytes for size in totals.values()):
        raise SkillContentError("QUOTA_EXCEEDED", "skill or root auxiliary state exceeds its quota")


def candidate_skill_names(manifest: SkillTreeManifest) -> set[str]:
    """
    上传前仅定位候选入口，未校验格式不代表已获得独立技能身份。

    :param manifest (SkillTreeManifest): 完整输入树
    :return set[str]: 包含直接说明文件的顶层目录
    """
    directories = {entry.path for entry in manifest.entries if entry.kind == "directory"}
    names: set[str] = set()
    for entry in manifest.entries:
        if entry.path.count("/") != 1 or not entry.path.endswith("/SKILL.md"):
            continue
        name = entry.path.split("/", 1)[0]
        if name not in directories:
            continue
        try:
            validate_skill_name(name)
        except ValueError:
            continue
        names.add(name)
    return names


async def valid_candidate_names(
    store: PrivateObjectStore,
    user_id: UUID,
    manifest: SkillTreeManifest,
) -> set[str]:
    """
    从实际完整字节识别可建立新身份的目录，错误格式仍作为辅助数据保留。

    :param store (PrivateObjectStore): 私有内容卷
    :param user_id (UUID): 内容归属用户
    :param manifest (SkillTreeManifest): 已完整持久化的树
    :return set[str]: 名称、目录类型和实际说明均有效的候选
    """
    names: set[str] = set()
    for name in candidate_skill_names(manifest):
        try:
            entry = _skill_document(manifest, name + "/SKILL.md")
            _metadata(await store.read_prefix(user_id, entry, 65_544), name)
        except SkillContentError:
            continue
        names.add(name)
    return names


async def validate_persisted_scopes(
    repository: SkillRuntimeRepository,
    store: PrivateObjectStore,
    snapshot: SessionSkillSnapshot,
    manifest: SkillTreeManifest,
    policy: SkillStoragePolicy,
) -> None:
    """
    字节齐备后重新识别新目录，不能靠无效入口拆分根级数据额度。

    :param repository (SkillRuntimeRepository): 已锁定的运行态仓储
    :param store (PrivateObjectStore): 私有内容卷
    :param snapshot (SessionSkillSnapshot): 原始授权快照
    :param manifest (SkillTreeManifest): 已验证完整输入
    :param policy (SkillStoragePolicy): 部署配额
    """
    names = {item.entry_name for item in await repository.snapshot_items(snapshot)}
    names |= await valid_candidate_names(store, snapshot.user_id, manifest)
    validate_finalization_limits(manifest, names, policy)


async def retain_incoming_checkpoint(
    repository: SkillRuntimeRepository,
    store: PrivateObjectStore,
    snapshot: SessionSkillSnapshot,
    manifest: SkillTreeManifest,
) -> SkillCheckpoint:
    """
    保存目录和已知条目的完整树视图，不推进任何运行分支 head。

    :param repository (SkillRuntimeRepository): 外层锁内运行态仓储
    :param store (PrivateObjectStore): 已验证私有内容卷
    :param snapshot (SessionSkillSnapshot): 已授权原始快照
    :param manifest (SkillTreeManifest): 已完整持久化目录
    :return SkillCheckpoint: 完整输入目录检查点
    """
    digest = manifest_digest(manifest)
    directory = SkillCheckpoint(
        id=uuid4(),
        user_id=snapshot.user_id,
        account_id=snapshot.account_id,
        scope="directory",
        directory_epoch=snapshot.directory_epoch,
        content_digest=digest,
        tree_digest=digest,
        parent_id=snapshot.starting_checkpoint_id,
        source_session_reference_id=snapshot.session_reference_id,
        invalid_skill_format=False,
    )
    repository.add(directory)
    await repository.flush()
    roots = {entry.path for entry in manifest.entries if "/" not in entry.path}
    for item in await repository.snapshot_items(snapshot):
        invalid = False
        try:
            entry = _skill_document(manifest, item.entry_name + "/SKILL.md")
            _metadata(await store.read_prefix(snapshot.user_id, entry, 65_544), item.entry_name)
        except SkillContentError:
            invalid = True
        checkpoint = SkillCheckpoint(
            id=uuid4(),
            user_id=snapshot.user_id,
            account_id=snapshot.account_id,
            scope="item",
            state_id=item.state_id,
            state_epoch=item.state_epoch,
            backing_directory_id=directory.id,
            subtree_prefix=item.entry_name,
            content_digest=digest,
            tree_digest=digest,
            parent_id=item.checkpoint_id,
            source_session_reference_id=snapshot.session_reference_id,
            invalid_skill_format=invalid,
        )
        repository.add(checkpoint)
        await repository.flush()
        if item.entry_name not in roots:
            continue
        repository.add(
            SkillDirectoryMember(
                user_id=snapshot.user_id,
                account_id=snapshot.account_id,
                directory_checkpoint_id=directory.id,
                entry_name=item.entry_name,
                state_id=item.state_id,
                checkpoint_id=checkpoint.id,
            )
        )
        directory.invalid_skill_format = directory.invalid_skill_format or invalid
    await repository.flush()
    return directory
