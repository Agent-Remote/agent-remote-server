import base64
import binascii
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_remote_server import __version__

_SHA256_HEX_LENGTH = 64
_MAXIMUM_MANIFEST_BYTES = 65_536
_MAXIMUM_EVIDENCE_LIFETIME_DAYS = 30


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """
    构造 JSON 对象并拒绝重复字段

    :param pairs (list[tuple[str, object]]): 按输入顺序解析的字段和值

    :return dict[str, object]: 不含重复字段的 JSON 对象

    :raises ValueError: JSON 对象包含重复字段
    """

    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate release evidence field: {key}")
        value[key] = item
    return value


def _read_manifest(path: Path) -> bytes:
    """
    通过单一文件描述符读取受限大小的普通清单文件

    :param path (Path): 发布证据清单路径

    :return bytes: 发布证据清单原始内容

    :raises OSError: 文件无法安全打开或读取
    :raises ValueError: 文件类型或大小不符合要求
    """

    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("release evidence path is not a regular file")
        if info.st_size <= 0 or info.st_size > _MAXIMUM_MANIFEST_BYTES:
            raise ValueError("release evidence manifest size is invalid")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            raw_manifest = source.read(_MAXIMUM_MANIFEST_BYTES + 1)
        if not raw_manifest or len(raw_manifest) > _MAXIMUM_MANIFEST_BYTES:
            raise ValueError("release evidence manifest size is invalid")
        return raw_manifest
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class DeviceControlReleaseEvidenceError(ValueError):
    """
    表示设备控制发布证据未通过生产启用校验
    """


class DeviceControlReleaseEvidence(BaseModel):
    """
    设备控制生产发布证据清单
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(..., description="发布证据清单格式版本")
    release_profile: Literal["apple-developer-id", "community-local-trust"] = Field(
        default="apple-developer-id", description="设备应用发布信任配置"
    )
    production_ready: bool = Field(default=True, description="是否通过所选配置的生产门禁")
    apple_notarized: bool = Field(default=True, description="设备应用是否已通过 Apple 公证")
    public_distribution: bool = Field(default=True, description="是否支持无人工信任的公开分发")
    manual_trust_required: bool = Field(
        default=False, description="安装时是否需要管理员人工建立信任"
    )
    release_version: str = Field(..., description="证据绑定的服务端发布版本")
    issued_at: datetime = Field(..., description="发布证据签发时间")
    expires_at: datetime = Field(..., description="发布证据失效时间")
    server_sha256: str = Field(..., description="服务端发布制品摘要")
    node_sha256: str = Field(..., description="Node 发布制品摘要")
    application_sha256: str = Field(..., description="所选发布配置验证后的 macOS 应用制品摘要")
    proxy_sha256: str = Field(..., description="远端 MCP 代理制品摘要")
    sbom_sha256: str = Field(..., description="软件物料清单摘要")
    provenance_sha256: str = Field(..., description="构建来源证明摘要")
    security_tests_sha256: str | None = Field(
        default=None, description="Apple 配置的安全测试与跨租户端到端测试证据摘要"
    )
    security_review_sha256: str | None = Field(
        default=None, description="Apple 配置的独立安全评审与复测证据摘要"
    )
    signing_notarization_sha256: str = Field(
        ..., description="严格签名公证或 Community 自签名证据摘要"
    )
    outbound_policy_sha256: str | None = Field(
        default=None, description="Apple 配置的系统出站允许列表激活证据摘要"
    )
    local_claude_isolation_sha256: str | None = Field(
        default=None, description="Apple 配置的本地 Claude 文件与网络隔离证据摘要"
    )
    stop_revocation_sha256: str | None = Field(
        default=None, description="Apple 配置的全局停止、撤销和故障关闭演练证据摘要"
    )
    compatibility_sha256: str | None = Field(
        default=None, description="Apple 配置的当前 Claude Code 与 MCP 兼容性证据摘要"
    )
    community_signing_sha256: str | None = Field(
        default=None, description="Community 自签名身份与嵌套签名证据摘要"
    )
    automated_release_checks_sha256: str | None = Field(
        default=None, description="Community 自动化发布检查汇总证据摘要"
    )
    risk_acceptance_sha256: str | None = Field(
        default=None, description="部署方接受 Community 剩余风险的记录摘要"
    )
    ci_run_url: str = Field(..., description="生成发布证据的持续集成运行地址")
    signature: str = Field(..., description="清单规范载荷的 Ed25519 签名")

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        """
        校验发布证据清单格式版本

        :param value (int): 待校验的格式版本

        :return int: 已通过校验的格式版本

        :raises ValueError: 格式版本不受支持
        """

        if value not in {1, 2}:
            raise ValueError("unsupported device control release evidence schema version")
        return value

    @model_validator(mode="after")
    def validate_release_profile(self) -> "DeviceControlReleaseEvidence":
        """
        校验发布配置与 Apple 信任状态不存在矛盾

        :return DeviceControlReleaseEvidence: 已通过配置一致性校验的发布证据

        :raises ValueError: 配置版本或信任声明不符合固定契约
        """

        strict_gate_digests = (
            self.security_tests_sha256,
            self.security_review_sha256,
            self.outbound_policy_sha256,
            self.local_claude_isolation_sha256,
            self.stop_revocation_sha256,
            self.compatibility_sha256,
        )
        if not self.production_ready:
            raise ValueError("device control release evidence is not production ready")
        if self.schema_version == 1:
            if (
                self.release_profile != "apple-developer-id"
                or not self.apple_notarized
                or not self.public_distribution
                or self.manual_trust_required
                or self.community_signing_sha256 is not None
                or self.automated_release_checks_sha256 is not None
                or self.risk_acceptance_sha256 is not None
                or any(digest is None for digest in strict_gate_digests)
            ):
                raise ValueError("schema version 1 requires the Apple release profile")
            return self
        if (
            self.release_profile != "community-local-trust"
            or self.apple_notarized
            or self.public_distribution
            or not self.manual_trust_required
            or self.community_signing_sha256 is None
            or self.automated_release_checks_sha256 is None
            or self.risk_acceptance_sha256 is None
            or any(digest is not None for digest in strict_gate_digests)
        ):
            raise ValueError("schema version 2 requires the community local-trust profile")
        return self

    @field_validator(
        "application_sha256",
        "server_sha256",
        "node_sha256",
        "proxy_sha256",
        "sbom_sha256",
        "provenance_sha256",
        "security_tests_sha256",
        "security_review_sha256",
        "signing_notarization_sha256",
        "outbound_policy_sha256",
        "local_claude_isolation_sha256",
        "stop_revocation_sha256",
        "compatibility_sha256",
        "community_signing_sha256",
        "automated_release_checks_sha256",
        "risk_acceptance_sha256",
    )
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        """
        校验证据摘要使用小写 SHA-256 十六进制格式

        :param value (str): 待校验的证据摘要

        :return str: 已通过校验的证据摘要

        :raises ValueError: 摘要格式不符合要求
        """

        if value is None:
            return None
        if len(value) != _SHA256_HEX_LENGTH or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("release evidence digest must be lowercase SHA-256 hexadecimal")
        return value

    @field_validator("release_version", "ci_run_url", "signature")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        """
        校验发布证据文本字段不为空且没有首尾空白

        :param value (str): 待校验的文本

        :return str: 已通过校验的文本

        :raises ValueError: 文本为空或包含首尾空白
        """

        if not value or value != value.strip():
            raise ValueError(
                "release evidence text must be non-empty without surrounding whitespace"
            )
        return value

    @field_validator("ci_run_url")
    @classmethod
    def validate_ci_run_url(cls, value: str) -> str:
        """
        校验持续集成证据使用 HTTPS 地址

        :param value (str): 待校验的持续集成运行地址

        :return str: 已通过校验的持续集成运行地址

        :raises ValueError: 地址未使用 HTTPS
        """

        if not value.startswith("https://"):
            raise ValueError("release evidence CI run URL must use HTTPS")
        return value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def validate_timestamp_timezone(cls, value: datetime) -> datetime:
        """
        校验发布证据时间包含时区

        :param value (datetime): 待校验的时间

        :return datetime: 已通过校验的时间

        :raises ValueError: 时间不包含时区
        """

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("release evidence timestamp must include a timezone")
        return value

    @classmethod
    def load(cls, path: Path) -> Self:
        """
        从受限大小的 JSON 文件加载发布证据清单

        :param path (Path): 发布证据清单路径

        :return Self: 已完成结构校验的发布证据清单

        :raises DeviceControlReleaseEvidenceError: 文件不可读、过大或内容无效
        """

        try:
            raw_manifest = _read_manifest(path)
            value = json.loads(
                raw_manifest,
                object_pairs_hook=_reject_duplicate_keys,
            )
            manifest = cls.model_validate(value)
        except DeviceControlReleaseEvidenceError:
            raise
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise DeviceControlReleaseEvidenceError(
                "device control release evidence could not be loaded"
            ) from exc
        return manifest

    def signing_payload(self) -> bytes:
        """
        生成签名覆盖的规范 JSON 载荷

        :return bytes: 不含签名字段的 UTF-8 规范 JSON
        """

        return self._canonical_json(exclude_signature=True)

    def encoded_manifest(self) -> bytes:
        """
        生成包含签名的规范 JSON 清单

        :return bytes: 包含签名字段的 UTF-8 规范 JSON
        """

        return self._canonical_json(exclude_signature=False)

    def _canonical_json(self, *, exclude_signature: bool) -> bytes:
        payload = self.model_dump(
            mode="json",
            exclude={"signature"} if exclude_signature else None,
        )
        return json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def verify_device_control_release_evidence(
    *,
    evidence_path: str,
    public_key_base64: str,
    now: datetime | None = None,
) -> DeviceControlReleaseEvidence:
    """
    验证生产设备控制发布证据的签名、版本和有效期

    :param evidence_path (str): 发布证据清单路径
    :param public_key_base64 (str): Base64 编码的 Ed25519 原始公钥
    :param now (datetime): 可选的当前时间，供确定性验证使用

    :return DeviceControlReleaseEvidence: 已通过全部校验的发布证据

    :raises DeviceControlReleaseEvidenceError: 配置缺失、签名无效、版本不符或证据已过期
    """

    if not evidence_path or not public_key_base64:
        raise DeviceControlReleaseEvidenceError(
            "production device control requires release evidence and a pinned public key"
        )
    manifest = DeviceControlReleaseEvidence.load(Path(evidence_path))
    if manifest.release_version != __version__:
        raise DeviceControlReleaseEvidenceError(
            "device control release evidence does not match the server version"
        )
    try:
        public_key_bytes = base64.b64decode(public_key_base64, validate=True)
        signature = base64.b64decode(manifest.signature, validate=True)
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature, manifest.signing_payload())
    except (binascii.Error, ValueError, InvalidSignature) as exc:
        raise DeviceControlReleaseEvidenceError(
            "device control release evidence signature is invalid"
        ) from exc
    verification_time = now or datetime.now(UTC)
    if verification_time.tzinfo is None or verification_time.utcoffset() is None:
        raise DeviceControlReleaseEvidenceError("release evidence verification time must be aware")
    if manifest.issued_at > verification_time:
        raise DeviceControlReleaseEvidenceError("device control release evidence is not yet valid")
    maximum_expiry = manifest.issued_at + timedelta(days=_MAXIMUM_EVIDENCE_LIFETIME_DAYS)
    if manifest.expires_at <= manifest.issued_at or manifest.expires_at > maximum_expiry:
        raise DeviceControlReleaseEvidenceError(
            "device control release evidence lifetime is invalid"
        )
    if manifest.expires_at <= verification_time:
        raise DeviceControlReleaseEvidenceError("device control release evidence has expired")
    return manifest


def ensure_device_control_release_evidence_current(
    *,
    environment: str,
    enabled: bool,
    evidence: DeviceControlReleaseEvidence | None,
    now: datetime | None = None,
) -> None:
    """
    在运行期间确认生产设备控制仍有当前有效的发布证据

    :param environment (str): 当前部署环境
    :param enabled (bool): 是否配置启用设备控制
    :param evidence (DeviceControlReleaseEvidence): 启动时已验证的发布证据
    :param now (datetime): 可选的当前时间，供确定性验证使用

    :raises DeviceControlReleaseEvidenceError: 生产证据缺失、尚未生效或已经过期
    """

    if environment.strip().lower() != "production" or not enabled:
        return
    if evidence is None:
        raise DeviceControlReleaseEvidenceError(
            "production device control requires current release evidence"
        )
    verification_time = now or datetime.now(UTC)
    if verification_time.tzinfo is None or verification_time.utcoffset() is None:
        raise DeviceControlReleaseEvidenceError("release evidence verification time must be aware")
    if evidence.issued_at > verification_time:
        raise DeviceControlReleaseEvidenceError("device control release evidence is not yet valid")
    if evidence.expires_at <= verification_time:
        raise DeviceControlReleaseEvidenceError("device control release evidence has expired")
