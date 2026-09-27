"""
定义跨语言一致且不访问宿主文件系统的技能清单。
"""

import unicodedata
from collections import deque
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def validate_relative_path(value: str) -> str:
    """
    验证规范化的相对目录路径。

    :param value (str): 待验证路径
    :return str: 已验证的原路径
    :raises ValueError: 路径不规范或超出协议边界
    """
    if not value or value.startswith("/") or "\\" in value:
        raise ValueError("skill path must be relative POSIX text")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("skill path must be NFC normalized")
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise ValueError("skill path contains forbidden characters")
    if len(value.encode("utf-8")) > 4096:
        raise ValueError("skill path is too long")
    for component in value.split("/"):
        if component in {"", ".", ".."} or len(component.encode("utf-8")) > 255:
            raise ValueError("skill path contains an invalid component")
    return value


class SkillTreeEntry(BaseModel):
    """
    技能目录中一个文件、目录或链接的不可变描述。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    path: str = Field(description="规范相对路径")
    kind: Literal["file", "directory", "symlink", "runtime_link"] = Field(
        description="目录条目类型"
    )
    mode: int = Field(default=0o644, strict=True, ge=0, le=0o777, description="普通权限位")
    size: int = Field(default=0, strict=True, ge=0, le=2**63 - 1, description="文件字节数")
    sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$", description="文件内容摘要")
    target: str = Field(default="", description="链接目标")
    content_kind: Literal["", "text", "binary"] = Field(default="", description="文件内容类型")
    dependency: str = Field(
        default="", pattern=r"^(?:[a-z][a-z0-9_.-]{0,63})?$", description="运行时依赖标识"
    )

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """
        验证条目路径。

        :param value (str): 路径内容
        :return str: 规范路径
        """
        return validate_relative_path(value)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        """
        拒绝不同条目类型之间混用字段。

        :return Self: 已验证条目
        :raises ValueError: 字段组合不合法
        """
        if self.kind == "file":
            if not self.sha256 or not self.content_kind or self.target or self.dependency:
                raise ValueError("file entry has inconsistent metadata")
            return self
        if self.size or self.sha256 or self.content_kind:
            raise ValueError("non-file entry cannot contain file metadata")
        if self.kind == "directory":
            if self.target or self.dependency:
                raise ValueError("directory cannot contain a link target")
            return self
        if self.mode != 0o777 or not self.target or "\\" in self.target:
            raise ValueError("link entry requires a target and mode 0777")
        if any(unicodedata.category(char) in {"Cc", "Cs"} for char in self.target):
            raise ValueError("link target contains forbidden characters")
        if len(self.target.encode("utf-8")) > 4096:
            raise ValueError("link target is too long")
        if unicodedata.normalize("NFC", self.target) != self.target:
            raise ValueError("link target must be NFC normalized")
        if self.kind == "runtime_link":
            if not self.target.startswith("/") or not self.dependency:
                raise ValueError("runtime link requires an absolute target and dependency")
            validate_relative_path(self.target[1:])
        elif self.target.startswith("/") or self.dependency:
            raise ValueError("ordinary link must stay relative and cannot claim a dependency")
        return self


class SkillTreeManifest(BaseModel):
    """
    包含完整有序目录树的第一版清单。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    version: int = Field(default=1, strict=True, ge=1, le=1, description="清单协议版本")
    entries: tuple[SkillTreeEntry, ...] = Field(
        default=(), max_length=100_000, description="按路径字节排序的完整条目"
    )

    @model_validator(mode="after")
    def validate_tree(self) -> Self:
        """
        验证排序、父目录、链接完整性和条目唯一性。

        :return Self: 已验证的完整清单
        :raises ValueError: 树结构有冲突或链接越界
        """
        by_path: dict[str, SkillTreeEntry] = {}
        previous = b""
        total = 0
        for entry in self.entries:
            encoded = entry.path.encode("utf-8")
            if encoded <= previous:
                raise ValueError("manifest paths must be unique and UTF-8 sorted")
            previous = encoded
            total += entry.size
            if total > 2**63 - 1:
                raise ValueError("skill tree byte count overflows the protocol")
            parts = entry.path.split("/")
            for index in range(1, len(parts)):
                parent = by_path.get("/".join(parts[:index]))
                if parent is None or parent.kind != "directory":
                    raise ValueError("manifest requires explicit directory parents")
            by_path[entry.path] = entry
        for entry in self.entries:
            if entry.kind == "symlink":
                resolve_manifest_link(entry, by_path)
        return self

    @property
    def total_bytes(self) -> int:
        """
        计算完整树的展开文件大小。

        :return int: 文件总字节数
        """
        return sum(entry.size for entry in self.entries)


def resolve_manifest_link(
    entry: SkillTreeEntry, entries: dict[str, SkillTreeEntry]
) -> tuple[str, tuple[str, ...]]:
    """
    按 POSIX 顺序解析链接而不访问真实文件系统。

    :param entry (SkillTreeEntry): 待解析的链接
    :param entries (dict[str, SkillTreeEntry]): 已验证路径索引
    :return tuple[str, tuple[str, ...]]: 最终目标与按实际解析顺序依赖的路径
    :raises ValueError: 链接悬空、循环或越界
    """
    resolved = entry.path.split("/")[:-1]
    pending = deque(entry.target.split("/"))
    hops = 0
    dependencies: dict[str, None] = {}
    while pending:
        component = pending.popleft()
        if component in {"", "."}:
            continue
        if component == "..":
            if not resolved:
                raise ValueError("link escapes manifest root")
            resolved.pop()
            continue
        path = "/".join([*resolved, component])
        target = entries.get(path)
        if target is None:
            raise ValueError("link target is missing from manifest")
        dependencies.setdefault(target.path, None)
        if target.kind == "symlink":
            hops += 1
            if hops > 40:
                raise ValueError("link resolution exceeds the cycle limit")
            pending.extendleft(reversed(target.target.split("/")))
            continue
        if pending and target.kind != "directory":
            raise ValueError("link traverses a non-directory entry")
        resolved.append(component)
    if not resolved:
        raise ValueError("link to the manifest root creates a directory cycle")
    result = "/".join(resolved)
    if entry.path.startswith(result + "/"):
        raise ValueError("link to an ancestor directory creates a cycle")
    return result, tuple(dependencies)
