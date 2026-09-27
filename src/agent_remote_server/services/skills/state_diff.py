"""
对保留检查点与明确原始版本或目录父节点做有界元数据比较。
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from agent_remote_server.models.skill_state import SkillCheckpoint
from agent_remote_server.repositories.skill_local import SkillLocalRepository
from agent_remote_server.schemas.skill_manifest import (
    SkillTreeEntry,
    SkillTreeManifest,
    validate_relative_path,
)
from agent_remote_server.schemas.skill_state_queries import SkillCheckpointDiff, SkillStatePathDiff
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.state_queries import SkillStateQueryService

type StateBaseline = tuple[
    Literal["package_revision", "local_initial_revision", "directory_checkpoint"],
    UUID,
    str,
    SkillTreeManifest,
]


@dataclass
class StateDiffService:
    """
    差异不读取正文或保存内容，也不隐式恢复过期的原始包。
    """

    queries: SkillStateQueryService
    local: SkillLocalRepository

    async def diff(
        self, user_id: UUID, checkpoint_id: UUID, limit: int = 100, cursor: str | None = None
    ) -> SkillCheckpointDiff:
        """
        目录与父目录比较，单项与该精确分支的原始包或本地初始快照比较。

        :param user_id (UUID): 当前用户
        :param checkpoint_id (UUID): 指定检查点
        :param limit (int): 有界路径页大小
        :param cursor (str | None): 上页末尾路径
        :return SkillCheckpointDiff: 明确两侧身份和摘要的差异页
        """
        if not 1 <= limit <= 500:
            raise SkillContentError("INVALID_REQUEST", "diff page size is out of range")
        if cursor is not None:
            try:
                validate_relative_path(cursor)
            except ValueError as error:
                raise SkillContentError("INVALID_REQUEST", "invalid diff cursor") from error
        checkpoint = await self.queries.require(user_id, checkpoint_id)
        if not checkpoint.retained or checkpoint.tree_digest is None:
            raise SkillContentError("STATE_EXPIRED", "checkpoint content is no longer retained")
        current = await self.queries.content.read_tree(user_id, "state", checkpoint.tree_digest)
        kind, reference, digest, base = await self.baseline(checkpoint)
        prefix = checkpoint.subtree_prefix
        before, after = _selected(base, prefix), _selected(current, prefix)
        paths = sorted(before.keys() | after.keys(), key=str.encode)
        items = []
        for path in paths:
            if cursor is not None and path.encode() <= cursor.encode():
                continue
            if before.get(path) == after.get(path):
                continue
            items.append(
                SkillStatePathDiff(path=path, base=before.get(path), current=after.get(path))
            )
            if len(items) > limit:
                break
        return SkillCheckpointDiff(
            checkpoint_id=checkpoint.id,
            base_kind=kind,
            base_reference_id=reference,
            base_tree_digest=digest,
            current_tree_digest=checkpoint.tree_digest,
            items=items[:limit],
            next_cursor=items[limit - 1].path if len(items) > limit else None,
        )

    async def baseline(self, checkpoint: SkillCheckpoint) -> StateBaseline:
        """
        基线内容过期时明确失败，不以空树伪装完整差异。

        :param checkpoint (SkillCheckpoint): 已授权当前侧
        :return StateBaseline: 基线种类、身份、原始摘要和对齐目录树
        """
        queries = self.queries
        if checkpoint.scope == "directory":
            base = (
                checkpoint
                if checkpoint.parent_id is None
                else await queries.require(checkpoint.user_id, checkpoint.parent_id)
            )
            if base.scope != "directory" or base.account_id != checkpoint.account_id:
                raise SkillContentError(
                    "STATE_SCOPE_MISMATCH", "directory parent has another scope"
                )
            if not base.retained or base.tree_digest is None:
                raise SkillContentError(
                    "STATE_EXPIRED", "comparison directory is no longer retained"
                )
            tree = await queries.content.read_tree(checkpoint.user_id, "state", base.tree_digest)
            return "directory_checkpoint", base.id, base.tree_digest, tree
        branch = await queries.repository.branch(checkpoint)
        assert branch is not None
        if branch.installation_id is not None:
            assert branch.base_revision_id is not None
            revision = await queries.library.revision(
                checkpoint.user_id, branch.installation_id, str(branch.base_revision_id)
            )
            if revision is None or not revision.retained or revision.tree_digest is None:
                raise SkillContentError(
                    "REVISION_EXPIRED", "original package is no longer retained"
                )
            package = await queries.content.read_tree(
                checkpoint.user_id, "package", revision.tree_digest
            )
            tree = SkillTreeManifest(
                entries=(
                    SkillTreeEntry(path=checkpoint.subtree_prefix, kind="directory", mode=0o755),
                    *(
                        entry.model_copy(
                            update={"path": checkpoint.subtree_prefix + "/" + entry.path}
                        )
                        for entry in package.entries
                    ),
                )
            )
            return "package_revision", revision.id, revision.tree_digest, tree
        assert branch.local_skill_id is not None and branch.local_revision_id is not None
        local_revision = await self.local.revision(
            checkpoint.user_id,
            checkpoint.account_id,
            branch.local_skill_id,
            branch.local_revision_id,
        )
        if (
            local_revision is None
            or not local_revision.retained
            or local_revision.tree_digest is None
        ):
            raise SkillContentError("REVISION_EXPIRED", "local initial state is no longer retained")
        if local_revision.subtree_prefix != checkpoint.subtree_prefix:
            raise SkillContentError(
                "STATE_SCOPE_MISMATCH", "local checkpoint changed its source name"
            )
        tree = await queries.content.read_tree(
            checkpoint.user_id, "state", local_revision.tree_digest
        )
        return "local_initial_revision", local_revision.id, local_revision.tree_digest, tree


def _selected(tree: SkillTreeManifest, prefix: str) -> dict[str, SkillTreeEntry]:
    """
    在原始完整路径上比较选中项，保留根权限并排除其他独立成员。

    :param tree (SkillTreeManifest): 完整保存树
    :param prefix (str): 单项前缀，目录范围为空
    :return dict[str, SkillTreeEntry]: 对象范围内的路径映射
    """
    return {
        entry.path: entry
        for entry in tree.entries
        if not prefix or entry.path == prefix or entry.path.startswith(prefix + "/")
    }
