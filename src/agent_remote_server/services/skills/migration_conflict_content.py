"""
导出迁移保存侧和有界原始三侧差异，不构造批准结果。
"""

import base64
import binascii
import json
from uuid import UUID

from agent_remote_server.models.skill_preparation import SkillBranchPreparation
from agent_remote_server.schemas.skill_manifest import validate_relative_path
from agent_remote_server.schemas.skill_migration_conflicts import (
    MigrationSide,
    SkillMigrationConflictDiff,
    SkillMigrationPathDiff,
    SkillMigrationTree,
)
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.services.skills.migration_conflicts import SkillMigrationConflictService


class SkillMigrationConflictContent:
    """
    每次通过真实迁移身份授权，摘要相等不扩大可读范围。
    """

    def __init__(self, conflicts: SkillMigrationConflictService) -> None:
        """
        共享已绑定事务的只读服务。

        :param conflicts (SkillMigrationConflictService): 迁移查询及授权服务
        """
        self.conflicts = conflicts
        self.content = conflicts.queries.content

    async def tree(
        self, user_id: UUID, migration_id: UUID, side: MigrationSide
    ) -> SkillMigrationTree:
        """
        保留原始完整树的全部路径和链接，额外根只表示上下文。

        :param user_id (UUID): 已认证用户
        :param migration_id (UUID): 原始迁移身份
        :param side (MigrationSide): 明确保存侧
        :return SkillMigrationTree: 精确清单及真实来源
        """
        info = await self.conflicts.info(user_id, migration_id)
        sides = {
            "base": info.base,
            "current": info.current,
            "incoming": info.incoming,
            "directory": info.directory,
        }
        if side not in sides:
            raise SkillContentError("INVALID_REQUEST", "invalid migration side")
        selected = sides[side]
        if selected.tree_digest is None:
            raise SkillContentError("STATE_EXPIRED", "saved directory content is not retained")
        tree = await self.content.read_tree(user_id, "state", selected.tree_digest)
        roots = {entry.path.split("/", 1)[0] for entry in tree.entries}
        return SkillMigrationTree(
            migration_id=migration_id,
            side=side,
            input=selected,
            target_root=info.name,
            extra_roots=tuple(sorted(roots - {info.name}, key=str.encode)),
            manifest=tree,
        )

    async def diff(
        self, user_id: UUID, migration_id: UUID, limit: int = 100, cursor: str | None = None
    ) -> SkillMigrationConflictDiff:
        """
        按原始路径分页元数据，游标绑定尝试和固定三侧摘要。

        :param user_id (UUID): 已认证用户
        :param migration_id (UUID): 明确迁移身份
        :param limit (int): 有界路径数量
        :param cursor (str | None): 上页绑定游标
        :return SkillMigrationConflictDiff: 保存输入的差异页
        """
        if not 1 <= limit <= 500:
            raise SkillContentError("INVALID_REQUEST", "diff page size is out of range")
        row = await self.conflicts.require(user_id, migration_id)
        if row.content_retired_at is not None:
            raise SkillContentError("STATE_EXPIRED", "migration comparison content has expired")
        after = decode_cursor(row, cursor) if cursor is not None else None
        sides = []
        for digest in (row.base_digest, row.current_digest, row.incoming_digest):
            tree = await self.content.read_tree(user_id, "state", digest)
            sides.append({entry.path: entry for entry in tree.entries})
        paths = sorted(set().union(*(side.keys() for side in sides)), key=str.encode)
        if after is not None and (
            after not in paths or sides[0].get(after) == sides[1].get(after) == sides[2].get(after)
        ):
            raise SkillContentError("INVALID_REQUEST", "cursor is not a differing path")
        items = []
        for path in paths:
            if after is not None and path.encode() <= after.encode():
                continue
            base, current, incoming = (side.get(path) for side in sides)
            if base == current == incoming:
                continue
            items.append(
                SkillMigrationPathDiff(path=path, base=base, current=current, incoming=incoming)
            )
            if len(items) > limit:
                break
        return SkillMigrationConflictDiff(
            migration_id=row.id,
            items=items[:limit],
            next_cursor=encode_cursor(row, items[limit - 1].path) if len(items) > limit else None,
        )


def encode_cursor(row: SkillBranchPreparation, path: str) -> str:
    """
    游标只包含身份、三侧摘要和路径，无文件正文或宿主路径。

    :param row (SkillBranchPreparation): 已授权原始记录
    :param path (str): 本页末尾不同路径
    :return str: 可验证范围的传输游标
    """
    value = [str(row.id), row.base_digest, row.current_digest, row.incoming_digest, path]
    return base64.urlsafe_b64encode(json.dumps(value, ensure_ascii=False).encode()).decode()


def decode_cursor(row: SkillBranchPreparation, cursor: str) -> str:
    """
    拒绝格式错误和跨尝试游标，不把任意未验证字符串当成路径。

    :param row (SkillBranchPreparation): 已授权原始记录
    :param cursor (str): 请求游标
    :return str: 同记录三侧下的原始相对路径
    """
    try:
        if len(cursor) > 16384:
            raise ValueError("oversized cursor")
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(value, list)
            or len(value) != 5
            or value[:4] != [str(row.id), row.base_digest, row.current_digest, row.incoming_digest]
            or not isinstance(value[4], str)
        ):
            raise ValueError("cursor scope mismatch")
        path: str = value[4]
        validate_relative_path(path)
        return path
    except (ValueError, UnicodeError, binascii.Error, RecursionError) as error:
        raise SkillContentError("INVALID_REQUEST", "invalid migration diff cursor") from error
