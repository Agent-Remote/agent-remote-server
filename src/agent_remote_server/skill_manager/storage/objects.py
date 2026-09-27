"""
为异步服务提供有界、不可变且按用户隔离的内容存取。
"""

from collections.abc import Iterator
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.skill_manager.storage.deletion import delete_with_receipt
from agent_remote_server.skill_manager.storage.filesystem import PrivateObjectFiles, verify_stream
from agent_remote_server.skill_manager.storage.io import run_storage_io
from agent_remote_server.skill_manager.storage.validation import ContentVerifier

_BLOCK_SIZE = 1024 * 1024


class PrivateObjectStore:
    """
    只接受已授权用户及引用的字节层，不替代事务权限和配额预留。
    """

    def __init__(self, root: Path) -> None:
        """
        配置部署管理的持久化目录。

        :param root (Path): 私有内容卷目录
        """
        self._files = PrivateObjectFiles(root)

    async def put_file(self, owner_id: UUID, entry: SkillTreeEntry, source: BinaryIO) -> bool:
        """
        从暂存流读取并完整验证文件后原子发布，始终不覆盖已有对象。

        :param owner_id (UUID): 来自身份授权的用户标识
        :param entry (SkillTreeEntry): 事务中已预留配额的文件元数据
        :param source (BinaryIO): 调用方持有的完整输入流
        :return bool: 是否创建了新对象
        """
        return await run_storage_io(partial(self._put_file, owner_id, entry, source))

    def _put_file(self, owner_id: UUID, entry: SkillTreeEntry, source: BinaryIO) -> bool:
        """
        在线程内完成独立暂存、流式校验、落盘和发布。

        :param owner_id (UUID): 已授权用户标识
        :param entry (SkillTreeEntry): 文件元数据
        :param source (BinaryIO): 上传输入流
        :return bool: 是否新增对象
        """
        verifier = ContentVerifier(entry)
        with self._files.staged_file(owner_id) as (directory, name, target):
            while chunk := source.read(_BLOCK_SIZE):
                verifier.update(chunk)
                target.write(chunk)
            verifier.finish()
            return self._files.publish(directory, name, target, entry)

    async def collect_uncommitted(
        self, owner_id: UUID, protected: set[str], expired: set[str], cutoff: datetime
    ) -> None:
        """
        在调用方持有用户事务锁时清理失去租约的对象及崩溃暂存。

        :param owner_id (UUID): 已授权用户标识
        :param protected (set[str]): 已登记或有效上传引用的摘要
        :param expired (set[str]): 已过期上传曾声明的摘要
        :param cutoff (datetime): 无记录孤立文件的保留截止时间
        """
        await run_storage_io(
            partial(
                self._files.collect_uncommitted, owner_id, protected, expired, cutoff.timestamp()
            )
        )

    async def verify_manifest(self, owner_id: UUID, manifest: SkillTreeManifest) -> None:
        """
        在登记可恢复引用前验证完整清单的所有文件已持久化。

        :param owner_id (UUID): 已授权用户标识
        :param manifest (SkillTreeManifest): 待发布的完整清单
        """
        await run_storage_io(partial(self._verify_manifest, owner_id, manifest))

    async def delete_committed(self, owner_id: UUID, digest: str, size: int, task_id: UUID) -> None:
        """
        原持久化任务和用户锁必须覆盖磁盘线程实际完成，包括协程取消后的等待。

        :param owner_id (UUID): 已授权且锁定的用户
        :param digest (str): 已提交任务的精确摘要
        :param size (int): 已验证原对象长度
        :param task_id (UUID): 原持久化删除任务
        """
        await run_storage_io(
            partial(delete_with_receipt, self._files, owner_id, digest, size, task_id)
        )

    def _verify_manifest(self, owner_id: UUID, manifest: SkillTreeManifest) -> None:
        """
        对各内容身份去重，但仍检查同摘要的元数据一致性。

        :param owner_id (UUID): 已授权用户标识
        :param manifest (SkillTreeManifest): 完整内容清单
        """
        verified: set[tuple[str, int, str]] = set()
        for entry in manifest.entries:
            if entry.kind != "file":
                continue
            identity = (entry.sha256, entry.size, entry.content_kind)
            if identity in verified:
                continue
            with self._files.read(owner_id, entry.sha256) as source:
                verify_stream(source, entry)
            verified.add(identity)

    async def read_prefix(self, owner_id: UUID, entry: SkillTreeEntry, limit: int) -> bytes:
        """
        先流式验证完整对象，再有界读取格式解析所需的头部。

        :param owner_id (UUID): 已授权用户标识
        :param entry (SkillTreeEntry): 已授权文件元数据
        :param limit (int): 最多返回的头部字节数
        :return bytes: 经过完整文件校验的有限头部
        """
        if not 0 <= limit <= 1024 * 1024:
            raise ValueError("content prefix limit is out of range")
        return await run_storage_io(partial(self._read_prefix, owner_id, entry, limit))

    def _read_prefix(self, owner_id: UUID, entry: SkillTreeEntry, limit: int) -> bytes:
        """
        在线程中校验和读取，避免大型说明文件占用等量内存。

        :param owner_id (UUID): 已授权用户标识
        :param entry (SkillTreeEntry): 文件元数据
        :param limit (int): 最大头部长度
        :return bytes: 有界文件前缀
        """
        with self._files.read(owner_id, entry.sha256) as source:
            verify_stream(source, entry)
            source.seek(0)
            return source.read(limit)

    async def copy_file(self, owner_id: UUID, entry: SkillTreeEntry, target: BinaryIO) -> None:
        """
        校验后拷贝到调用方暂存流，调用方须在成功返回后才对外发布。

        :param owner_id (UUID): 已授权用户标识
        :param entry (SkillTreeEntry): 已授权引用的文件元数据
        :param target (BinaryIO): 调用方私有输出暂存流
        """
        await run_storage_io(partial(self._copy_file, owner_id, entry, target))

    def _copy_file(self, owner_id: UUID, entry: SkillTreeEntry, target: BinaryIO) -> None:
        """
        单次读取同时验证和复制文件，错误时不声称输出完整。

        :param owner_id (UUID): 已授权用户标识
        :param entry (SkillTreeEntry): 已授权引用的文件元数据
        :param target (BinaryIO): 输出暂存流
        """
        verifier = ContentVerifier(entry)
        with self._files.read(owner_id, entry.sha256) as source:
            for chunk in _chunks(source):
                verifier.update(chunk)
                _write_all(target, chunk)
        verifier.finish()


def _chunks(source: BinaryIO) -> Iterator[bytes]:
    """
    用固定大小读取普通文件，保持大文件内存开销有界。

    :param source (BinaryIO): 文件输入流
    :return Iterator[bytes]: 有界内容块
    """
    while chunk := source.read(_BLOCK_SIZE):
        yield chunk


def _write_all(target: BinaryIO, chunk: bytes) -> None:
    """
    处理输出流短写，避免导出时将不完整数据视为成功。

    :param target (BinaryIO): 调用方输出流
    :param chunk (bytes): 必须完整保存的数据块
    :raises OSError: 输出流未取得写入进展
    """
    remaining = memoryview(chunk)
    while remaining:
        written = target.write(remaining)
        if written is None or written <= 0 or written > len(remaining):
            raise OSError("content output did not make progress")
        remaining = remaining[written:]
