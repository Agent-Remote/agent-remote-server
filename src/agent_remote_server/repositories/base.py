from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.db import Base


class Repository[ModelT: Base]:
    """
    通用异步仓储
    """

    def __init__(self, session: AsyncSession, model_type: type[ModelT]) -> None:
        self._session = session
        self._model_type = model_type

    async def get(self, entity_id: UUID) -> ModelT | None:
        """
        按主键读取实体

        :param entity_id (UUID): 实体主键

        :return ModelT: 实体对象
        """

        return await self._session.get(self._model_type, entity_id)

    async def add(self, entity: ModelT) -> ModelT:
        """
        添加实体并刷新会话

        :param entity (ModelT): 待添加实体

        :return ModelT: 已加入会话的实体
        """

        self._session.add(entity)
        await self._session.flush()
        return entity

    async def list(self, *, limit: int = 100, offset: int = 0) -> Sequence[ModelT]:
        """
        分页列出实体

        :param limit (int): 最大返回数量
        :param offset (int): 起始偏移量

        :return Sequence: 实体列表
        """

        statement = select(self._model_type).limit(limit).offset(offset)
        result = await self._session.scalars(statement)
        return result.all()

    async def delete(self, entity: ModelT) -> None:
        """
        删除实体并刷新会话

        :param entity (ModelT): 待删除实体
        """

        await self._session.delete(entity)
        await self._session.flush()
