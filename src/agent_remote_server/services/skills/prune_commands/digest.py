"""
流式编码完整内部计划，集合插入顺序不改变身份，任何未支持类型直接拒绝。
"""

import hashlib
from collections.abc import Iterator
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel


def digest(value: object) -> str:
    """
    逐字段更新摘要，不复制整份目录清单或将未知对象转换为字符串。

    :param value (object): 仅包含明确支持类型的内部计划
    :return str: 带协议域的规范摘要
    """
    result = hashlib.sha256(b"agent-remote:prune-plan:v1\x00")
    for chunk in _encode(value):
        result.update(chunk)
    return result.hexdigest()


def _scalar(tag: bytes, value: bytes) -> bytes:
    """
    长度前缀防止相邻字段或不同类型产生歧义。

    :param tag (bytes): 精确类型标签
    :param value (bytes): 标量编码
    :return bytes: 自描述标量
    """
    return tag + str(len(value)).encode("ascii") + b":" + value


def _encode(value: object) -> Iterator[bytes]:
    """
    有序集合保留顺序，无序容器仅排序键或成员的规范编码。

    :param value (object): 内部计划节点，不接受任意序列化扩展
    :return Iterator[bytes]: 长度明确且可以增量散列的片段
    """
    if value is None:
        yield b"n"
    elif isinstance(value, bool):
        yield b"t" if value else b"f"
    elif isinstance(value, int):
        yield _scalar(b"i", str(value).encode("ascii"))
    elif isinstance(value, str):
        yield _scalar(b"s", value.encode("utf-8"))
    elif isinstance(value, UUID):
        yield _scalar(b"u", str(value).encode("ascii"))
    elif isinstance(value, datetime):
        stamp = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        yield _scalar(b"d", stamp.isoformat(timespec="microseconds").encode("ascii"))
    elif isinstance(value, (tuple, list)):
        yield _scalar(b"q" if isinstance(value, tuple) else b"l", str(len(value)).encode())
        for item in value:
            yield from _encode(item)
    elif isinstance(value, (set, frozenset)):
        yield _scalar(b"e", str(len(value)).encode())
        for encoded in sorted(b"".join(_encode(item)) for item in value):
            yield encoded
    elif isinstance(value, dict):
        yield _scalar(b"m", str(len(value)).encode())
        ordered = sorted((b"".join(_encode(key)), key) for key in value)
        for encoded, key in ordered:
            yield encoded
            yield from _encode(value[key])
    elif isinstance(value, BaseModel) or (is_dataclass(value) and not isinstance(value, type)):
        names = (
            tuple(type(value).model_fields)
            if isinstance(value, BaseModel)
            else tuple(field.name for field in fields(value))
        )
        yield _scalar(b"c", (type(value).__module__ + "." + type(value).__qualname__).encode())
        yield from _encode(len(names))
        for name in names:
            yield from _encode(name)
            yield from _encode(getattr(value, name))
    else:
        raise TypeError("unsupported prune plan value type")
