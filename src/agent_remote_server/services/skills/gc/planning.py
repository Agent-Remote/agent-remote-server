"""
以不可变计划分别解释树引用、分类额度和共享文件，不把计划当作已释放磁盘空间。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from agent_remote_server.repositories.skill_retention import RetentionIndex
from agent_remote_server.services.skills.retention.trees import StoredTreeRetention
from agent_remote_server.services.skills.retention.uploads import (
    active_uploads,
    upload_file_references,
)
from agent_remote_server.skill_manager.retention.graph import RetentionKey


@dataclass(frozen=True, order=True)
class ContentUploadLease:
    """
    零字节预留也必须保留的精确上传身份和有效期。
    """

    id: UUID
    category: str
    expires_at: datetime


@dataclass(frozen=True)
class ContentObjectRelease:
    """
    分类对象的原身份、实际树引用及本次是否解除计量。
    """

    key: RetentionKey
    size: int
    content_kind: str
    status: str
    created_at: datetime
    tree_keys: tuple[RetentionKey, ...]
    selected: bool
    release: bool


@dataclass(frozen=True)
class ContentBlobRelease:
    """
    一个共享文件的两分类事实和全部有效上传，不重复计算物理长度。
    """

    digest: str
    objects: tuple[ContentObjectRelease, ...]
    leases: tuple[ContentUploadLease, ...]

    @property
    def delete_file(self) -> bool:
        """
        只有全部分类对象都被释放且没有任一上传租约时，才能计划物理删除。

        :return bool: 是否需持久化物理删除任务
        """
        return bool(self.objects) and all(row.release for row in self.objects) and not self.leases


@dataclass(frozen=True)
class ContentReclamationPlan:
    """
    可整体重建比较的用户授权计划，不承担公开幂等回执语义。
    """

    user_id: UUID
    requested_trees: tuple[RetentionKey, ...]
    requested_objects: tuple[RetentionKey, ...]
    all_unreferenced: bool
    trees: tuple[StoredTreeRetention, ...]
    blockers: tuple[tuple[RetentionKey, tuple[str, ...]], ...]
    blobs: tuple[ContentBlobRelease, ...]

    @property
    def ready(self) -> bool:
        """
        整份计划都通过才允许任何树、配额或任务变更。

        :return bool: 是否没有阻断项
        """
        return not self.blockers

    @property
    def package_bytes(self) -> int:
        """
        仅在整份计划可执行时计算原始包分类的实际释放。

        :return int: 原始包逻辑额度字节
        """
        return self._released("package_object")

    @property
    def state_bytes(self) -> int:
        """
        仅在整份计划可执行时计算运行内容分类的实际释放。

        :return int: 运行内容逻辑额度字节
        """
        return self._released("state_object")

    def _released(self, kind: str) -> int:
        """
        分类唯一对象只计一次，仍有树或上传的对象不释放。

        :param kind (str): 精确额度对象种类
        :return int: 可执行计划的分类释放字节
        """
        if not self.ready:
            return 0
        return sum(
            row.size
            for blob in self.blobs
            for row in blob.objects
            if row.release and row.key.kind == kind
        )

    @property
    def pending_file_bytes(self) -> int:
        """
        返回需要持久化删除任务的去重字节，不能当作磁盘已释放。

        :return int: 待删除共享文件的字节数
        """
        if not self.ready:
            return 0
        return sum(blob.objects[0].size for blob in self.blobs if blob.delete_file)


def utc(value: datetime) -> datetime:
    """
    SQLite 的无时区持久化值仍按已约定的 UTC 解释。

    :param value (datetime): 持久化时间
    :return datetime: 带 UTC 时区的时间
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def upload_leases(
    index: RetentionIndex, digests: set[str], now: datetime
) -> dict[str, tuple[ContentUploadLease, ...]]:
    """
    只解析有效上传并保留所有分类；已过期上传没有继续 complete 的权利。

    :param index (RetentionIndex): 同用户完整引用库存
    :param digests (set[str]): 本次受影响共享摘要
    :param now (datetime): 固定真实时间
    :return dict[str, tuple[ContentUploadLease, ...]]: 每个摘要的全部有效租约
    """
    result: dict[str, list[ContentUploadLease]] = {}
    uploads = active_uploads(index, now)
    leases = {
        upload.id: ContentUploadLease(
            upload.id, "package" if upload.scope == "package" else "state", utc(upload.expires_at)
        )
        for upload in uploads.values()
    }
    for upload_id, digest in upload_file_references(index, uploads):
        if digest in digests:
            result.setdefault(digest, []).append(leases[upload_id])
    return {digest: tuple(sorted(rows)) for digest, rows in result.items()}
