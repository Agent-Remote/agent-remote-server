"""
表达只在只读分析中存在的替代 head 和新增内容边，不改变任何原始历史身份。
"""

from dataclasses import dataclass
from uuid import UUID

from agent_remote_server.skill_manager.retention.graph import DirectoryReference


@dataclass(frozen=True)
class ProjectedCheckpoint:
    """
    尚未持久化的完整目录或单项视图，只承载保护图需要的内容依赖。
    """

    id: UUID
    original_id: UUID
    state_id: UUID | None
    tree_digest: str
    backing_directory_id: UUID | None


@dataclass(frozen=True)
class RetentionProjection:
    """
    用户内的增量图覆盖；新增身份不能替代快照、比较和基线中的原始引用。
    """

    branch_heads: tuple[tuple[UUID, UUID], ...]
    directory_heads: tuple[tuple[UUID, UUID], ...]
    checkpoints: tuple[ProjectedCheckpoint, ...]
    members: tuple[DirectoryReference, ...]
    tree_objects: tuple[tuple[str, str], ...]
