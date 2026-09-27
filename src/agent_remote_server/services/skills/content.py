"""
编排内容上传、独立配额预留及完整树的原子登记。
"""

from datetime import UTC, datetime, timedelta
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.models.skill_storage import (
    SkillContentObject,
    SkillContentUpload,
    SkillStorageUsage,
    SkillStoredTree,
    SkillTreeObjectReference,
)
from agent_remote_server.repositories.skill_storage import SkillStorageRepository
from agent_remote_server.repositories.skill_tree_files import SkillTreeFileRepository
from agent_remote_server.schemas.skill_manifest import SkillTreeEntry, SkillTreeManifest
from agent_remote_server.services.skills.content_admission import require_available_files
from agent_remote_server.services.skills.content_errors import (
    SkillContentError as SkillContentError,
)
from agent_remote_server.services.skills.content_manifest import unique_files as _unique_files
from agent_remote_server.services.skills.content_upload_index import (
    SkillUploadDeclarations,
    validated_upload_manifest,
)
from agent_remote_server.services.skills.retention.clocks import retention_mutation
from agent_remote_server.skill_manager.manifest import manifest_digest
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore
from agent_remote_server.skill_manager.storage.policy import ContentScope, SkillStoragePolicy


class SkillContentService:
    """
    用数据库事务保护私有字节层，调用方负责提交或回滚事务。
    """

    def __init__(
        self, session: AsyncSession, store: PrivateObjectStore, policy: SkillStoragePolicy
    ) -> None:
        """
        绑定持久化事务、对象存储和部署配额。

        :param session (AsyncSession): 异步事务
        :param store (PrivateObjectStore): 私有字节存储
        :param policy (SkillStoragePolicy): 配额和保留配置
        """
        self._session = session
        self._repository = SkillStorageRepository(session)
        self._tree_files = SkillTreeFileRepository(session)
        self._declarations = SkillUploadDeclarations(session)
        self._store = store
        self._policy = policy

    async def begin(
        self, user_id: UUID, key: str, manifest: SkillTreeManifest, scope: ContentScope
    ) -> SkillContentUpload:
        """
        幂等登记完整上传计划并在同一事务预留唯一内容额度。

        :param user_id (UUID): 认证用户标识
        :param key (str): 客户端持久化的幂等键
        :param manifest (SkillTreeManifest): 完整上传清单
        :param scope (ContentScope): 本次内容范围
        :return SkillContentUpload: 原始或新建的上传计划
        """
        if not key or len(key) > 128 or not key.isascii() or not key.isprintable():
            raise SkillContentError("INVALID_IDEMPOTENCY_KEY", "invalid idempotency key")
        self._policy.validate_manifest(manifest, scope)
        entries = _unique_files(manifest)
        usage = await self._repository.lock_usage(user_id)
        await self._expire(user_id, usage)
        existing = await self._repository.upload_by_key(user_id, key)
        digest = manifest_digest(manifest)
        if existing is not None:
            if existing.tree_digest != digest or existing.scope != scope:
                raise SkillContentError(
                    "IDEMPOTENCY_CONFLICT", "key already belongs to another request"
                )
            return existing
        category = _category(scope)
        await require_available_files(self._repository, user_id, set(entries))
        stored = await self._repository.objects(user_id, category, set(entries))
        _validate_stored(entries, stored)
        required = sum(entry.size for digest, entry in entries.items() if digest not in stored)
        self._reserve(usage, category, required)
        upload = SkillContentUpload(
            id=uuid4(),
            user_id=user_id,
            idempotency_key=key,
            scope=scope,
            tree_digest=digest,
            manifest_json=manifest.model_dump(mode="json"),
            reserved_bytes=required,
            status="staged",
            expires_at=datetime.now(UTC) + timedelta(hours=self._policy.staging_hours),
        )
        self._repository.add(upload)
        await self._repository.flush()
        await self._declarations.repository.build(upload, entries)
        return upload

    async def validate_state_admission(self, user_id: UUID, manifest: SkillTreeManifest) -> None:
        """
        预览检查实际状态额度但不建立上传、预留或清理记录。

        :param user_id (UUID): 当前用户
        :param manifest (SkillTreeManifest): 拟完整保存的账户目录
        """
        await self.validate_state_admissions(user_id, (manifest,))

    async def validate_state_admissions(
        self, user_id: UUID, manifests: tuple[SkillTreeManifest, ...]
    ) -> None:
        """
        多份迁移比较树按摘要并集计量，预览不能分别通过后在提交时超额。

        :param user_id (UUID): 当前用户
        :param manifests (tuple[SkillTreeManifest, ...]): 同一事务需完整保留的全部树
        """
        entries: dict[str, SkillTreeEntry] = {}
        for manifest in manifests:
            self._policy.validate_manifest(manifest, "account_directory")
            for digest, entry in _unique_files(manifest).items():
                previous = entries.get(digest)
                if previous is not None and (previous.size, previous.content_kind) != (
                    entry.size,
                    entry.content_kind,
                ):
                    raise SkillContentError("CONTENT_INVALID", "shared digest metadata differs")
                entries[digest] = entry
        usage = await self._repository.lock_existing_usage(user_id)
        await require_available_files(self._repository, user_id, set(entries))
        stored = await self._repository.objects(user_id, "state", set(entries))
        _validate_stored(entries, stored)
        required = sum(entry.size for digest, entry in entries.items() if digest not in stored)
        reserved = usage.state_reserved if usage else 0
        for upload in await self._repository.expired_uploads(user_id, datetime.now(UTC)):
            if upload.scope != "package":
                reserved -= upload.reserved_bytes
        if (
            usage.state_bytes if usage else 0
        ) + reserved + required > self._policy.user_state_bytes:
            raise SkillContentError("QUOTA_EXCEEDED", "runtime state storage quota exceeded")

    async def prepare_file(self, user_id: UUID, upload_id: UUID, digest: str) -> SkillTreeEntry:
        """
        接收网络字节前锁定有效上传并确定精确文件上限。

        :param user_id (UUID): 认证用户标识
        :param upload_id (UUID): 已持久化上传计划
        :param digest (str): 清单内声明的摘要
        :return SkillTreeEntry: 已授权且仍可上传的文件描述
        """
        await self._repository.lock_usage(user_id)
        upload = await self.inspect_upload(user_id, upload_id)
        _require_active(upload)
        entry = await self._declarations.entry(upload, digest)
        await require_available_files(self._repository, user_id, {digest})
        return entry

    async def read_tree(self, user_id: UUID, scope: ContentScope, digest: str) -> SkillTreeManifest:
        """
        只向当前用户返回完整已提交清单。

        :param user_id (UUID): 认证用户标识
        :param scope (ContentScope): 内容额度分类
        :param digest (str): 完整树摘要
        :return SkillTreeManifest: 已授权树清单
        """
        if await self._repository.lock_existing_usage(user_id) is None:
            raise SkillContentError("CONTENT_NOT_FOUND", "content not found")
        tree = await self._repository.tree(user_id, _category(scope), digest)
        if tree is None:
            raise SkillContentError("CONTENT_NOT_FOUND", "content not found")
        manifest = SkillTreeManifest.model_validate(tree.manifest_json)
        await require_available_files(self._repository, user_id, set(_unique_files(manifest)))
        return manifest

    async def put_file(self, user_id: UUID, upload_id: UUID, digest: str, source: BinaryIO) -> bool:
        """
        只接收有效租约中声明的文件，整个写入期间保留用户事务锁。

        :param user_id (UUID): 认证用户标识
        :param upload_id (UUID): 上传租约标识
        :param digest (str): 此上传清单中声明的文件摘要
        :param source (BinaryIO): 已有界暂存的输入流
        :return bool: 是否新增磁盘对象，已有引用返回假且不消耗输入
        """
        await self._repository.lock_usage(user_id)
        upload = await self.inspect_upload(user_id, upload_id)
        _require_active(upload)
        entry = await self._declarations.entry(upload, digest)
        await require_available_files(self._repository, user_id, {digest})
        stored = await self._repository.objects(user_id, _category(upload.scope), {digest})
        _validate_stored({digest: entry}, stored)
        if digest in stored:
            return False
        return await self._store.put_file(user_id, entry, source)

    async def complete(self, user_id: UUID, upload_id: UUID) -> SkillStoredTree:
        """
        全部字节验证后一次性登记对象、完整树和实际使用量。

        :param user_id (UUID): 认证用户标识
        :param upload_id (UUID): 上传租约标识
        :return SkillStoredTree: 可以被后续版本或快照引用的完整树
        """
        usage = await self._repository.lock_usage(user_id)
        upload = await self._require_upload(user_id, upload_id)
        category = _category(upload.scope)
        if upload.status == "committed":
            tree = await self._repository.tree(user_id, category, upload.tree_digest)
            if tree is None:
                raise SkillContentError("CONTENT_EXPIRED", "committed tree is no longer retained")
            manifest = SkillTreeManifest.model_validate(tree.manifest_json)
            await require_available_files(self._repository, user_id, set(_unique_files(manifest)))
            return tree
        async with retention_mutation(self._session, user_id):
            await self._complete_active(user_id, upload, usage)
        tree = await self._repository.tree(user_id, category, upload.tree_digest)
        assert tree is not None
        return tree

    async def _complete_active(
        self, user_id: UUID, upload: SkillContentUpload, usage: SkillStorageUsage
    ) -> SkillStoredTree:
        """
        在同一保存点校验字节、登记引用和完整树等待事件，失败不会留下部分计量。

        :param user_id (UUID): 已锁定所有者
        :param upload (SkillContentUpload): 原始未完成上传
        :param usage (SkillStorageUsage): 同事务配额行
        :return SkillStoredTree: 完整已登记树，调用方仍须最终提交
        """
        category = _category(upload.scope)
        _require_active(upload)
        manifest = validated_upload_manifest(upload)
        entries = _unique_files(manifest)
        await require_available_files(self._repository, user_id, set(entries))
        await self._store.verify_manifest(user_id, manifest)
        stored = await self._repository.objects(user_id, category, set(entries))
        _validate_stored(entries, stored)
        added = 0
        for digest, entry in entries.items():
            if digest not in stored:
                added += entry.size
                self._repository.add(
                    SkillContentObject(
                        user_id=user_id,
                        category=category,
                        digest=digest,
                        size=entry.size,
                        content_kind=entry.content_kind,
                        status="available",
                    )
                )
        tree = await self._repository.tree(user_id, category, upload.tree_digest)
        if tree is None:
            tree = SkillStoredTree(
                user_id=user_id,
                category=category,
                digest=upload.tree_digest,
                manifest_json=upload.manifest_json,
                total_bytes=manifest.total_bytes,
            )
            self._repository.add(tree)
            await self._repository.flush()
            for digest in entries:
                self._repository.add(
                    SkillTreeObjectReference(
                        user_id=user_id,
                        category=category,
                        tree_digest=tree.digest,
                        object_digest=digest,
                    )
                )
        tree.retention_released_at = datetime.now(UTC)
        _release(usage, category, upload.reserved_bytes)
        if category == "package":
            usage.package_bytes += added
        else:
            usage.state_bytes += added
        upload.reserved_bytes = 0
        await self._declarations.repository.clear(upload)
        upload.status = "committed"
        await self._repository.flush()
        return tree

    async def get(self, user_id: UUID, upload_id: UUID) -> SkillContentUpload:
        """
        查询当前租约状态，过期只释放预留而不延长有效期。

        :param user_id (UUID): 认证用户标识
        :param upload_id (UUID): 上传标识
        :return SkillContentUpload: 当前用户的上传记录
        """
        usage = await self._repository.lock_usage(user_id)
        await self._expire(user_id, usage)
        await self._repository.flush()
        return await self._require_upload(user_id, upload_id)

    async def inspect_upload(self, user_id: UUID, upload_id: UUID) -> SkillContentUpload:
        """
        在调用方已有用户锁内只查询当前租约元数据，不扫描暂存卷或加载清单。

        :param user_id (UUID): 已授权并取得存储锁的用户
        :param upload_id (UUID): 原始上传标识
        :return SkillContentUpload: 当前数据库中的轻量上传记录
        """
        upload = await self._repository.upload(user_id, upload_id, include_manifest=False)
        if upload is None:
            raise SkillContentError("UPLOAD_NOT_FOUND", "upload not found")
        return upload

    async def read_file(
        self,
        user_id: UUID,
        scope: ContentScope,
        tree_digest: str,
        file_digest: str,
        target: BinaryIO,
    ) -> None:
        """
        仅通过当前用户已提交完整树授权下载，不能读取暂存对象。

        :param user_id (UUID): 认证用户标识
        :param scope (ContentScope): 原始或运行内容范围
        :param tree_digest (str): 已提交完整树摘要
        :param file_digest (str): 该树引用的文件摘要
        :param target (BinaryIO): 调用方私有输出暂存流
        """
        entry = await self.authorize_file(user_id, scope, tree_digest, file_digest)
        try:
            await self._store.copy_file(user_id, entry, target)
        except FileNotFoundError as error:
            raise SkillContentError(
                "CONTENT_INCOMPLETE", "retained content file is unavailable"
            ) from error
        except ValueError as error:
            raise SkillContentError(
                "CONTENT_INVALID", "retained content verification failed"
            ) from error

    async def authorize_file(
        self, user_id: UUID, scope: ContentScope, tree_digest: str, file_digest: str
    ) -> SkillTreeEntry:
        """
        在用户锁内通过完整树对象引用授权文件，整树仍受跨分类删除屏障约束。

        :param user_id (UUID): 已认证所有者
        :param scope (ContentScope): 原始内容类别
        :param tree_digest (str): 调用方授权的完整树
        :param file_digest (str): 该树内请求的唯一文件摘要
        :return SkillTreeEntry: 仅用于私有对象字节校验的普通文件声明
        """
        if await self._repository.lock_existing_usage(user_id) is None:
            raise SkillContentError("CONTENT_NOT_FOUND", "content not found")
        category = _category(scope)
        if not await self._tree_files.exists(user_id, category, tree_digest):
            raise SkillContentError("CONTENT_NOT_FOUND", "content not found")
        if await self._tree_files.has_unavailable_member(user_id, category, tree_digest):
            raise SkillContentError("CONTENT_UNAVAILABLE", "shared content is being retired")
        member = await self._tree_files.member(user_id, category, tree_digest, file_digest)
        if member is None:
            raise SkillContentError("CONTENT_NOT_FOUND", "content not found")
        try:
            return SkillTreeEntry.model_validate(
                {
                    "path": member.digest,
                    "kind": "file",
                    "size": member.size,
                    "sha256": member.digest,
                    "content_kind": member.content_kind,
                }
            )
        except ValueError as error:
            raise SkillContentError("CONTENT_INVALID", "stored file metadata is invalid") from error

    async def _require_upload(self, user_id: UUID, upload_id: UUID) -> SkillContentUpload:
        """
        隐藏其他用户的上传标识是否存在。

        :param user_id (UUID): 用户标识
        :param upload_id (UUID): 上传标识
        :return SkillContentUpload: 当前用户上传
        """
        upload = await self._repository.upload(user_id, upload_id)
        if upload is None:
            raise SkillContentError("UPLOAD_NOT_FOUND", "upload not found")
        return upload

    async def _expire(self, user_id: UUID, usage: SkillStorageUsage) -> None:
        """
        在已持有用户锁时释放无人使用的过期租约配额。

        :param user_id (UUID): 用户标识
        :param usage (SkillStorageUsage): 已锁定配额状态
        """
        now = datetime.now(UTC)
        expired = await self._repository.expired_uploads(user_id, now)
        protected = await self._repository.retained_digests(user_id)
        for active in await self._repository.active_uploads(user_id, now):
            protected.update(_unique_files(SkillTreeManifest.model_validate(active.manifest_json)))
        candidates: set[str] = set()
        for upload in expired:
            candidates.update(_unique_files(SkillTreeManifest.model_validate(upload.manifest_json)))
        await self._store.collect_uncommitted(
            user_id, protected, candidates, now - timedelta(hours=self._policy.staging_hours)
        )
        for upload in expired:
            _release(usage, _category(upload.scope), upload.reserved_bytes)
            upload.reserved_bytes = 0
            await self._declarations.repository.clear(upload)
            upload.status = "expired"

    def _reserve(self, usage: SkillStorageUsage, category: str, required: int) -> None:
        """
        同时验证保留空间和并发暂存空间的硬上限。

        :param usage (SkillStorageUsage): 已锁定计量状态
        :param category (str): 包或运行状态分类
        :param required (int): 本次唯一缺失对象字节数
        """
        if category == "package":
            reserved = usage.package_reserved + required
            if (
                reserved > self._policy.user_staging_bytes
                or usage.package_bytes + reserved > self._policy.user_package_bytes
            ):
                raise SkillContentError(
                    "QUOTA_EXCEEDED", "package storage or upload quota exceeded"
                )
            usage.package_reserved = reserved
        else:
            reserved = usage.state_reserved + required
            if usage.state_bytes + reserved > self._policy.user_state_bytes:
                raise SkillContentError("QUOTA_EXCEEDED", "runtime state storage quota exceeded")
            usage.state_reserved = reserved


def _category(scope: str) -> str:
    """
    将完整目录和单项运行快照映射到同一独立额度分类。

    :param scope (str): 内容范围
    :return str: 存储额度分类
    """
    return "package" if scope == "package" else "state"


def _validate_stored(
    entries: dict[str, SkillTreeEntry], stored: dict[str, SkillContentObject]
) -> None:
    """
    拒绝声明与已验证对象冲突或对正在删除的对象建立新引用。

    :param entries (dict[str, SkillTreeEntry]): 本次文件声明
    :param stored (dict[str, SkillContentObject]): 用户已有内容对象
    """
    for digest, obj in stored.items():
        entry = entries[digest]
        if obj.status != "available":
            raise SkillContentError("CONTENT_UNAVAILABLE", "content is being retired")
        if (obj.size, obj.content_kind) != (entry.size, entry.content_kind):
            raise SkillContentError(
                "CONTENT_METADATA_CONFLICT", "content metadata differs from stored object"
            )


def _require_active(upload: SkillContentUpload) -> None:
    """
    拒绝已提交、过期或已终结的上传租约。

    :param upload (SkillContentUpload): 已授权的上传记录
    """
    expires = upload.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if upload.status != "staged" or expires <= datetime.now(UTC):
        raise SkillContentError("UPLOAD_NOT_ACTIVE", "upload is no longer active")


def _release(usage: SkillStorageUsage, category: str, amount: int) -> None:
    """
    在同一事务释放预留，不改写已登记对象用量。

    :param usage (SkillStorageUsage): 已锁定的计量状态
    :param category (str): 存储额度分类
    :param amount (int): 已持有的预留字节数
    """
    if category == "package":
        usage.package_reserved -= amount
    else:
        usage.state_reserved -= amount
