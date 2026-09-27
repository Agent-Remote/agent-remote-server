"""
验证索引文件下载保留完整树删除屏障与精确归属，并避免完整清单读取。
"""

import io
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from sqlalchemy import delete, event, update
from sqlalchemy.engine import Connection, ExecutionContext
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_node_skill_content import node_client as node_client
from test_skill_content_service import database as database
from test_skill_content_service import service, user
from test_skill_snapshots import prepared as prepared
from test_skill_storage import file_entry

from agent_remote_server.models.skill_storage import SkillContentObject, SkillTreeObjectReference
from agent_remote_server.schemas.skill_manifest import SkillTreeManifest
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.skill_manager.storage.policy import ContentScope


async def store_tree(
    database: async_sessionmaker[AsyncSession],
    root: Path,
    owner: UUID,
    files: dict[str, bytes],
    scope: ContentScope = "package",
) -> str:
    """
    所有树和对象引用均由正式完成事务创建，不手工伪造下载资格。

    :param database (async_sessionmaker[AsyncSession]): 隔离事务工厂
    :param root (Path): 私有字节卷
    :param owner (UUID): 当前所有者
    :param files (dict[str, bytes]): 实际普通文件内容
    :param scope (ContentScope): 当前配额类别
    :return str: 完整已保存树摘要
    """
    entries = tuple(file_entry(value, path=path) for path, value in sorted(files.items()))
    async with database.begin() as session:
        content = service(session, root)
        upload = await content.begin(owner, str(uuid4()), SkillTreeManifest(entries=entries), scope)
        for entry in entries:
            await content.put_file(owner, upload.id, entry.sha256, io.BytesIO(files[entry.path]))
        tree = await content.complete(owner, upload.id)
        return tree.digest


@pytest.mark.parametrize("opposite", [False, True])
async def test_other_unavailable_tree_member_blocks_healthy_file(
    database: async_sessionmaker[AsyncSession], tmp_path: Path, opposite: bool
) -> None:
    """
    不能把整树跨分类屏障退化成只检查请求摘要。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 私有内容卷
    :param opposite (bool): 是否仅另一类别持有删除标记
    """
    owner = await user(database)
    tree = await store_tree(database, tmp_path, owner, {"a": b"healthy", "b": b"retiring"})
    if opposite:
        await store_tree(database, tmp_path, owner, {"b": b"retiring"}, "state")
    async with database.begin() as session:
        await session.execute(
            update(SkillContentObject)
            .where(
                SkillContentObject.user_id == owner,
                SkillContentObject.category == ("state" if opposite else "package"),
                SkillContentObject.digest == file_entry(b"retiring").sha256,
            )
            .values(status="deleting")
        )
    output = io.BytesIO()
    async with database.begin() as session:
        with pytest.raises(SkillContentError) as failure:
            await service(session, tmp_path).read_file(
                owner, "package", tree, file_entry(b"healthy").sha256, output
            )
        assert failure.value.code == "CONTENT_UNAVAILABLE"
    assert output.getvalue() == b""


async def test_unrelated_markers_do_not_grant_or_deny_membership(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """
    删除标记及文件引用必须匹配正确用户、类别和树，别名只共享同一已验证对象。

    :param database (async_sessionmaker[AsyncSession]): 隔离事务工厂
    :param tmp_path (Path): 当前私有卷
    """
    owner, stranger = await user(database), await user(database)
    tree = await store_tree(database, tmp_path, owner, {"a": b"same", "alias": b"same"})
    other = await store_tree(database, tmp_path, owner, {"other": b"other"}, "state")
    foreign = await store_tree(database, tmp_path, stranger, {"a": b"same"})
    async with database.begin() as session:
        await session.execute(
            update(SkillContentObject)
            .where(
                (SkillContentObject.user_id == stranger)
                | (SkillContentObject.digest == file_entry(b"other").sha256)
            )
            .values(status="deleting")
        )
    async with database.begin() as session:
        content = service(session, tmp_path)
        output = io.BytesIO()
        await content.read_file(owner, "package", tree, file_entry(b"same").sha256, output)
        assert output.getvalue() == b"same"
        denied: tuple[tuple[ContentScope, str, str], ...] = (
            ("state", tree, file_entry(b"same").sha256),
            ("package", other, file_entry(b"other").sha256),
            ("package", foreign, file_entry(b"same").sha256),
            ("package", tree, file_entry(b"other").sha256),
        )
        for scope, requested_tree, digest in denied:
            with pytest.raises(SkillContentError) as failure:
                await content.read_file(owner, scope, requested_tree, digest, io.BytesIO())
            assert failure.value.code == "CONTENT_NOT_FOUND"


async def test_digest_without_original_tree_reference_is_not_authority(
    database: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """
    已有对象行及磁盘字节不能替代原树的明确对象引用。

    :param database (async_sessionmaker[AsyncSession]): 独立事务工厂
    :param tmp_path (Path): 私有内容卷
    """
    owner = await user(database)
    tree = await store_tree(database, tmp_path, owner, {"a": b"private"})
    digest = file_entry(b"private").sha256
    async with database.begin() as session:
        await session.execute(
            delete(SkillTreeObjectReference).where(
                SkillTreeObjectReference.user_id == owner,
                SkillTreeObjectReference.tree_digest == tree,
            )
        )
    async with database.begin() as session:
        with pytest.raises(SkillContentError) as failure:
            await service(session, tmp_path).read_file(owner, "package", tree, digest, io.BytesIO())
        assert failure.value.code == "CONTENT_NOT_FOUND"


async def test_node_file_downloads_do_not_select_full_manifest(
    node_client: AsyncClient, prepared: RuntimeHarness, tmp_path: Path
) -> None:
    """
    正式节点文件路由不查询完整 JSON，重复读取仍校验实际摘要及损坏字节。

    :param node_client (AsyncClient): 真实节点认证测试客户端
    :param prepared (RuntimeHarness): 原始精确准备快照
    :param tmp_path (Path): 私有内容卷
    """
    base = f"/api/v1/node/skill-snapshots/{prepared.snapshot}"
    params = {"task_id": str(prepared.task)}
    response = await node_client.get(base, params=params)
    assert response.status_code == 200
    entry = next(
        item for item in response.json()["data"]["manifest"]["entries"] if item["kind"] == "file"
    )
    async with prepared.database() as session:
        engine = session.get_bind()
    statements: list[str] = []

    def observe(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        """
        只记录 SQL 模板，绝不记录令牌、文件正文或绑定参数。

        :param connection (Connection): 当前驱动连接
        :param cursor (object): 不读取的游标
        :param statement (str): SQL 模板
        :param parameters (object): 不读取的参数
        :param context (ExecutionContext): 驱动执行上下文
        :param executemany (bool): 是否为批量执行
        """
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        for _ in range(2):
            response = await node_client.get(base + "/files/" + entry["sha256"], params=params)
            assert response.status_code == 200, response.text
            assert len(response.content) == entry["size"]
        path = tmp_path / "objects" / str(prepared.owner) / entry["sha256"][:2] / entry["sha256"]
        path.chmod(0o600)
        path.write_bytes(b"corrupted")
        path.chmod(0o400)
        response = await node_client.get(base + "/files/" + entry["sha256"], params=params)
        assert (
            response.status_code == 422
            and response.json()["errors"][0]["code"] == "CONTENT_INVALID"
        )
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert statements
    assert not any("skill_stored_trees.manifest_json" in statement for statement in statements)
