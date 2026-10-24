"""
计算跨语言稳定的审核意图摘要，不把原始配置写入额外凭据。
"""

import base64
import binascii
import hashlib
import json
from uuid import UUID

from agent_remote_server.errors import ApiError
from agent_remote_server.schemas.config_import_requests import (
    ConfigImportManifestEntry,
    ConfigImportRequest,
)
from agent_remote_server.services.tool_accounts import (
    CONFIG_IMPORT_MAX_FILE_BYTES,
    CONFIG_IMPORT_MAX_TOTAL_BYTES,
)


def invalid_import() -> ApiError:
    """
    错误只返回固定文本，不能回显路径或文件正文。

    :return ApiError: 有界配置导入校验错误
    """
    return ApiError(
        code="COMMON_VALIDATION_ERROR", message="Config import request is invalid.", status_code=422
    )


def manifest(payload: ConfigImportRequest) -> list[ConfigImportManifestEntry]:
    """
    检查真实解码尺寸并按规范路径建立唯一文件清单。

    :param payload (ConfigImportRequest): 严格校验过 JSON 类型的请求
    :return list[ConfigImportManifestEntry]: 按路径排序的无内容清单
    """
    entries: list[ConfigImportManifestEntry] = []
    paths: set[str] = set()
    size = 0
    for file in sorted(payload.files, key=lambda file: file.path):
        if file.path in paths:
            raise invalid_import()
        paths.add(file.path)
        try:
            content = base64.b64decode(file.content_base64, validate=True)
        except (ValueError, binascii.Error) as error:
            raise invalid_import() from error
        size += len(content)
        if len(content) > CONFIG_IMPORT_MAX_FILE_BYTES or size > CONFIG_IMPORT_MAX_TOTAL_BYTES:
            raise invalid_import()
        entries.append(
            ConfigImportManifestEntry(
                path=file.path,
                sha256="sha256:" + hashlib.sha256(content).hexdigest(),
                size=len(content),
                mode=file.mode,
            )
        )
    return entries


def intent_fingerprint(
    account_id: UUID, payload: ConfigImportRequest, entries: list[ConfigImportManifestEntry]
) -> str:
    """
    严格按公开协议序列化非秘密意图和内容身份，包含路径列表原始顺序。

    :param account_id (UUID): 已授权目标账户身份
    :param payload (ConfigImportRequest): 原始严格请求
    :param entries (list[ConfigImportManifestEntry]): 排序后的原始文件内容身份
    :return str: 带算法前缀的稳定摘要
    """
    value = {
        "schema_version": 1,
        "tool_account_id": str(account_id),
        "tool_type": payload.tool_type,
        "source": payload.source,
        "include": payload.include,
        "exclude": payload.exclude,
        "include_resume_history": payload.include_resume_history,
        "dry_run": False,
        "files": [entry.model_dump() for entry in entries],
    }
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    except UnicodeError as error:
        raise invalid_import() from error
    return "sha256:" + hashlib.sha256(data).hexdigest()
