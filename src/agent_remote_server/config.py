"""
实现配置模块。
"""

import ipaddress
import re
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_remote_server.ego_browser.release_policy import (
    EGO_BROWSER_LOCAL_RUNTIME_VERSION,
    EGO_BROWSER_PROTOCOL_VERSION,
    EGO_BROWSER_SKILL_COMMIT,
    EGO_BROWSER_SKILL_TREE_SHA256,
    EGO_BROWSER_SKILL_VERSION,
    EGO_BROWSER_WRAPPER_VERSION,
)

_DEFAULT_PUBLIC_BASE_URL = "http://localhost:8000"
_LOOPBACK_ENVIRONMENTS = frozenset(
    {"development", "development-local", "logic-test", "test", "testing"}
)


def canonicalize_origin(
    value: str,
    *,
    environment: str = "development",
    allow_legacy_default: bool = False,
) -> str:
    """
    将公开地址规范化为唯一的 scheme/host/port origin。

    :param value (str): 待规范化的公开地址
    :param environment (str): 当前部署环境
    :param allow_legacy_default (bool): 是否允许旧测试配置的默认 loopback HTTP 地址
    :return str: 不含路径、查询或片段的规范 origin
    :raises ValueError: 地址包含不安全或不明确的组成部分
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError("public base URL must be a non-empty string")
    raw = value.strip()
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw):
        raise ValueError("public base URL contains control characters")
    # urlsplit 接受空 ?/# 后缀，但来源标识不能允许这种等价表示。
    if "?" in raw or "#" in raw or "\\" in raw:
        raise ValueError("public base URL must not contain query, fragment, or backslash")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("public base URL has an invalid authority") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("public base URL must use http or https")
    if not parsed.netloc or parsed.path not in {"", "/"}:
        raise ValueError("public base URL must be an origin without path, query, or fragment")

    authority = parsed.netloc
    if "@" in authority or "/" in authority or any(character.isspace() for character in authority):
        raise ValueError("public base URL must not contain userinfo or an invalid authority")

    host_text: str
    port_text: str | None
    if authority.startswith("["):
        closing = authority.find("]")
        if closing <= 1 or authority.find("[", 1) != -1 or "]" in authority[closing + 1 :]:
            raise ValueError("public base URL has an invalid IPv6 authority")
        host_text = authority[1:closing]
        suffix = authority[closing + 1 :]
        if suffix and not suffix.startswith(":"):
            raise ValueError("public base URL has an invalid IPv6 authority")
        port_text = suffix[1:] if suffix else None
        if "%" in host_text:
            # Zone ID 仅在本机接口内有效；IPvFuture 也由 IPv6Address 拒绝。
            raise ValueError("public base URL must not contain an IPv6 zone")
        try:
            ipv6_value = ipaddress.IPv6Address(host_text)
        except ValueError as exc:
            raise ValueError("public base URL has an invalid IPv6 address") from exc
        hostname = ipv6_value.compressed.lower()
        is_ip_literal = True
    else:
        if "[" in authority or "]" in authority or authority.count(":") > 1:
            raise ValueError("public base URL has an invalid host")
        if ":" in authority:
            host_text, port_text = authority.rsplit(":", 1)
        else:
            host_text, port_text = authority, None
        if not host_text:
            raise ValueError("public base URL has an invalid host")
        # 主机名在校验前统一转换为确定性的 ASCII/IDNA 形式。
        host_text = host_text.rstrip(".")
        if not host_text:
            raise ValueError("public base URL has an invalid host")
        try:
            ip_value = ipaddress.ip_address(host_text)
        except ValueError:
            ip_value = None
        if ip_value is not None:
            hostname = ip_value.compressed.lower()
            is_ip_literal = True
        else:
            try:
                hostname = host_text.encode("idna").decode("ascii").lower()
            except UnicodeError as exc:
                raise ValueError("public base URL has an invalid host") from exc
            labels = hostname.split(".")
            if len(hostname) > 253 or any(
                not label
                or len(label) > 63
                or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label) is None
                for label in labels
            ):
                raise ValueError("public base URL has an invalid host")
            is_ip_literal = False

    if port_text is not None:
        if not port_text or any(character < "0" or character > "9" for character in port_text):
            raise ValueError("public base URL has an invalid port")
        port = int(port_text)
        if not 1 <= port <= 65_535:
            raise ValueError("public base URL has an invalid port")
    else:
        port = None

    is_loopback = hostname == "localhost" or (
        is_ip_literal and ipaddress.ip_address(hostname).is_loopback
    )
    if port is None or (scheme, port) in {("http", 80), ("https", 443)}:
        canonical_port = None
    else:
        canonical_port = port
    host_part = f"[{hostname}]" if is_ip_literal and ":" in hostname else hostname
    netloc = host_part if canonical_port is None else f"{host_part}:{canonical_port}"
    canonical = urlunsplit((scheme, netloc, "", "", ""))

    environment_name = environment.strip().lower()
    if scheme == "http" and (
        not is_loopback
        or (
            environment_name not in _LOOPBACK_ENVIRONMENTS
            and not (allow_legacy_default and canonical == _DEFAULT_PUBLIC_BASE_URL)
        )
    ):
        raise ValueError("http public origins are restricted to explicit loopback development")
    return canonical


class Settings(BaseSettings):
    """
    服务端运行配置
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = Field(default="agent-remote-server", description="应用名称")
    environment: str = Field(
        default="development",
        validation_alias="AGENT_REMOTE_ENV",
        description="运行环境",
    )
    public_base_url: str = Field(
        default=_DEFAULT_PUBLIC_BASE_URL,
        validation_alias="PUBLIC_BASE_URL",
        description="公开访问基础地址",
    )

    @property
    def public_origin(self) -> str:
        """
        返回不带路径的规范 scheme/host/port 来源。

        :return str: 规范化后的公开来源
        """

        allow_legacy_default = (
            "public_base_url" not in self.model_fields_set
            and self.public_base_url.strip() == _DEFAULT_PUBLIC_BASE_URL
        )
        return canonicalize_origin(
            self.public_base_url,
            environment=self.environment,
            allow_legacy_default=allow_legacy_default,
        )

    cors_allowed_origins: list[str] = Field(
        default=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        validation_alias="CORS_ALLOWED_ORIGINS",
        description="允许跨域访问的前端来源",
    )
    database_url: str = Field(
        default="postgresql+asyncpg://agent_remote:agent_remote@localhost:5432/agent_remote",
        validation_alias="DATABASE_URL",
        description="PostgreSQL 异步连接地址",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        validation_alias="REDIS_URL",
        description="Redis 连接地址",
    )
    secret_key: str = Field(
        default="dev-only-change-me",
        validation_alias="AGENT_REMOTE_SECRET_KEY",
        description="应用加密主密钥",
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL", description="日志级别")
    request_id_header: str = Field(default="x-request-id", description="请求 ID 头名称")
    dependency_check_timeout_seconds: float = Field(default=2.0, description="依赖检查超时时间")
    access_token_ttl_seconds: int = Field(default=3600, description="访问令牌有效秒数")
    device_token_ttl_seconds: int = Field(
        default=2_592_000,
        description="设备令牌有效秒数",
    )
    cli_login_ttl_seconds: int = Field(default=600, description="CLI 登录码有效秒数")
    cli_login_poll_interval_seconds: int = Field(default=5, description="CLI 登录轮询间隔秒数")
    node_task_lease_seconds: int = Field(default=30, description="节点任务租约秒数")
    node_offline_after_seconds: int = Field(default=120, description="节点离线判定秒数")
    port_forwarding_enabled: bool = Field(default=True, description="是否允许 session 端口转发")
    port_forward_min_port: int = Field(default=1024, ge=1, le=65535, description="允许的最小端口")
    port_forward_max_port: int = Field(default=65535, ge=1, le=65535, description="允许的最大端口")
    port_forward_max_per_user: int = Field(default=10, ge=1, description="单用户最大转发数")
    port_forward_max_per_device: int = Field(default=10, ge=1, description="单设备最大转发数")
    port_forward_max_per_session: int = Field(default=5, ge=1, description="单 session 最大转发数")
    port_forward_max_streams: int = Field(
        default=128, ge=1, le=1024, description="单转发最大 stream 数"
    )
    port_forward_default_ttl_seconds: int = Field(
        default=28_800, ge=60, description="端口转发默认有效秒数"
    )
    port_forward_max_ttl_seconds: int = Field(
        default=86_400, ge=60, description="端口转发最大有效秒数"
    )
    port_forward_connection_token_ttl_seconds: int = Field(
        default=60, ge=10, description="一次性连接 token 有效秒数"
    )
    port_forward_lease_seconds: int = Field(default=60, ge=10, description="Node 授权租约秒数")
    port_forward_control_plane_grace_seconds: int = Field(
        default=300, ge=0, description="控制面不可用宽限秒数"
    )
    port_forward_bytes_per_second: int = Field(
        default=0, ge=0, description="端口转发每方向带宽上限"
    )
    port_forward_cleanup_interval_seconds: int = Field(
        default=30, ge=1, description="端口转发生命周期对账间隔秒数"
    )
    port_forward_create_rate_limit_per_minute: int = Field(
        default=30, ge=1, description="单用户设备每分钟创建转发上限"
    )
    port_forward_redeem_rate_limit_per_minute: int = Field(
        default=120, ge=1, description="单 Node 设备转发每分钟兑换上限"
    )
    device_session_lease_seconds: int = Field(
        default=60, ge=10, le=300, description="设备控制短租约秒数"
    )
    device_control_enabled: bool = Field(
        default=False, description="是否在部署门禁验证完成后启用设备控制"
    )
    device_control_v2_enabled: bool = Field(
        default=True,
        validation_alias="DEVICE_CONTROL_V2_ENABLED",
        description="是否为新设备控制 generation 自动协商 Computer Use v2",
    )
    device_session_authorization_mode: Literal["per_application_approval", "session_full_trust"] = (
        Field(
            default="per_application_approval",
            validation_alias="DEVICE_SESSION_AUTHORIZATION_MODE",
            description="新设备控制会话使用的服务端授权模式",
        )
    )
    device_control_release_evidence_path: str = Field(
        default="",
        validation_alias="DEVICE_CONTROL_RELEASE_EVIDENCE_PATH",
        description="生产设备控制发布证据清单路径",
    )
    device_control_release_public_key: str = Field(
        default="",
        validation_alias="DEVICE_CONTROL_RELEASE_PUBLIC_KEY",
        description="用于验证生产发布证据的 Base64 编码 Ed25519 公钥",
    )
    device_session_max_ttl_seconds: int = Field(
        default=3600, ge=60, le=28_800, description="设备控制会话最长生命周期秒数"
    )
    device_session_retention_days: int = Field(
        default=0,
        ge=0,
        le=3_650,
        validation_alias="DEVICE_SESSION_RETENTION_DAYS",
        description="终态设备控制会话元数据保留天数，零表示不自动清理",
    )
    device_session_audit_retention_days: int = Field(
        default=0,
        ge=0,
        le=3_650,
        validation_alias="DEVICE_SESSION_AUDIT_RETENTION_DAYS",
        description="设备控制会话审计元数据保留天数，零表示不自动清理",
    )
    device_control_retention_cleanup_interval_seconds: int = Field(
        default=300,
        ge=60,
        le=86_400,
        description="设备控制元数据保留清理间隔秒数",
    )
    device_control_retention_cleanup_batch_size: int = Field(
        default=500,
        ge=1,
        le=5_000,
        description="设备控制元数据单次保留清理最大行数",
    )
    device_relay_material_ttl_seconds: int = Field(
        default=900, ge=30, le=900, description="设备中继单代连接材料最长有效秒数"
    )
    device_relay_ticket_ttl_seconds: int = Field(
        default=30, ge=5, le=120, description="设备中继一次性票据有效秒数"
    )
    device_relay_pair_timeout_seconds: int = Field(
        default=15, ge=5, le=60, description="设备中继等待对端连接秒数"
    )
    device_relay_max_frame_bytes: int = Field(
        default=1_048_576,
        ge=16_384,
        le=4_194_304,
        description="设备中继单个密文帧最大字节数",
    )
    device_relay_max_bytes_per_second: int = Field(
        default=8_388_608,
        ge=16_384,
        le=67_108_864,
        description="设备中继每个方向每秒允许的最大密文字节数",
    )
    device_relay_max_connection_seconds: int = Field(
        default=900,
        ge=30,
        le=900,
        description="设备中继配对后单次连接最长秒数",
    )
    ego_browser_bridge_enabled: bool = Field(
        default=False,
        validation_alias="EGO_BROWSER_BRIDGE_ENABLED",
        description="是否允许 ego-browser claim、relay 和远端执行",
    )
    ego_browser_enrollment_enabled: bool = Field(
        default=True,
        validation_alias="EGO_BROWSER_ENROLLMENT_ENABLED",
        description="是否允许 ego-browser 设备登记、状态查询和凭据刷新",
    )
    ego_browser_require_device_pop: bool = Field(
        default=False,
        validation_alias="EGO_BROWSER_REQUIRE_DEVICE_POP",
        description="设备注册和连接是否强制校验 Ed25519 proof-of-possession",
    )
    ego_browser_pop_challenge_ttl_seconds: int = Field(
        default=60,
        ge=10,
        le=300,
        description="ego-browser 设备 PoP 单次 challenge 有效秒数",
    )
    ego_browser_lease_seconds: int = Field(
        default=60, ge=10, le=300, description="ego-browser binding 短租约秒数"
    )
    ego_browser_lease_renew_interval_seconds: int = Field(
        default=20, ge=5, le=60, description="ego-browser binding 自动续租间隔"
    )
    ego_browser_lease_renew_failure_grace_seconds: int = Field(
        default=10, ge=1, le=60, description="ego-browser 续租失败宽限秒数"
    )
    ego_browser_lease_admission_min_remaining_seconds: int = Field(
        default=20, ge=1, le=120, description="ego-browser execute admission 最小剩余租约"
    )
    ego_browser_absolute_ttl_seconds: int = Field(
        default=28_800, ge=60, le=28_800, description="ego-browser binding 绝对 TTL"
    )
    ego_browser_max_parallel_requests: int = Field(
        default=4, ge=1, le=4, description="单个 ego-browser binding 最大并发请求数"
    )
    ego_browser_device_credential_ttl_seconds: int = Field(
        default=86_400,
        ge=300,
        le=2_592_000,
        description="独立 ego-browser Device Client 凭据有效秒数",
    )
    ego_browser_ensure_result_retention_seconds: int = Field(
        default=172_800,
        ge=86_400,
        le=2_592_000,
        validation_alias="EGO_BROWSER_ENSURE_RESULT_RETENTION_SECONDS",
        description="ensure 短期响应恢复材料的最短保留时间",
    )
    ego_browser_relay_ticket_ttl_seconds: int = Field(
        default=30, ge=5, le=120, description="ego-browser relay 一次性票据有效秒数"
    )
    ego_browser_relay_pair_timeout_seconds: int = Field(
        default=15, ge=5, le=60, description="ego-browser relay 等待对端连接秒数"
    )
    ego_browser_relay_max_frame_bytes: int = Field(
        default=16_777_216,
        ge=1_048_576,
        le=16_777_216,
        description="ego-browser outer envelope 最大字节数",
    )
    ego_browser_relay_max_bytes_per_second: int = Field(
        default=33_554_432,
        ge=1_048_576,
        le=134_217_728,
        description="ego-browser relay 每方向每秒最大密文字节数",
    )
    ego_browser_relay_max_connection_seconds: int = Field(
        default=900, ge=30, le=900, description="ego-browser relay 单次连接最长秒数"
    )
    ego_browser_cleanup_interval_seconds: int = Field(
        default=10, ge=1, le=300, description="ego-browser 租约和撤销 outbox 清理间隔"
    )
    ego_browser_cleanup_batch_size: int = Field(
        default=100, ge=1, le=1_000, description="ego-browser 后台清理单批最大记录数"
    )
    ego_browser_expected_release_profile: Literal[
        "logic-test", "development-local", "community-local-trust", "developer-id"
    ] = Field(
        default="development-local",
        validation_alias="EGO_BROWSER_EXPECTED_RELEASE_PROFILE",
        description="控制面接受的 ego-browser Bridge 发布 profile",
    )
    ego_browser_expected_signer_certificate_sha256: str = Field(
        default="",
        validation_alias="EGO_BROWSER_EXPECTED_SIGNER_CERTIFICATE_SHA256",
        description="受控发布物固定的 signer 证书 SHA-256",
    )
    ego_browser_expected_wrapper_version: str = Field(
        default=EGO_BROWSER_WRAPPER_VERSION,
        validation_alias="EGO_BROWSER_EXPECTED_WRAPPER_VERSION",
        description="Node 上受信 ego-browser wrapper 的精确版本",
    )
    ego_browser_expected_skill_version: str = Field(
        default=EGO_BROWSER_SKILL_VERSION,
        validation_alias="EGO_BROWSER_EXPECTED_SKILL_VERSION",
        description="Node 上官方 ego-browser Skill 的精确版本",
    )
    ego_browser_expected_skill_tree_sha256: str = Field(
        default=EGO_BROWSER_SKILL_TREE_SHA256,
        validation_alias="EGO_BROWSER_EXPECTED_SKILL_TREE_SHA256",
        description="Node 上官方 ego-browser Skill 完整目录的 SHA-256",
    )
    ego_browser_expected_skill_commit: str = Field(
        default=EGO_BROWSER_SKILL_COMMIT,
        validation_alias="EGO_BROWSER_EXPECTED_SKILL_COMMIT",
        description="官方 ego-browser Skill 的不可变 commit SHA",
    )
    ego_browser_expected_local_runtime_version: str = Field(
        default=EGO_BROWSER_LOCAL_RUNTIME_VERSION,
        validation_alias="EGO_BROWSER_EXPECTED_LOCAL_RUNTIME_VERSION",
        description="本机 ego-browser runtime 的精确版本",
    )
    ego_browser_expected_protocol_version: str = Field(
        default=EGO_BROWSER_PROTOCOL_VERSION,
        validation_alias="EGO_BROWSER_EXPECTED_PROTOCOL_VERSION",
        description="Server 接受的 ego-browser Bridge 协议版本",
    )
    ego_browser_expected_learning_bundle_signing_key_id: str = Field(
        default="ego-browser-learning-2026-01",
        validation_alias="EGO_BROWSER_EXPECTED_LEARNING_BUNDLE_SIGNING_KEY_ID",
        description="Server 接受的 Site Learning 签名密钥标识",
    )
    ego_browser_expected_learning_bundle_digest: str = Field(
        default="",
        validation_alias="EGO_BROWSER_EXPECTED_LEARNING_BUNDLE_SHA256",
        description="Server 接受的 Site Learning bundle SHA-256 摘要",
    )
    ego_browser_expected_distribution_version: str = Field(
        default="",
        validation_alias="EGO_BROWSER_EXPECTED_DISTRIBUTION_VERSION",
        description="Server 接受的根发行组合版本",
    )
    ego_browser_expected_root_manifest_sha256: str = Field(
        default="",
        validation_alias="EGO_BROWSER_EXPECTED_ROOT_MANIFEST_SHA256",
        description="Server 接受的根 release manifest SHA-256 摘要",
    )
    ego_browser_expected_bridge_release_manifest_sha256: str = Field(
        default="",
        validation_alias="EGO_BROWSER_EXPECTED_BRIDGE_RELEASE_MANIFEST_SHA256",
        description="Server 接受的 Bridge aggregate manifest SHA-256 摘要",
    )
    ego_browser_expected_bridge_release_archive_sha256: str = Field(
        default="",
        validation_alias="EGO_BROWSER_EXPECTED_BRIDGE_RELEASE_ARCHIVE_SHA256",
        description="Server 接受的 Bridge release archive SHA-256 摘要",
    )
    ego_browser_expected_bridge_signing_evidence_sha256: str = Field(
        default="",
        validation_alias="EGO_BROWSER_EXPECTED_BRIDGE_SIGNING_EVIDENCE_SHA256",
        description="Server 接受的 Bridge signing evidence SHA-256 摘要",
    )
    ego_browser_expected_bridge_sigstore_sha256: str = Field(
        default="",
        validation_alias="EGO_BROWSER_EXPECTED_BRIDGE_SIGSTORE_SHA256",
        description="Server 接受的 Bridge Sigstore evidence SHA-256 摘要",
    )
    ego_browser_expected_bridge_provenance_sha256: str = Field(
        default="",
        validation_alias="EGO_BROWSER_EXPECTED_BRIDGE_PROVENANCE_SHA256",
        description="Server 接受的 Bridge provenance SHA-256 摘要",
    )

    @model_validator(mode="after")
    def validate_coherent_policy(self) -> "Settings":
        """
        校验跨字段部署策略中的关联边界

        :return "Settings": 已通过关联边界校验的应用配置
        :raises ValueError: 部署策略关联边界不合法，如"端口范围、TTL、设备中继速率或保留期配置冲突"
        """

        # 仅隔离开发测试可沿用缺省地址；显式生产或社区地址始终严格校验。
        allow_legacy_default = (
            "public_base_url" not in self.model_fields_set
            and self.public_base_url.strip() == _DEFAULT_PUBLIC_BASE_URL
        )
        canonicalize_origin(
            self.public_base_url,
            environment=self.environment,
            allow_legacy_default=allow_legacy_default,
        )
        if self.port_forward_min_port > self.port_forward_max_port:
            raise ValueError("port forward minimum port must not exceed maximum port")
        if self.port_forward_default_ttl_seconds > self.port_forward_max_ttl_seconds:
            raise ValueError("port forward default TTL must not exceed maximum TTL")
        if self.device_relay_max_bytes_per_second < self.device_relay_max_frame_bytes:
            raise ValueError("device relay byte rate must not be smaller than one frame")
        if self.ego_browser_relay_max_bytes_per_second < self.ego_browser_relay_max_frame_bytes:
            raise ValueError("ego-browser relay byte rate must not be smaller than one frame")
        if self.ego_browser_lease_admission_min_remaining_seconds >= self.ego_browser_lease_seconds:
            raise ValueError("ego-browser admission window must be shorter than its lease")
        if self.ego_browser_lease_renew_interval_seconds >= self.ego_browser_lease_seconds:
            raise ValueError("ego-browser renewal interval must be shorter than its lease")
        if (
            self.device_session_audit_retention_days > 0
            and self.device_session_audit_retention_days < self.device_session_retention_days
        ):
            raise ValueError(
                "device session audit retention must not be shorter than session retention"
            )
        if (
            self.environment.strip().lower() == "production"
            and self.device_control_enabled
            and (
                self.device_session_retention_days == 0
                or self.device_session_audit_retention_days == 0
            )
        ):
            raise ValueError("production device control requires explicit metadata retention")
        if self.environment.strip().lower() == "production" and self.ego_browser_bridge_enabled:
            if not self.ego_browser_require_device_pop:
                raise ValueError(
                    "production ego-browser bridge requires device proof-of-possession"
                )
            if self.ego_browser_expected_release_profile not in {
                "community-local-trust",
                "developer-id",
            }:
                raise ValueError(
                    "production ego-browser bridge requires a production release profile"
                )
            digest = self.ego_browser_expected_signer_certificate_sha256
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError(
                    "production ego-browser bridge requires a pinned signer certificate"
                )
        for version in (
            self.ego_browser_expected_wrapper_version,
            self.ego_browser_expected_skill_version,
            self.ego_browser_expected_local_runtime_version,
        ):
            if (
                not version
                or len(version) > 64
                or any(
                    character
                    not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-"
                    for character in version
                )
            ):
                raise ValueError("ego-browser expected versions are invalid")
        skill_digest = self.ego_browser_expected_skill_tree_sha256
        if len(skill_digest) != 64 or any(
            character not in "0123456789abcdef" for character in skill_digest
        ):
            raise ValueError("ego-browser expected Skill digest is invalid")
        skill_commit = self.ego_browser_expected_skill_commit
        if len(skill_commit) != 40 or any(
            character not in "0123456789abcdef" for character in skill_commit
        ):
            raise ValueError("ego-browser expected Skill commit is invalid")
        protocol = self.ego_browser_expected_protocol_version
        if (
            not protocol
            or len(protocol) > 64
            or any(
                character
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-"
                for character in protocol
            )
        ):
            raise ValueError("ego-browser expected protocol version is invalid")
        key_id = self.ego_browser_expected_learning_bundle_signing_key_id
        if (
            not key_id
            or len(key_id) > 128
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                for character in key_id
            )
        ):
            raise ValueError("ego-browser expected learning key ID is invalid")
        if (
            self.ego_browser_expected_distribution_version
            and re.fullmatch(
                r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-.+][0-9A-Za-z.-]+)?$",
                self.ego_browser_expected_distribution_version,
            )
            is None
        ):
            raise ValueError("ego-browser expected distribution version is invalid")
        for digest in (
            self.ego_browser_expected_signer_certificate_sha256,
            self.ego_browser_expected_learning_bundle_digest,
            self.ego_browser_expected_root_manifest_sha256,
            self.ego_browser_expected_bridge_release_manifest_sha256,
            self.ego_browser_expected_bridge_release_archive_sha256,
            self.ego_browser_expected_bridge_signing_evidence_sha256,
            self.ego_browser_expected_bridge_sigstore_sha256,
            self.ego_browser_expected_bridge_provenance_sha256,
        ):
            if digest and re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError("ego-browser expected release digest is invalid")
        return self


@lru_cache
def get_settings() -> Settings:
    """
    获取缓存后的应用配置

    :return Settings: 应用配置实例
    """

    return Settings()
