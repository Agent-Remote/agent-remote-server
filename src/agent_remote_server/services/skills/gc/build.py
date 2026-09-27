"""
从当前真实树库存与对象生成完整回收计划，不改变 ORM 或授权范围。
"""

from datetime import datetime
from uuid import UUID

from agent_remote_server.models.skill_storage import SkillContentObject
from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.content_errors import SkillContentError
from agent_remote_server.services.skills.gc.planning import (
    ContentBlobRelease,
    ContentObjectRelease,
    ContentReclamationPlan,
    upload_leases,
    utc,
)
from agent_remote_server.services.skills.retention.trees import StoredTreeRetention
from agent_remote_server.skill_manager.retention.graph import RetentionKey


def requested_keys(keys: tuple[RetentionKey, ...], *, trees: bool) -> tuple[RetentionKey, ...]:
    """
    在任何查询前验证完整显式选择，不能接收历史 UUID 或跨种类替换。

    :param keys (tuple[RetentionKey, ...]): 本次调用方明确授权身份
    :param trees (bool): 是否只允许分类树
    :return tuple[RetentionKey, ...]: 排序去重的完整身份
    """
    allowed = {"package_tree", "state_tree"} if trees else {"package_object", "state_object"}
    if len(keys) > 1_000_000 or any(key.kind not in allowed for key in keys):
        raise SkillContentError("INVALID_RECLAMATION_SCOPE", "invalid content reclamation scope")
    return tuple(sorted(set(keys)))


def affected_objects(index: RetentionIndex, trees: tuple[RetentionKey, ...]) -> set[RetentionKey]:
    """
    精确树只选中其本类别对象，不因共享文件而扩张到另一类别。

    :param index (RetentionIndex): 完整引用库存
    :param trees (tuple[RetentionKey, ...]): 精确选定树
    :return set[RetentionKey]: 本次可考虑释放的分类对象
    """
    selected = set(trees)
    return {
        RetentionKey(
            "package_object" if row.category == "package" else "state_object", row.object_digest
        )
        for row in index.tree_objects
        if RetentionKey(
            "package_tree" if row.category == "package" else "state_tree", row.tree_digest
        )
        in selected
    }


def reclamation_plan(
    user_id: UUID,
    index: RetentionIndex,
    views: tuple[StoredTreeRetention, ...],
    objects: tuple[SkillContentObject, ...],
    tree_keys: tuple[RetentionKey, ...],
    object_keys: tuple[RetentionKey, ...],
    now: datetime,
    *,
    all_unreferenced: bool,
    projected_tree_objects: tuple[tuple[str, str], ...] = (),
) -> ContentReclamationPlan:
    """
    保留完整前置事实以重验，并计算不会越过引用、租约或分类边界的释放。

    :param user_id (UUID): 已锁定用户
    :param index (RetentionIndex): 同用户完整引用库存
    :param views (tuple[StoredTreeRetention, ...]): 全部树的等待与实际历史外键
    :param objects (tuple[SkillContentObject, ...]): 受影响摘要的两类别完整对象
    :param tree_keys (tuple[RetentionKey, ...]): 本次精确树选择
    :param object_keys (tuple[RetentionKey, ...]): 本次显式追加的分类对象选择
    :param now (datetime): 单次固定真实时间
    :param all_unreferenced (bool): 是否明确提前结束树等待
    :param projected_tree_objects (tuple[tuple[str, str], ...]): 只读整理新增的状态树/对象边
    :return ContentReclamationPlan: 完整不可变回收计划
    """
    by_tree = {row.key: row for row in views}
    if any(key not in by_tree for key in tree_keys):
        raise SkillContentError("CONTENT_NOT_FOUND", "selected tree is not retained by this user")
    selected_objects = affected_objects(index, tree_keys) | set(object_keys)
    actual_keys = {
        RetentionKey("package_object" if row.category == "package" else "state_object", row.digest)
        for row in objects
    }
    if not selected_objects <= actual_keys:
        raise SkillContentError("CONTENT_NOT_FOUND", "selected object is not retained by this user")
    blockers: list[tuple[RetentionKey, tuple[str, ...]]] = []
    selected_views = tuple(by_tree[key] for key in tree_keys)
    for view in selected_views:
        reasons: list[str] = []
        if view.reasons:
            reasons.append("protected")
        if view.references:
            reasons.append("retained_history")
        if not all_unreferenced and (view.expires_at is None or view.expires_at > now):
            reasons.append("waiting" if view.expires_at is not None else "unknown_clock")
        if reasons:
            blockers.append((view.key, tuple(reasons)))
    refs: dict[RetentionKey, set[RetentionKey]] = {}
    for ref in index.tree_objects:
        key = RetentionKey(
            "package_object" if ref.category == "package" else "state_object", ref.object_digest
        )
        refs.setdefault(key, set()).add(
            RetentionKey(
                "package_tree" if ref.category == "package" else "state_tree", ref.tree_digest
            )
        )
    for tree_digest, object_digest in projected_tree_objects:
        refs.setdefault(RetentionKey("state_object", object_digest), set()).add(
            RetentionKey("state_tree", tree_digest)
        )
    leases = upload_leases(index, {row.digest for row in objects}, now)
    blobs: dict[str, list[ContentObjectRelease]] = {}
    removed_trees = set(tree_keys)
    for obj in objects:
        key = RetentionKey(
            "package_object" if obj.category == "package" else "state_object", obj.digest
        )
        selected = key in selected_objects
        if obj.status != "available":
            blockers.append((key, ("content_unavailable",)))
        release = (
            selected
            and obj.status == "available"
            and not (refs.get(key, set()) - removed_trees)
            and not any(lease.category == obj.category for lease in leases.get(obj.digest, ()))
        )
        blobs.setdefault(obj.digest, []).append(
            ContentObjectRelease(
                key,
                obj.size,
                obj.content_kind,
                obj.status,
                utc(obj.created_at),
                tuple(sorted(refs.get(key, ()))),
                selected,
                release,
            )
        )
    result: list[ContentBlobRelease] = []
    for digest, rows in sorted(blobs.items()):
        if len({(row.size, row.content_kind) for row in rows}) != 1:
            raise SkillContentError("CONTENT_INVALID", "shared content metadata differs")
        result.append(
            ContentBlobRelease(
                digest, tuple(sorted(rows, key=lambda row: row.key)), leases.get(digest, ())
            )
        )
    return ContentReclamationPlan(
        user_id,
        tree_keys,
        object_keys,
        all_unreferenced,
        selected_views,
        tuple(sorted(blockers)),
        tuple(result),
    )
