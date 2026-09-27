"""
用有界 TCP 转发对真实 SSH 加密流限速，不替换 CLI、网关或任何授权逻辑。
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


@asynccontextmanager
async def export_link(target: int, slow: bool) -> AsyncIterator[int]:
    """
    首次连接限速超过原始授权寿命，后续撤销验证使用普通转发。

    :param target (int): 一次性容器的本机 SSH 端口
    :param slow (bool): 是否显式启用真实长传输验收
    :return AsyncIterator[int]: CLI 使用的本机端口
    """
    if not slow:
        yield target
        return
    tasks: set[asyncio.Task[None]] = set()
    first = True

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """
        拥有一条有界转发连接和双向任务，退出时关闭并等待全部资源。

        :param reader (asyncio.StreamReader): CLI 的加密输入
        :param writer (asyncio.StreamWriter): 发往 CLI 的加密输出
        """
        nonlocal first
        paced, first = first, False
        upstream: asyncio.StreamWriter | None = None
        try:
            remote, upstream = await asyncio.open_connection("127.0.0.1", target, limit=16384)
            async with asyncio.TaskGroup() as group:
                group.create_task(copy_stream(reader, upstream, False))
                group.create_task(copy_stream(remote, writer, paced))
        finally:
            for stream in (writer, upstream):
                if stream is not None:
                    stream.close()
                    with contextlib.suppress(ConnectionError):
                        await stream.wait_closed()

    def accepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """
        保存连接任务直到测试结束，所有异常由拥有者收集。

        :param reader (asyncio.StreamReader): CLI 的加密输入
        :param writer (asyncio.StreamWriter): 发往 CLI 的加密输出
        """
        tasks.add(asyncio.create_task(connection(reader, writer)))

    server = await asyncio.start_server(accepted, "127.0.0.1", 0, limit=16384)
    try:
        yield int(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def copy_stream(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, paced: bool
) -> None:
    """
    保留握手速度，随后限制下载为每半秒最多八 KiB，九百三十秒后恢复速度。

    :param reader (asyncio.StreamReader): 加密源字节流
    :param writer (asyncio.StreamWriter): 对端连接
    :param paced (bool): 是否限制这条下载流
    """
    count = 0
    started: float | None = None
    while data := await reader.read(8192):
        count += len(data)
        if paced and count > 65536:
            if started is None:
                started = time.monotonic()
            if time.monotonic() - started < 930:
                await asyncio.sleep(0.5)
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()
        await writer.drain()
