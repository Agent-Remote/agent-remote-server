"""
以完整目录为边界组合运行子树，保留根级数据并重新校验跨项链接。
"""

from dataclasses import dataclass

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest


@dataclass(frozen=True)
class SkillSubtree:
    """
    一个已授权 checkpoint 的完整树与原始顶层前缀。
    """

    name: str
    prefix: str
    tree: SkillTreeManifest


class MaterializationError(ValueError):
    """
    提供调用层可转换的稳定组合失败类别。
    """

    def __init__(self, code: str, message: str) -> None:
        """
        保存不含内容正文的错误信息。

        :param code (str): 稳定错误码
        :param message (str): 可公开说明
        """
        super().__init__(message)
        self.code = code


def compose_directory(
    directory: SkillTreeManifest,
    member_names: set[str],
    items: list[SkillSubtree],
    item_byte_limit: int,
) -> SkillTreeManifest:
    """
    只移除已知条目命名空间，再加入实际选定分支；不修改持久化原目录。

    :param directory (SkillTreeManifest): 当前完整目录 head
    :param member_names (set[str]): head 的全部已知成员，包括此次停用的成员
    :param items (list[SkillSubtree]): 此次实际暴露的分支
    :param item_byte_limit (int): 每个分支展开字节上限
    :return SkillTreeManifest: 重新验证依赖后的完整会话树
    """
    entries = [
        entry for entry in directory.entries if entry.path.split("/", 1)[0] not in member_names
    ]
    if sum(entry.size for entry in entries) > item_byte_limit:
        raise MaterializationError("QUOTA_EXCEEDED", "root auxiliary state exceeds its byte quota")
    occupied = {entry.path.split("/", 1)[0] for entry in entries}
    for item in items:
        if not item.name or "/" in item.name or item.name in {"ego-browser", "agent-remote-device"}:
            raise MaterializationError("STATE_SCOPE_MISMATCH", "invalid managed entry name")
        if item.name in occupied:
            raise MaterializationError(
                "STATE_SCOPE_MISMATCH", "entry collides with retained directory data"
            )
        occupied.add(item.name)
        if item.prefix:
            # 重命名可能改变指向其他条目的相对链接，必须显式迁移而不是静默改写。
            if item.prefix != item.name:
                raise MaterializationError(
                    "STATE_SCOPE_MISMATCH", "checkpoint name requires explicit migration"
                )
            selected = [
                entry
                for entry in item.tree.entries
                if entry.path == item.prefix or entry.path.startswith(item.prefix + "/")
            ]
            if not selected or selected[0].path != item.prefix or selected[0].kind != "directory":
                raise MaterializationError("STATE_SCOPE_MISMATCH", "checkpoint subtree is missing")
        else:
            selected = [SkillTreeEntry(path=item.name, kind="directory", mode=0o755)]
            selected.extend(
                entry.model_copy(update={"path": item.name + "/" + entry.path})
                for entry in item.tree.entries
            )
        if sum(entry.size for entry in selected) > item_byte_limit:
            raise MaterializationError("QUOTA_EXCEEDED", "skill checkpoint exceeds its byte quota")
        entries.extend(selected)
    if any(
        entry.path.split("/", 1)[0] in {"ego-browser", "agent-remote-device"} for entry in entries
    ):
        raise MaterializationError("STATE_SCOPE_MISMATCH", "system skills cannot enter user state")
    try:
        return SkillTreeManifest(
            entries=tuple(sorted(entries, key=lambda entry: entry.path.encode("utf-8")))
        )
    except ValueError as error:
        raise MaterializationError(
            "STATE_DEPENDENCY_MISSING", "selected state has missing or incompatible dependencies"
        ) from error
