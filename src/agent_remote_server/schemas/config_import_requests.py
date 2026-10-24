"""
定义不可宽松转换的配置导入意图、无内容清单及原始受理凭据。
"""

import unicodedata
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator


def canonical_uuid(value: str) -> str:
    """
    仅接受标准小写带连字符的 UUID 文本。

    :param value (str): 原始身份文本
    :return str: 未修改的规范身份
    """
    if str(UUID(value)) != value:
        raise ValueError("UUID must use canonical lowercase text")
    return value


def canonical_path(value: str) -> str:
    """
    拒绝会改变审核范围的别名、路径折叠和不可编码文本。

    :param value (str): 原始配置路径
    :return str: 未修改的规范路径
    """
    if (
        not value.startswith("~/")
        or value.strip() != value
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(unicodedata.category(c) == "Cc" or 0xD800 <= ord(c) <= 0xDFFF for c in value)
    ):
        raise ValueError("configuration path must be canonical")
    return value


type CanonicalUuid = Annotated[
    str, Field(min_length=36, max_length=36), AfterValidator(canonical_uuid)
]
type ConfigPath = Annotated[
    str, Field(min_length=3, max_length=2048), AfterValidator(canonical_path)
]
type Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
type WarningCode = Literal[
    "CONFIG_IMPORT_EXECUTABLE_REVIEW_REQUIRED", "CONFIG_IMPORT_CUSTOM_MARKDOWN_REVIEW_REQUIRED"
]


class StrictImportModel(BaseModel):
    """
    禁止未知字段及隐式 JSON 类型转换。
    """

    model_config = ConfigDict(extra="forbid", strict=True)


class ConfigImportFile(StrictImportModel):
    """
    提交给原节点任务的单个配置文件。
    """

    path: ConfigPath = Field(..., description="规范配置路径")
    content_base64: str = Field(..., max_length=1398104, description="原文件内容的标准 base64")
    mode: int = Field(..., ge=0, le=0o777, description="不含特殊位的文件权限")


class ConfigImportManifestEntry(StrictImportModel):
    """
    可独立复核但不包含原内容的单文件清单。
    """

    path: ConfigPath = Field(..., description="规范配置路径")
    sha256: Digest = Field(..., description="解码后内容的 SHA256 身份")
    size: int = Field(..., ge=0, le=1024 * 1024, description="解码后字节数")
    mode: int = Field(..., ge=0, le=0o777, description="原始审核文件权限")


class ConfigImportRequest(StrictImportModel):
    """
    严格版本的调用方键配置导入请求。
    """

    schema_version: Literal[1] = Field(..., description="精确整数协议版本")
    request_id: CanonicalUuid = Field(..., description="调用方生成的原始请求身份")
    expected_intent_fingerprint: Digest = Field(..., description="调用方独立计算的审核意图身份")
    tool_type: str = Field(..., min_length=1, max_length=32, description="目标账户工具类型")
    source: Literal["local_cli"] = Field(..., description="共享 CLI 配置导入来源")
    include: list[ConfigPath] = Field(..., max_length=4096, description="保留顺序的审核导入范围")
    exclude: list[ConfigPath] = Field(..., max_length=4096, description="保留顺序的审核排除范围")
    files: list[ConfigImportFile] = Field(
        ..., min_length=1, max_length=4096, description="审核文件集合"
    )
    include_resume_history: bool = Field(..., description="是否明确审核恢复历史")
    dry_run: Literal[False] = Field(..., description="强版本仅接受明确的实际导入")

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        """
        避免 Python 布尔及浮点值与整数一相等导致版本绕过。

        :param value (object): 原始 JSON 版本
        :return object: 类型正确的原始版本
        """
        if type(value) is not int:
            raise ValueError("schema version must be an integer")
        return value

    @field_validator("dry_run", mode="before")
    @classmethod
    def boolean_execution(cls, value: object) -> object:
        """
        拒绝数值零冒充用户明确的非预览操作。

        :param value (object): 原始 JSON 预览标志
        :return object: 类型正确的原始标志
        """
        if type(value) is not bool:
            raise ValueError("dry_run must be a boolean")
        return value


class ConfigImportReceipt(StrictImportModel):
    """
    原始受理凭据不随任务状态变化，所有身份和清单字段均不可为空。
    """

    schema_version: Literal[1] = Field(..., description="受理协议版本")
    request_id: CanonicalUuid = Field(..., description="原始调用方请求身份")
    tool_account_id: CanonicalUuid = Field(..., description="原始目标账户身份")
    tool_type: str = Field(..., description="原始目标工具类型")
    source: Literal["local_cli"] = Field(..., description="原始配置来源")
    include: list[ConfigPath] = Field(..., description="原始审核导入范围")
    exclude: list[ConfigPath] = Field(..., description="原始审核排除范围")
    include_resume_history: bool = Field(..., description="原始恢复历史授权")
    dry_run: Literal[False] = Field(..., description="已受理实际导入而非任务完成")
    manifest: list[ConfigImportManifestEntry] = Field(..., description="原始无内容文件清单")
    intent_fingerprint: Digest = Field(..., description="可独立重算的原始意图身份")
    task_id: str = Field(..., description="精确原始节点任务身份")
    accepted: list[ConfigPath] = Field(..., description="全部已审核并接受的导入路径")
    rejected: list[ConfigPath] = Field(..., description="强版本受理时始终为空的拒绝路径")
    warnings: list[WarningCode] = Field(..., description="固定且不含原始内容的审核警告码")
    account_remote_path: str = Field(..., description="受理时绑定的远端账户路径")
    file_count: int = Field(..., ge=1, le=4096, description="原始派发文件数量")


class ConfigImportReceiptResponse(BaseModel):
    """
    包装不可变受理凭据及独立 HTTP 跟踪身份。
    """

    data: ConfigImportReceipt = Field(..., description="原始导入受理凭据")
    request_id: str | None = Field(default=None, description="当前 HTTP 请求跟踪身份")
