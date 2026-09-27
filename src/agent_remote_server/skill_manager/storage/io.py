"""
隔离磁盘工作线程并保证取消期间资源的生命周期。
"""

import asyncio
from collections.abc import Callable


async def run_storage_io[T](function: Callable[[], T]) -> T:
    """
    在线程执行磁盘操作，取消时等工作线程退出再释放调用方资源。

    :param function (Callable[[], T]): 同步磁盘操作
    :return T: 操作结果
    """
    task = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # 调用方取消仍优先返回；取出线程异常可避免未消费异常日志泄露路径。
        if not task.cancelled():
            task.exception()
        raise
