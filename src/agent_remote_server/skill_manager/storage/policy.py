"""
定义发布包和运行状态互不混用的可配置存储额度。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_remote_server.schemas.skill_manifest import SkillTreeManifest

type ContentScope = Literal["package", "state", "account_directory"]
_MIB = 1024**2
_GIB = 1024**3


class SkillStoragePolicy(BaseModel):
    """
    设计约定的初始配额和历史保留期。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    package_file_bytes: int = Field(default=10 * _MIB, gt=0, description="发布包单文件字节上限")
    package_bytes: int = Field(default=50 * _MIB, gt=0, description="单个发布包展开字节上限")
    package_entries: int = Field(default=5_000, gt=0, le=100_000, description="发布包条目上限")
    checkpoint_bytes: int = Field(default=_GIB, gt=0, description="单项运行快照展开字节上限")
    directory_bytes: int = Field(default=10 * _GIB, gt=0, description="完整账户目录展开字节上限")
    state_entries: int = Field(default=100_000, gt=0, le=100_000, description="运行快照条目上限")
    user_package_bytes: int = Field(
        default=2 * _GIB, gt=0, description="单用户保留原始对象字节上限"
    )
    user_staging_bytes: int = Field(default=2 * _GIB, gt=0, description="单用户包上传暂存字节上限")
    user_state_bytes: int = Field(default=20 * _GIB, gt=0, description="单用户运行对象字节上限")
    history_days: int = Field(default=30, gt=0, description="无引用普通历史保留天数")
    archive_days: int = Field(default=90, gt=0, description="无引用卸载历史保留天数")
    staging_hours: int = Field(default=24, gt=0, description="无活动租约暂存保留小时数")

    def validate_manifest(self, manifest: SkillTreeManifest, scope: ContentScope) -> None:
        """
        在开始上传前检查完整树的展开额度及安装包链接约束。

        :param manifest (SkillTreeManifest): 完整且结构有效的清单
        :param scope (ContentScope): 内容所属范围
        :raises ValueError: 内容超额或发布包包含运行依赖
        """
        if scope == "package":
            byte_limit = self.package_bytes
            entry_limit = self.package_entries
            if any(entry.kind == "runtime_link" for entry in manifest.entries):
                raise ValueError("installation package cannot include runtime dependencies")
            if any(entry.size > self.package_file_bytes for entry in manifest.entries):
                raise ValueError("package file quota exceeded")
        else:
            byte_limit = (
                self.directory_bytes if scope == "account_directory" else self.checkpoint_bytes
            )
            entry_limit = self.state_entries
        if manifest.total_bytes > byte_limit or len(manifest.entries) > entry_limit:
            raise ValueError("skill tree quota exceeded")
