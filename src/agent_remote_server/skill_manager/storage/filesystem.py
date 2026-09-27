"""
用目录描述符保护私有对象目录，防止链接替换和不完整内容发布。
"""

import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry
from agent_remote_server.skill_manager.storage.validation import ContentVerifier

_BLOCK_SIZE = 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def validate_digest(digest: str) -> None:
    """
    阻止任何非固定长度摘要成为存储路径。

    :param digest (str): 对象 SHA-256 摘要
    :raises ValueError: 摘要格式不正确
    """
    if re.fullmatch(r"[a-f0-9]{64}", digest) is None:
        raise ValueError("invalid content digest")


@contextmanager
def private_directory(parent: int, name: str, *, create: bool) -> Iterator[int]:
    """
    相对于已打开父目录安全打开私有目录。

    :param parent (int): 父目录描述符
    :param name (str): 服务端生成的单个路径组件
    :param create (bool): 是否允许创建目录
    :return Iterator[int]: 生命周期受控的目录描述符
    :raises ValueError: 目录权限或所属身份不安全
    """
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            os.fsync(parent)
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("content directory must be owned by the service with mode 0700")
        yield descriptor
    finally:
        os.close(descriptor)


class PrivateObjectFiles:
    """
    按用户隔离、通过原子链接提交的不可变字节存储。
    """

    def __init__(self, root: Path) -> None:
        """
        固定持久化卷位置，父目录必须由部署管理。

        :param root (Path): 持久化私有对象根目录
        """
        self._root = root

    @contextmanager
    def owner_directory(self, owner_id: UUID, *, create: bool) -> Iterator[int]:
        """
        从私有根逐层打开身份隔离的对象目录。

        :param owner_id (UUID): 已由调用方鉴权的用户标识
        :param create (bool): 是否允许创建目录
        :return Iterator[int]: 用户目录描述符
        """
        if create:
            try:
                self._root.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                parent = os.open(self._root.parent, _DIRECTORY_FLAGS)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
        root = os.open(self._root, _DIRECTORY_FLAGS)
        try:
            info = os.fstat(root)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("content root must be owned by the service with mode 0700")
            with private_directory(root, str(owner_id), create=create) as owner:
                yield owner
        finally:
            os.close(root)

    @contextmanager
    def staged_file(self, owner_id: UUID) -> Iterator[tuple[int, str, BinaryIO]]:
        """
        创建不可被下载的独立暂存文件并保证退出时删除。

        :param owner_id (UUID): 已鉴权用户标识
        :return Iterator[tuple[int, str, BinaryIO]]: 用户目录、临时名称和写入流
        """
        with self.owner_directory(owner_id, create=True) as directory:
            name = f"upload-{uuid4()}"
            descriptor = os.open(
                name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    yield directory, name, stream
            finally:
                os.unlink(name, dir_fd=directory)
                os.fsync(directory)

    def publish(self, directory: int, name: str, stream: BinaryIO, entry: SkillTreeEntry) -> bool:
        """
        完整落盘后发布不可变对象，并校验并发写入者留下的既有对象。

        :param directory (int): 用户目录描述符
        :param name (str): 当前暂存名称
        :param stream (BinaryIO): 已完整验证的暂存写流
        :param entry (SkillTreeEntry): 预期内容元数据
        :return bool: 是否新建对象，重复上传返回假
        """
        validate_digest(entry.sha256)
        stream.flush()
        os.fchmod(stream.fileno(), 0o400)
        os.fsync(stream.fileno())
        with private_directory(directory, entry.sha256[:2], create=True) as shard:
            try:
                os.link(
                    name,
                    entry.sha256,
                    src_dir_fd=directory,
                    dst_dir_fd=shard,
                    follow_symlinks=False,
                )
            except FileExistsError:
                with self.open_blob(shard, entry.sha256) as existing:
                    verify_stream(existing, entry)
                os.fsync(shard)
                return False
            os.fsync(shard)
            return True

    @contextmanager
    def open_blob(self, shard: int, digest: str) -> Iterator[BinaryIO]:
        """
        打开不可变普通文件，拒绝链接、设备和不安全权限。

        :param shard (int): 用户摘要分片目录描述符
        :param digest (str): 已校验摘要
        :return Iterator[BinaryIO]: 只读文件流
        """
        descriptor = os.open(
            digest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=shard
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o400
            ):
                raise ValueError("content object must be a private immutable regular file")
        except BaseException:
            os.close(descriptor)
            raise
        with os.fdopen(descriptor, "rb") as stream:
            yield stream

    def collect_uncommitted(
        self, owner_id: UUID, protected: set[str], expired: set[str], cutoff: float
    ) -> None:
        """
        不跟随链接地回收无有效引用内容，保留目录用于幂等重试。

        :param owner_id (UUID): 已锁定事务的用户标识
        :param protected (set[str]): 跨额度分类的全部保活摘要
        :param expired (set[str]): 明确过期且不再可写的上传摘要
        :param cutoff (float): 无登记文件允许回收的时间戳
        """
        try:
            with self.owner_directory(owner_id, create=False) as owner:
                for name in os.listdir(owner):
                    if re.fullmatch(r"upload-[a-f0-9-]{36}", name):
                        _remove_old_file(owner, name, cutoff)
                    elif re.fullmatch(r"[a-f0-9]{2}", name):
                        with private_directory(owner, name, create=False) as shard:
                            for digest in os.listdir(shard):
                                if re.fullmatch(r"[a-f0-9]{64}", digest) is None:
                                    continue
                                if digest in protected:
                                    continue
                                deadline = float("inf") if digest in expired else cutoff
                                _remove_old_file(shard, digest, deadline)
                            os.fsync(shard)
                os.fsync(owner)
        except FileNotFoundError:
            # 从未上传字节或上次清理已完成时，数据库租约仍可以正常终结。
            return

    def delete_committed(self, owner_id: UUID, digest: str, size: int) -> None:
        """
        仅供持久化删除任务在用户锁内调用，异常文件不按摘要猜测可删除。

        :param owner_id (UUID): 原任务授权用户
        :param digest (str): 原任务确切共享摘要
        :param size (int): 已验证原对象长度
        """
        validate_digest(digest)
        if size < 0:
            raise ValueError("invalid deletion size")
        try:
            with self.owner_directory(owner_id, create=False) as owner:
                with private_directory(owner, digest[:2], create=False) as shard:
                    try:
                        with self.open_blob(shard, digest) as stream:
                            if os.fstat(stream.fileno()).st_size != size:
                                raise ValueError("deletion object size differs")
                        os.unlink(digest, dir_fd=shard)
                    except FileNotFoundError:
                        pass
                    os.fsync(shard)
                os.fsync(owner)
        except FileNotFoundError:
            return

    @contextmanager
    def read(self, owner_id: UUID, digest: str) -> Iterator[BinaryIO]:
        """
        只在已授权用户的命名空间查找对象。

        :param owner_id (UUID): 已鉴权用户标识
        :param digest (str): 已授权引用的内容摘要
        :return Iterator[BinaryIO]: 有界读取所用的文件流
        """
        validate_digest(digest)
        with (
            self.owner_directory(owner_id, create=False) as owner,
            private_directory(owner, digest[:2], create=False) as shard,
            self.open_blob(shard, digest) as stream,
        ):
            yield stream


def verify_stream(stream: BinaryIO, entry: SkillTreeEntry) -> None:
    """
    用固定内存重新验证完整磁盘对象。

    :param stream (BinaryIO): 普通文件只读流
    :param entry (SkillTreeEntry): 预期文件元数据
    """
    verifier = ContentVerifier(entry)
    while chunk := stream.read(_BLOCK_SIZE):
        verifier.update(chunk)
    verifier.finish()


def _remove_old_file(directory: int, name: str, cutoff: float) -> None:
    """
    仅删除服务拥有的普通文件，异常类型保留以便诊断。

    :param directory (int): 私有目录描述符
    :param name (str): 已校验的单组件对象名称
    :param cutoff (float): 最晚可回收修改时间
    """
    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("uncommitted content is not a service-owned regular file")
    if info.st_mtime <= cutoff:
        os.unlink(name, dir_fd=directory)
