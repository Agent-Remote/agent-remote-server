"""
验证 PostgreSQL 建表不会使后续 SQLite 模板遗漏延后创建的外键。
"""

from conftest import isolated_metadata
from sqlalchemy import create_engine, create_mock_engine, inspect

from agent_remote_server.db import Base


def test_postgresql_ddl_does_not_remove_sqlite_foreign_keys() -> None:
    """
    先运行 PostgreSQL DDL 再复制模型，SQLite 仍包含模型声明的全部外键。
    """
    metadata = isolated_metadata(Base.metadata)

    def execute(sql: object, *multiparams: object, **params: object) -> None:
        """
        接收 PostgreSQL DDL，仅运行生成器的元数据副作用。

        :param sql (object): DDL 语句
        :param multiparams (object): 位置参数
        :param params (object): 命名参数
        """

    metadata.create_all(create_mock_engine("postgresql://", execute))
    engine = create_engine("sqlite://")
    try:
        isolated_metadata(metadata).create_all(engine)
        for table in metadata.tables.values():
            expected = {constraint.name for constraint in table.foreign_key_constraints}
            actual = {
                constraint["name"] for constraint in inspect(engine).get_foreign_keys(table.name)
            }
            assert actual == expected, table.name
    finally:
        engine.dispose()
