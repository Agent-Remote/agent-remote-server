"""
验证私有存储的内容身份、权限、异常收尾与大文件资源边界。
"""

import asyncio
import hashlib
import io
import os
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import SkillStoragePolicy
from agent_remote_server.skill_manager.storage.validation import ContentVerifier


def file_entry(content: bytes, *, path: str = "SKILL.md") -> SkillTreeEntry:
    """
    从测试字节生成真实的文件身份。

    :param content (bytes): 测试文件完整内容
    :param path (str): 清单路径
    :return SkillTreeEntry: 真实文件元数据
    """
    try:
        content.decode("utf-8")
        is_text = b"\x00" not in content
    except UnicodeDecodeError:
        is_text = False
    return SkillTreeEntry(
        path=path,
        kind="file",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content_kind="text" if is_text else "binary",
    )


@pytest.mark.parametrize("content", [b"", "中文🦋".encode(), b"\xc3", b"a\x00b", b"\xff"])
def test_stream_validation_across_utf8_boundaries(content: bytes) -> None:
    """
    分块不得改变完整文件的文本或二进制分类。

    :param content (bytes): 覆盖多种编码边界的文件内容
    """
    entry = file_entry(content)
    verifier = ContentVerifier(entry)
    for byte in content:
        verifier.update(bytes([byte]))
    verifier.finish()
    opposite = entry.model_copy(
        update={"content_kind": "binary" if entry.content_kind == "text" else "text"}
    )
    verifier = ContentVerifier(opposite)
    verifier.update(content)
    with pytest.raises(ValueError, match="classification"):
        verifier.finish()


async def test_objects_are_private_durable_and_idempotent(tmp_path: Path) -> None:
    """
    相同摘要也无法跨用户读取，重建实例能恢复已落盘字节。

    :param tmp_path (Path): 临时持久化卷
    """
    root = tmp_path / "objects"
    store = PrivateObjectStore(root)
    owner, stranger = uuid4(), uuid4()
    content = "用户学习结果\n".encode()
    entry = file_entry(content)
    assert await store.put_file(owner, entry, io.BytesIO(content))
    assert not await store.put_file(owner, entry, io.BytesIO(content))
    with pytest.raises(FileNotFoundError):
        await store.copy_file(stranger, entry, io.BytesIO())
    recovered = PrivateObjectStore(root)
    output = io.BytesIO()
    await recovered.copy_file(owner, entry, output)
    assert output.getvalue() == content
    await recovered.verify_manifest(owner, SkillTreeManifest(entries=(entry,)))
    assert await store.put_file(stranger, entry, io.BytesIO(content))
    first = root / str(owner) / entry.sha256[:2] / entry.sha256
    second = root / str(stranger) / entry.sha256[:2] / entry.sha256
    assert first.stat().st_ino != second.stat().st_ino
    assert first.stat().st_mode & 0o777 == 0o400
    assert root.stat().st_mode & 0o777 == 0o700
    assert not list(root.rglob("upload-*"))


@pytest.mark.parametrize("content", [b"short", b"different", b"excess-content"])
async def test_rejected_upload_never_publishes(tmp_path: Path, content: bytes) -> None:
    """
    长度错误和摘要错误都不能形成可下载的对象。

    :param tmp_path (Path): 临时持久化卷
    :param content (bytes): 与声明不一致的实际内容
    """
    root = tmp_path / "objects"
    store = PrivateObjectStore(root)
    owner = uuid4()
    entry = file_entry(b"expected!")
    with pytest.raises(ValueError):
        await store.put_file(owner, entry, io.BytesIO(content))
    with pytest.raises(FileNotFoundError):
        await store.copy_file(owner, entry, io.BytesIO())
    assert not list(root.rglob("upload-*"))


async def test_concurrent_upload_is_complete_and_deduplicated(tmp_path: Path) -> None:
    """
    两个上传并发发布同一对象时只有一个胜出，另一个验证完整旧对象。

    :param tmp_path (Path): 临时持久化卷
    """
    store = PrivateObjectStore(tmp_path / "objects")
    owner = uuid4()
    content = b"learning" * 100_000
    entry = file_entry(content)
    outcomes = await asyncio.gather(
        store.put_file(owner, entry, io.BytesIO(content)),
        store.put_file(owner, entry, io.BytesIO(content)),
    )
    assert sorted(outcomes) == [False, True]
    output = io.BytesIO()
    await store.copy_file(owner, entry, output)
    assert output.getvalue() == content


async def test_existing_corrupt_object_is_never_silently_reused(tmp_path: Path) -> None:
    """
    重复上传必须发现损坏，不能覆盖证据或宣称完整。

    :param tmp_path (Path): 临时持久化卷
    """
    root = tmp_path / "objects"
    store = PrivateObjectStore(root)
    owner = uuid4()
    entry = file_entry(b"original")
    await store.put_file(owner, entry, io.BytesIO(b"original"))
    target = root / str(owner) / entry.sha256[:2] / entry.sha256
    target.chmod(0o600)
    target.write_bytes(b"corrupt!")
    target.chmod(0o400)
    with pytest.raises(ValueError, match="digest"):
        await store.put_file(owner, entry, io.BytesIO(b"original"))
    with pytest.raises(ValueError, match="digest"):
        await store.verify_manifest(owner, SkillTreeManifest(entries=(entry,)))
    assert target.read_bytes() == b"corrupt!"


@pytest.mark.parametrize("level", ["root", "owner", "shard", "blob"])
async def test_storage_never_follows_replaced_links(tmp_path: Path, level: str) -> None:
    """
    任一可替换组件为链接时均不能读取或改写链接目标。

    :param tmp_path (Path): 临时持久化卷
    :param level (str): 链接攻击层级
    """
    root = tmp_path / "objects"
    owner = uuid4()
    store = PrivateObjectStore(root)
    entry = file_entry(b"original")
    await store.put_file(owner, entry, io.BytesIO(b"original"))
    components = {
        "root": root,
        "owner": root / str(owner),
        "shard": root / str(owner) / entry.sha256[:2],
        "blob": root / str(owner) / entry.sha256[:2] / entry.sha256,
    }
    target = components[level]
    displaced = tmp_path / "displaced"
    target.rename(displaced)
    target.symlink_to(displaced, target_is_directory=level != "blob")
    with pytest.raises(OSError):
        await store.copy_file(owner, entry, io.BytesIO())
    with pytest.raises(OSError):
        await store.put_file(owner, entry, io.BytesIO(b"original"))


async def test_incomplete_manifest_and_conflicting_metadata_are_rejected(tmp_path: Path) -> None:
    """
    摘要去重不能掩盖缺失内容或同摘要的错误类型声明。

    :param tmp_path (Path): 临时持久化卷
    """
    store = PrivateObjectStore(tmp_path / "objects")
    owner = uuid4()
    entry = file_entry(b"original", path="a")
    await store.put_file(owner, entry, io.BytesIO(b"original"))
    missing = file_entry(b"missing", path="b")
    with pytest.raises(FileNotFoundError):
        await store.verify_manifest(owner, SkillTreeManifest(entries=(entry, missing)))
    wrong = entry.model_copy(update={"path": "b", "content_kind": "binary"})
    with pytest.raises(ValueError, match="classification"):
        await store.verify_manifest(owner, SkillTreeManifest(entries=(entry, wrong)))


class InterruptedSource(io.BytesIO):
    """
    在读取一部分数据后模拟上传连接异常。
    """

    def read(self, size: int | None = -1) -> bytes:
        """
        返回首块，后续读取失败。

        :param size (int | None): 最大读取字节数
        :return bytes: 首块内容
        """
        if self.tell():
            raise OSError("input interrupted")
        return super().read(min(size if size is not None and size >= 0 else 2, 2))


async def test_interrupted_input_removes_only_its_staging_file(tmp_path: Path) -> None:
    """
    上传失败清理暂存但不改变之前已发布的数据。

    :param tmp_path (Path): 临时持久化卷
    """
    root = tmp_path / "objects"
    store = PrivateObjectStore(root)
    owner = uuid4()
    entry = file_entry(b"original")
    await store.put_file(owner, entry, io.BytesIO(b"original"))
    with pytest.raises(OSError, match="interrupted"):
        await store.put_file(owner, entry, InterruptedSource(b"original"))
    await store.verify_manifest(owner, SkillTreeManifest(entries=(entry,)))
    assert not list(root.rglob("upload-*"))


class PausedSource(io.BytesIO):
    """
    用事件控制线程读取，检查取消操作不会释放仍在使用的流。
    """

    def __init__(self, content: bytes, entered: Event, release: Event) -> None:
        """
        创建有显式同步点的测试源。

        :param content (bytes): 文件内容
        :param entered (Event): 已进入读取通知
        :param release (Event): 允许继续读取通知
        """
        super().__init__(content)
        self.entered = entered
        self.release = release

    def read(self, size: int | None = -1) -> bytes:
        """
        等待测试释放后读取。

        :param size (int | None): 最大读取字节数
        :return bytes: 输入块
        """
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("test did not release source")
        return super().read(size)


async def test_cancellation_waits_for_disk_worker_cleanup(tmp_path: Path) -> None:
    """
    取消等待不得使工作线程使用已关闭的输入或遗留活动暂存。

    :param tmp_path (Path): 临时持久化卷
    """
    root = tmp_path / "objects"
    store = PrivateObjectStore(root)
    entered, release = Event(), Event()
    entry = file_entry(b"original")
    with PausedSource(b"original", entered, release) as source:
        task = asyncio.create_task(store.put_file(uuid4(), entry, source))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not list(root.rglob("upload-*"))


def test_package_and_runtime_quotas_remain_independent() -> None:
    """
    运行文件允许超过安装文件限制，完整目录仍受独立总量限制。
    """
    policy = SkillStoragePolicy(
        package_file_bytes=4, package_bytes=8, checkpoint_bytes=12, directory_bytes=20
    )
    entry = file_entry(b"large-file")
    manifest = SkillTreeManifest(entries=(entry,))
    with pytest.raises(ValueError, match="file quota"):
        policy.validate_manifest(manifest, "package")
    policy.validate_manifest(manifest, "state")
    combined = SkillTreeManifest(
        entries=(entry.model_copy(update={"path": "a"}), entry.model_copy(update={"path": "b"}))
    )
    with pytest.raises(ValueError, match="tree quota"):
        policy.validate_manifest(combined, "state")
    policy.validate_manifest(combined, "account_directory")
    exceeded = SkillTreeManifest(entries=(*combined.entries, file_entry(b"x", path="c")))
    with pytest.raises(ValueError, match="tree quota"):
        policy.validate_manifest(exceeded, "account_directory")


def test_runtime_links_are_never_installation_assets() -> None:
    """
    安装包不得借运行依赖类型引入任意外部文件。
    """
    manifest = SkillTreeManifest(
        entries=(
            SkillTreeEntry(
                path="python",
                kind="runtime_link",
                mode=0o777,
                target="/usr/bin/python3",
                dependency="python",
            ),
        )
    )
    with pytest.raises(ValueError, match="runtime dependencies"):
        SkillStoragePolicy().validate_manifest(manifest, "package")
    SkillStoragePolicy().validate_manifest(manifest, "state")


async def test_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    """
    被替换为 FIFO 的对象读取必须立即拒绝而非等待写入者。

    :param tmp_path (Path): 临时持久化卷
    """
    root = tmp_path / "objects"
    owner = uuid4()
    entry = file_entry(b"original")
    store = PrivateObjectStore(root)
    await store.put_file(owner, entry, io.BytesIO(b"original"))
    target = root / str(owner) / entry.sha256[:2] / entry.sha256
    target.unlink()
    os.mkfifo(target, 0o400)
    with pytest.raises(ValueError, match="regular file"):
        await asyncio.wait_for(store.copy_file(owner, entry, io.BytesIO()), 2)


class ShortWriteTarget(io.BytesIO):
    """
    模拟底层流每次只能写入少量字节。
    """

    def write(self, buffer: object) -> int:
        """
        对测试中传入的内存视图只写两个字节。

        :param buffer (object): 待写入的二进制视图
        :return int: 实际写入长度
        """
        assert isinstance(buffer, memoryview)
        return super().write(buffer[:2])


async def test_export_handles_short_writes(tmp_path: Path) -> None:
    """
    即使目标流发生短写，成功返回也必须代表完整内容已保存。

    :param tmp_path (Path): 临时持久化卷
    """
    store = PrivateObjectStore(tmp_path / "objects")
    owner = uuid4()
    entry = file_entry(b"complete export")
    await store.put_file(owner, entry, io.BytesIO(b"complete export"))
    target = ShortWriteTarget()
    await store.copy_file(owner, entry, target)
    assert target.getvalue() == b"complete export"


async def test_orphan_cleanup_preserves_live_and_other_user_content(tmp_path: Path) -> None:
    """
    崩溃孤立文件按保留期清理，不能越过用户或有效内容引用。

    :param tmp_path (Path): 私有对象卷
    """
    from datetime import UTC, datetime, timedelta

    root = tmp_path / "objects"
    store = PrivateObjectStore(root)
    owner, stranger = uuid4(), uuid4()
    abandoned, live = file_entry(b"abandoned"), file_entry(b"live")
    await store.put_file(owner, abandoned, io.BytesIO(b"abandoned"))
    await store.put_file(owner, live, io.BytesIO(b"live"))
    await store.put_file(stranger, abandoned, io.BytesIO(b"abandoned"))
    old = datetime.now(UTC) - timedelta(days=2)
    orphan = root / str(owner) / abandoned.sha256[:2] / abandoned.sha256
    os.utime(orphan, (old.timestamp(), old.timestamp()))
    temporary = root / str(owner) / f"upload-{uuid4()}"
    temporary.write_bytes(b"incomplete")
    os.utime(temporary, (old.timestamp(), old.timestamp()))
    await store.collect_uncommitted(
        owner, {live.sha256}, set(), datetime.now(UTC) - timedelta(days=1)
    )
    assert not orphan.exists() and not temporary.exists()
    await store.verify_manifest(owner, SkillTreeManifest(entries=(live,)))
    await store.verify_manifest(stranger, SkillTreeManifest(entries=(abandoned,)))
