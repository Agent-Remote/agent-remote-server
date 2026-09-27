"""
为默认字节容量验收生成完整物理文件，并经真实网络逐块发送。
"""

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

from httpx import AsyncClient
from skill_runtime_support import RuntimeHarness
from test_skill_storage import file_entry

from agent_remote_server.schemas.skill_finalizations import SkillFinalizationRequest
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest

GIB = 1024**3


def write_binary(path: Path, size: int, manifest_path: str) -> SkillTreeEntry:
    """
    完整写入随机字节并同步到磁盘，禁止稀疏文件或虚拟输入替代容量。

    :param path (Path): 本次验收独占的输入路径
    :param size (int): 实际字节数
    :param manifest_path (str): 清单中的相对路径
    :return SkillTreeEntry: 从实际写入字节计算的文件声明
    """
    digest = hashlib.sha256()
    remaining = size
    with path.open("xb") as target:
        while remaining:
            chunk = os.urandom(min(1024**2, remaining))
            assert target.write(chunk) == len(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        target.flush()
        os.fsync(target.fileno())
    info = path.stat()
    assert info.st_size == size and info.st_blocks * 512 >= size
    return SkillTreeEntry(
        path=manifest_path,
        kind="file",
        size=size,
        sha256=digest.hexdigest(),
        content_kind="binary",
    )


def directory_fixture(root: Path, batch: int, total_bytes: int) -> SkillTreeManifest:
    """
    生成十个格式有效技能，每项至多一 GiB，完整目录使用确切目标字节数。

    :param root (Path): 本批次输入根目录
    :param batch (int): 保证各批次说明文件及目录身份不同的序号
    :param total_bytes (int): 完整目录实际字节数
    :return SkillTreeManifest: 有序且摘要各异的完整输入
    """
    root.mkdir()
    entries: list[SkillTreeEntry] = []
    for index in range(10):
        name = f"capacity-{batch}-{index:02d}"
        directory = root / name
        directory.mkdir()
        entries.append(SkillTreeEntry(path=name, kind="directory", mode=0o755))
        document = f"---\nname: {name}\ndescription: Byte capacity fixture\n---\n".encode()
        (directory / "SKILL.md").write_bytes(document)
        entries.append(file_entry(document, path=name + "/SKILL.md"))
        item_bytes = GIB if index < 9 else total_bytes - 9 * GIB
        assert 0 < item_bytes <= GIB
        entries.append(
            write_binary(directory / "state.bin", item_bytes - len(document), name + "/state.bin")
        )
    manifest = SkillTreeManifest(entries=tuple(entries))
    assert manifest.total_bytes == total_bytes
    files = [entry for entry in entries if entry.kind == "file"]
    assert len({entry.sha256 for entry in files}) == len(files)
    return manifest


async def disk_chunks(path: Path) -> AsyncIterator[bytes]:
    """
    从实际磁盘文件读取有界块，避免客户端把整个 GiB 文件装入内存。

    :param path (Path): 已完整生成的源文件
    :return AsyncIterator[bytes]: 真实网络请求体的有界块
    """
    with path.open("rb") as source:
        while chunk := await asyncio.to_thread(source.read, 1024**2):
            yield chunk


async def begin_directory(
    client: AsyncClient, state: RuntimeHarness, manifest: SkillTreeManifest
) -> tuple[str, UUID]:
    """
    通过正式节点认证入口受理原始完整收尾输入。

    :param client (AsyncClient): 真实回环网络客户端
    :param state (RuntimeHarness): 该输入的精确已停止会话
    :param manifest (SkillTreeManifest): 待上传完整清单
    :return tuple[str, UUID]: 收尾路由和当前上传尝试
    """
    payload = SkillFinalizationRequest(
        session_id=state.session, idempotency_key=str(uuid4()), manifest=manifest, unclean=False
    )
    response = await client.post(
        f"/api/v1/node/skill-snapshots/{state.snapshot}/finalization",
        json=payload.model_dump(mode="json"),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert not response.json()["committed"]
    return f"/api/v1/node/skill-finalizations/{data['id']}", UUID(data["upload_id"])


async def upload_directory(
    client: AsyncClient, base: str, upload_id: UUID, manifest: SkillTreeManifest, root: Path
) -> None:
    """
    每个真实文件走有界接收和私有存储，全部发送后才请求完整验证。

    :param client (AsyncClient): 真实认证 HTTP 客户端
    :param base (str): 原收尾路由
    :param upload_id (UUID): 当前上传尝试
    :param manifest (SkillTreeManifest): 原始完整清单
    :param root (Path): 实际源文件根目录
    """
    transferred = 0
    params = {"upload_id": str(upload_id)}
    for entry in manifest.entries:
        if entry.kind != "file":
            continue
        response = await client.put(
            base + "/files/" + entry.sha256,
            params=params,
            headers={"Content-Length": str(entry.size)},
            content=disk_chunks(root / entry.path),
        )
        assert response.status_code == 200, response.text
        assert not response.json()["committed"] and response.json()["data"]["created"]
        transferred += entry.size
        if entry.path.endswith("/state.bin"):
            print(f"byte_capacity_uploaded={transferred}", flush=True)
    response = await client.post(base + "/complete", params=params)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "persisted" and response.json()["committed"]
    replay = await client.post(base + "/complete", params=params)
    assert replay.status_code == 200 and replay.json() == response.json()
