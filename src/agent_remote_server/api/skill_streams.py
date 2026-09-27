"""
在内容校验后共享有界流式响应的文件清理协议。
"""

import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from functools import partial
from typing import BinaryIO, cast

from fastapi import Request

from agent_remote_server.schemas.skill_manifest import SkillTreeEntry
from agent_remote_server.services.skills.content import SkillContentError
from agent_remote_server.skill_manager.storage.io import run_storage_io
from agent_remote_server.skill_manager.storage.validation import ContentVerifier


def download_chunks(source: BinaryIO) -> Iterator[bytes]:
    """
    在响应工作线程逐块读取，正常结束和断线都释放私有暂存。

    :param source (BinaryIO): 已完整验证的输出暂存
    :return Iterator[bytes]: 有界二进制块
    """
    try:
        while chunk := source.read(64 * 1024):
            yield chunk
    finally:
        source.close()


@asynccontextmanager
async def receive_verified_file(request: Request, entry: SkillTreeEntry) -> AsyncIterator[BinaryIO]:
    """
    以已授权大小为边界验证网络流，并在线程中读写私有暂存。

    :param request (Request): 已认证的原始网络请求
    :param entry (SkillTreeEntry): 原始清单内声明的文件
    :return AsyncIterator[BinaryIO]: 已完整验证且定位到起点的暂存流
    """
    verifier = ContentVerifier(entry)
    # 暂存可能已转为磁盘文件，退出时在线程中关闭以免阻塞事件循环。
    source = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")  # noqa: SIM115
    try:
        try:
            async for chunk in request.stream():
                verifier.update(chunk)
                await run_storage_io(partial(source.write, chunk))
            verifier.finish()
        except ValueError as error:
            raise SkillContentError(
                "CONTENT_INVALID", "uploaded bytes do not match manifest"
            ) from error
        await run_storage_io(partial(source.seek, 0))
        yield cast(BinaryIO, source)
    finally:
        await run_storage_io(source.close)
