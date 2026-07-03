from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.db import Base
from agent_remote_server.repositories import Repository


class PersistenceService:
    """
    持久化服务入口
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def repository[ModelT: Base](self, model_type: type[ModelT]) -> Repository[ModelT]:
        """
        创建指定模型的仓储

        :param model_type (type): SQLAlchemy 模型类型

        :return Repository: 模型仓储
        """

        return Repository(self._session, model_type)

    @property
    def session(self) -> AsyncSession:
        """
        返回当前数据库会话

        :return AsyncSession: 数据库会话
        """

        return self._session
