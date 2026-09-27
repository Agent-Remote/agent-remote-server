"""
用进程间文件锁和原任务完成回执防止数据库断连后的迟到线程删除新内容。
"""

import fcntl
import os
import stat
from uuid import UUID

from agent_remote_server.skill_manager.storage.filesystem import (
    PrivateObjectFiles,
    private_directory,
    validate_digest,
)


def delete_with_receipt(
    files: PrivateObjectFiles,
    owner_id: UUID,
    digest: str,
    size: int,
    task_id: UUID,
) -> None:
    """
    SQL 锁失效时仍串行磁盘动作，完成回执让同任务迟到执行不触碰重新上传的文件。

    :param files (PrivateObjectFiles): 私有卷目录操作
    :param owner_id (UUID): 原任务用户
    :param digest (str): 原任务摘要
    :param size (int): 原任务长度
    :param task_id (UUID): 原始持久化删除 UUID
    """
    validate_digest(digest)
    if size < 0:
        raise ValueError("invalid deletion size")
    receipt = f"{task_id}-{digest}-{size}"
    with files.owner_directory(owner_id, create=True) as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        try:
            with private_directory(owner, ".deletions", create=True) as directory:
                if _completed(directory, receipt):
                    return
                files.delete_committed(owner_id, digest, size)
                descriptor = os.open(
                    receipt,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o400,
                    dir_fd=directory,
                )
                try:
                    os.fchmod(descriptor, 0o400)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.fsync(directory)
        finally:
            fcntl.flock(owner, fcntl.LOCK_UN)


def _completed(directory: int, name: str) -> bool:
    """
    只接受服务所有的不可变零字节普通完成回执，链接或损坏回执拒绝删除。

    :param directory (int): 私有回执目录描述符
    :param name (str): 从原 UUID、摘要和长度构造的单组件名称
    :return bool: 是否已有有效完成证据
    """
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory
        )
    except FileNotFoundError:
        return False
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o400
            or info.st_size != 0
        ):
            raise ValueError("invalid content deletion receipt")
        os.fsync(descriptor)
        os.fsync(directory)
        return True
    finally:
        os.close(descriptor)
