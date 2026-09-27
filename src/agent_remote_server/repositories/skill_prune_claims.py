"""
在历史退役事务中登记派生内容续扫归属，依赖真实外键销毁旧生命周期资格。
"""

from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_prune_claims import SkillPruneContentClaim
from agent_remote_server.skill_manager.retention.graph import RetentionKey


class SkillPruneClaimRepository:
    """
    完整分块写入仍共享调用方一个保存点，已有原生命周期身份不会被刷新或替换。
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        沿用已取得用户锁的业务事务。

        :param session (AsyncSession): 调用方负责提交的事务
        """
        self._session = session

    async def register(
        self, user_id: UUID, account_id: UUID, source_key: str, keys: set[RetentionKey]
    ) -> None:
        """
        只登记实际存在的状态树或对象，新树同摘要重建没有本行就不能继承旧授权。

        :param user_id (UUID): 已认证所有者
        :param account_id (UUID): 明确退役账户
        :param source_key (str): 原规范来源或完整目录标识
        :param keys (set[RetentionKey]): 本次确实解除历史引用的树及其原状态对象
        """
        if len(keys) > 1_000_000 or any(
            key.kind not in {"state_tree", "state_object"} for key in keys
        ):
            raise ValueError("invalid prune content claim scope")
        dialect = self._session.get_bind().dialect.name
        if dialect not in {"postgresql", "sqlite"}:
            raise ValueError("unsupported skill storage database")
        ordered = sorted(keys)
        for start in range(0, len(ordered), 100):
            rows = [
                dict(
                    user_id=user_id,
                    account_id=account_id,
                    source_key=source_key,
                    kind="tree" if key.kind == "state_tree" else "object",
                    digest=key.identity,
                    category="state",
                    tree_digest=key.identity if key.kind == "state_tree" else None,
                    object_digest=key.identity if key.kind == "state_object" else None,
                )
                for key in ordered[start : start + 100]
            ]
            statement = (
                postgres_insert(SkillPruneContentClaim)
                if dialect == "postgresql"
                else sqlite_insert(SkillPruneContentClaim)
            )
            await self._session.execute(
                statement.values(rows).on_conflict_do_nothing(
                    index_elements=["user_id", "account_id", "source_key", "kind", "digest"]
                )
            )
        await self._session.flush()
