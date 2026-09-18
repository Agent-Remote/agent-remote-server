"""
定义Ego Browser接口模型。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_remote_server.ego_browser.release_policy import EGO_BROWSER_PROTOCOL_VERSION

EgoBrowserAuthorizationMode = Literal["ego_browser_script_full_trust"]
EgoBrowserProtocol = Literal["ego-browser-bridge-v1"]
EgoBrowserReleaseProfile = Literal[
    "logic-test", "development-local", "community-local-trust", "developer-id"
]
EgoBrowserCredentialProfile = Literal["community_file", "keychain_access_group"]
EgoBrowserBindingStatus = Literal[
    "pending_device",
    "connecting",
    "probing_local_browser",
    "active",
    "paused",
    "stopping",
    "stopped",
    "expired",
    "failed",
    "revoked",
]
EgoBrowserDeviceStatus = Literal["active", "retiring", "revoked"]
EgoBrowserLeaseHealth = Literal["healthy", "renewal_grace", "expired"]
EgoBrowserConcurrencyMode = Literal["task_space_tab", "task_space", "binding"]
EgoBrowserRelayRole = Literal["bridge", "wrapper"]
EgoBrowserRequestStatus = Literal[
    "accepted", "cancel_requested", "cancelled", "completed", "rejected"
]
EgoBrowserProofOperation = Literal[
    "register_device",
    "device_rotate",
    "claim_binding",
    "connect_binding",
    "renew_binding",
    "pause_binding",
    "resume_binding",
    "stop_binding",
    "revoke_binding",
    "revoke_device",
    "issue_relay_ticket",
    "confirm_allowlist",
]
EgoBrowserEnrollmentMode = Literal["initial", "ensure", "re_enroll", "rotate"]


class _Schema(BaseModel):
    """
    定义模型。
    """

    model_config = ConfigDict(extra="forbid")


class EgoBrowserProofChallengeRequest(_Schema):
    """
    申请一次性设备所有权证明挑战。
    """

    operation: EgoBrowserProofOperation = Field(..., description="即将签署的设备操作")
    ego_browser_device_id: UUID = Field(..., description="签署操作的独立设备 ID")
    generation: int = Field(default=1, ge=1, description="即将签署的操作代次（兼容字段）")
    device_generation: int | None = Field(
        default=None, ge=1, description="签名设备身份代次；与 binding 代次分开"
    )
    operation_generation: int | None = Field(
        default=None, ge=1, description="即将签署的 binding 或设备操作代次"
    )
    binding_id: UUID | None = Field(default=None, description="操作绑定的浏览器绑定 ID")

    @model_validator(mode="after")
    def normalize_explicit_generations(self) -> "EgoBrowserProofChallengeRequest":
        """
        兼容旧 generation，同时保留设备与操作代次的明确命名。

        :return "EgoBrowserProofChallengeRequest": 规范化后的挑战请求
        """

        if self.operation_generation is not None:
            if self.generation != 1 and self.generation != self.operation_generation:
                raise ValueError("generation and operation_generation disagree")
            self.generation = self.operation_generation
        return self


class EgoBrowserProofChallengeData(_Schema):
    """
    一次性设备所有权证明挑战。
    """

    challenge: str = Field(..., min_length=43, max_length=64, description="单次随机挑战值")
    expires_at: datetime = Field(..., description="挑战过期时间")
    device_generation: int | None = Field(default=None, ge=1, description="签名设备身份代次")
    operation_generation: int | None = Field(default=None, ge=1, description="本次操作代次")


class EgoBrowserProofChallengeResponse(_Schema):
    """
    一次性设备所有权证明挑战响应。
    """

    data: EgoBrowserProofChallengeData = Field(..., description="挑战数据")
    request_id: str | None = Field(default=None, description="请求追踪 ID")


class EgoBrowserDeviceRegisterRequest(_Schema):
    """
    注册或轮换独立 ego-browser 设备的请求。
    """

    device_id: UUID = Field(..., description="待注册或轮换的独立设备 ID")
    public_key: str = Field(
        ..., min_length=40, max_length=64, description="用于验证设备 PoP 签名的 Ed25519 公钥"
    )
    encryption_public_key: str | None = Field(
        default=None, min_length=40, max_length=64, description="独立 X25519 加密公钥"
    )
    signing_public_key: str | None = Field(
        default=None, min_length=40, max_length=64, description="签名公钥的明确兼容字段"
    )
    generation: int = Field(default=1, ge=1, description="待注册或轮换的设备代次")
    device_generation: int | None = Field(
        default=None, ge=1, description="独立设备身份代次；优先于旧 generation 字段"
    )
    enrollment_mode: EgoBrowserEnrollmentMode = Field(
        default="initial",
        description="设备登记意图：首次登记、幂等 ensure、显式重新加入或密钥轮换",
    )
    release_profile: EgoBrowserReleaseProfile = Field(
        ..., description="Bridge 构建与签名发布配置档案"
    )
    credential_profile: EgoBrowserCredentialProfile = Field(..., description="设备凭据存储配置档案")
    platform: Literal["macos"] = Field(..., description="独立设备支持的本地平台")
    bridge_protocol_version: str = Field(
        default=EGO_BROWSER_PROTOCOL_VERSION,
        min_length=1,
        max_length=64,
        description="Bridge 协议版本",
    )
    bridge_version: str | None = Field(default=None, max_length=64, description="Bridge 客户端版本")
    local_ego_browser_runtime_version: str | None = Field(
        default=None, max_length=64, description="本地 ego-browser 运行时版本"
    )
    ego_lite_runtime_version: str | None = Field(
        default=None, max_length=64, description="本地 ego-lite 运行时版本"
    )
    skill_version: str | None = Field(
        default=None, max_length=64, description="调用 Bridge 的自动化技能版本"
    )
    signer_certificate_sha256: str = Field(
        default="development", max_length=64, description="Bridge 签名证书 SHA-256 摘要"
    )
    allowlist_revision: int = Field(default=1, ge=1, description="文件允许列表修订版本")
    allowlist_roots_digest: str | None = Field(
        default=None, max_length=128, description="规范化允许列表根摘要"
    )
    learning_bundle_digest: str | None = Field(
        default=None, max_length=80, description="学习软件包内容摘要"
    )
    capabilities: list[str] = Field(
        default_factory=list, max_length=32, description="经策略校验的 Bridge 能力列表"
    )
    policy_digest: str | None = Field(default=None, max_length=128, description="本地策略摘要")
    capability_digest: str | None = Field(default=None, max_length=128, description="能力集合摘要")
    proof_challenge: str | None = Field(
        default=None, max_length=512, description="一次性设备 PoP 挑战值"
    )
    proof_signature: str | None = Field(
        default=None, max_length=512, description="设备对 PoP 挑战值的签名"
    )

    @field_validator("public_key", "encryption_public_key", "proof_challenge", "proof_signature")
    @classmethod
    def no_control_chars(cls, value: str | None) -> str | None:
        """
        拒绝凭据字段中的控制字符。

        :param value (str | None): 待校验或规范化的值
        :return str | None: 通过控制字符校验的原值
        :raises ValueError: 值包含控制字符
        """
        if value is not None and any(ord(char) < 0x20 for char in value):
            raise ValueError("value contains control characters")
        return value

    @model_validator(mode="after")
    def require_complete_proof_pair(self) -> "EgoBrowserDeviceRegisterRequest":
        """
        确保所有权证明挑战和签名必须成对出现。

        :return "EgoBrowserDeviceRegisterRequest": 通过 proof 字段完整性校验的注册请求
        :raises ValueError: challenge 与签名未同时提供
        """
        if (self.proof_challenge is None) != (self.proof_signature is None):
            raise ValueError("proof_challenge and proof_signature must be supplied together")
        if self.device_generation is not None:
            if self.generation != 1 and self.generation != self.device_generation:
                raise ValueError("generation and device_generation disagree")
            self.generation = self.device_generation
        if self.signing_public_key is not None and self.signing_public_key != self.public_key:
            raise ValueError("public_key and signing_public_key disagree")
        return self


class EgoBrowserDeviceCredentialData(_Schema):
    """
    独立 ego-browser 设备凭据的非秘密元数据。
    """

    id: UUID = Field(..., description="凭据记录 ID")
    ego_browser_device_id: UUID = Field(..., description="所属独立浏览器设备 ID")
    credential_profile: EgoBrowserCredentialProfile = Field(..., description="凭据存储配置档案")
    generation: int = Field(..., description="签发时设备代次（兼容字段）")
    device_generation: int = Field(..., description="签发时独立设备身份代次")
    revision: int = Field(..., description="凭据轮换修订版本")
    credential_revision: int = Field(..., description="明确命名的凭据轮换修订版本")
    expires_at: datetime = Field(..., description="凭据过期时间")
    credential_expires_at: datetime = Field(..., description="明确命名的凭据过期时间")
    credential_scope: Literal["device"] = Field(
        default="device", description="凭据仅授权一个独立 Device identity"
    )


class EgoBrowserDeviceCredentialIssueData(EgoBrowserDeviceCredentialData):
    """
    独立设备凭据首次签发响应；原始 token 只在此响应出现。
    """

    access_token: str = Field(..., description="仅返回一次的独立设备访问凭据")
    token_type: str = Field(default="bearer", description="凭据类型")
    expires_in: int = Field(..., description="凭据有效秒数")


class EgoBrowserDeviceData(_Schema):
    """
    独立 ego-browser 设备的零内容响应数据。
    """

    id: UUID = Field(..., description="设备 ID")
    device_id: UUID = Field(..., description="明确命名的独立设备 ID")
    user_id: UUID = Field(..., description="所属用户 ID")
    public_key: str = Field(..., description="设备公钥")
    signing_public_key: str = Field(..., description="明确命名的 Ed25519 签名公钥")
    encryption_public_key: str | None = Field(
        default=None, description="Bridge 密钥包装用 X25519 公钥"
    )
    generation: int = Field(..., description="设备代次（兼容字段）")
    device_generation: int = Field(..., description="独立设备身份代次")
    status: Literal["active", "retiring", "revoked"] = Field(..., description="设备状态")
    platform: Literal["macos"] = Field(..., description="本地平台")
    release_profile: EgoBrowserReleaseProfile = Field(..., description="Bridge 发布配置档案")
    signer_certificate_sha256: str = Field(..., description="签名证书摘要")
    credential_profile: EgoBrowserCredentialProfile = Field(..., description="凭据存储配置档案")
    bridge_protocol_version: str = Field(..., description="Bridge 协议版本")
    bridge_version: str | None = Field(default=None, description="Bridge 客户端版本")
    local_ego_browser_runtime_version: str | None = Field(
        default=None, description="本地 ego-browser 运行时版本"
    )
    ego_lite_runtime_version: str | None = Field(
        default=None, description="本地 ego-lite 运行时版本"
    )
    skill_version: str | None = Field(default=None, description="调用 Bridge 的自动化技能版本")
    capabilities: list[str] = Field(default_factory=list, description="能力列表")
    allowlist_revision: int = Field(..., description="文件允许列表修订版本")
    allowlist_roots_digest: str | None = Field(default=None, description="允许列表根摘要")
    learning_bundle_digest: str | None = Field(default=None, description="学习软件包内容摘要")
    policy_digest: str = Field(..., description="规范化本地策略摘要")
    capability_digest: str = Field(..., description="规范化能力集合摘要")
    server_origin: str = Field(..., description="登记该身份的规范 Server origin")
    last_seen_at: datetime | None = Field(default=None, description="最后在线时间")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")
    credential: EgoBrowserDeviceCredentialIssueData | None = Field(
        default=None, description="仅注册响应一次返回的独立设备凭据"
    )


class EgoBrowserDeviceResponse(_Schema):
    """
    单个设备响应。
    """

    data: EgoBrowserDeviceData = Field(..., description="独立设备响应数据")
    request_id: str | None = Field(default=None, description="请求追踪 ID")


class EgoBrowserDeviceListData(_Schema):
    """
    设备列表响应数据。
    """

    items: list[EgoBrowserDeviceData] = Field(default_factory=list, description="独立设备数据列表")


class EgoBrowserDeviceListResponse(_Schema):
    """
    设备列表响应。
    """

    data: EgoBrowserDeviceListData = Field(..., description="独立设备列表数据")
    request_id: str | None = Field(default=None, description="请求追踪 ID")


class EgoBrowserDeviceRevokeRequest(_Schema):
    """
    永久撤销独立 ego-browser 设备及其凭据的请求。
    """

    generation: int = Field(default=1, ge=1, description="期望的设备代次（兼容字段）")
    device_generation: int | None = Field(default=None, ge=1, description="期望的独立设备身份代次")
    reason: str = Field(
        default="device_revoked", min_length=1, max_length=64, description="设备撤销原因"
    )
    proof_challenge: str | None = Field(default=None, max_length=512, description="设备 PoP 挑战值")
    proof_signature: str | None = Field(default=None, max_length=512, description="设备 PoP 签名")

    @model_validator(mode="after")
    def require_complete_proof_pair(self) -> "EgoBrowserDeviceRevokeRequest":
        """
        确保设备撤销请求的 proof 字段成对出现。

        :return "EgoBrowserDeviceRevokeRequest": 通过 proof 字段完整性校验的撤销请求
        :raises ValueError: challenge 与签名未同时提供
        """

        if (self.proof_challenge is None) != (self.proof_signature is None):
            raise ValueError("proof_challenge and proof_signature must be supplied together")
        if self.device_generation is not None:
            if self.generation != 1 and self.generation != self.device_generation:
                raise ValueError("generation and device_generation disagree")
            self.generation = self.device_generation
        return self


class EgoBrowserBindingClaimRequest(_Schema):
    """
    用户明确选择远端会话后创建绑定的请求。
    """

    tool_session_id: UUID = Field(..., description="关联的远端工具会话 ID")
    ego_browser_device_id: UUID = Field(..., description="关联的独立 ego-browser 设备 ID")
    encryption_public_key: str | None = Field(
        default=None, min_length=40, max_length=64, description="预期的 Bridge X25519 公钥"
    )
    authorization_mode: EgoBrowserAuthorizationMode = Field(
        default="ego_browser_script_full_trust", description="脚本全信任授权模式"
    )
    authorization_policy_version: int = Field(default=1, ge=1, description="授权策略版本")
    release_profile: EgoBrowserReleaseProfile = Field(
        ..., description="Bridge 构建与签名发布配置档案"
    )
    credential_profile: EgoBrowserCredentialProfile = Field(..., description="设备凭据存储配置档案")
    remote_platform: Literal["linux"] = Field(default="linux", description="远端封装器所在平台")
    local_platform: Literal["macos"] = Field(default="macos", description="Bridge 所在的本地平台")
    device_capabilities: list[str] = Field(
        default_factory=list, max_length=32, description="独立设备声明的 Bridge 能力列表"
    )
    allowlist_revision: int = Field(default=1, ge=1, description="文件允许列表修订版本")
    learning_bundle_digest: str | None = Field(
        default=None, max_length=80, description="学习软件包内容摘要"
    )
    task_space_label: str | None = Field(
        default=None, max_length=256, description="用于识别隔离上下文的脱敏 Task Space 标签"
    )
    concurrency_mode: EgoBrowserConcurrencyMode = Field(
        default="binding", description="浏览器请求并发隔离模式"
    )
    user_confirmation: bool = Field(..., description="用户是否明确确认当前高权限操作")
    proof_challenge: str | None = Field(
        default=None, max_length=512, description="一次性设备 PoP 挑战值"
    )
    proof_signature: str | None = Field(
        default=None, max_length=512, description="设备对 PoP 挑战值的签名"
    )

    @model_validator(mode="after")
    def require_confirmation(self) -> "EgoBrowserBindingClaimRequest":
        """
        要求显式全信任确认并校验 proof 字段成对出现。

        :return "EgoBrowserBindingClaimRequest": 通过用户确认与 proof 完整性校验的认领请求
        :raises ValueError: 用户未明确确认全信任操作或 proof 字段不完整
        """
        if not self.user_confirmation:
            raise ValueError("explicit user_confirmation is required")
        if (self.proof_challenge is None) != (self.proof_signature is None):
            raise ValueError("proof_challenge and proof_signature must be supplied together")
        return self


class EgoBrowserBindingCandidateData(_Schema):
    """
    可供用户选择的远端 Claude 会话候选。
    """

    tool_session_id: UUID = Field(..., description="关联的远端工具会话 ID")
    tool_type: Literal["claude"] = Field(..., description="候选会话的工具类型")
    tool_account_id: UUID = Field(..., description="候选会话使用的工具账户 ID")
    workspace_id: UUID = Field(..., description="候选会话所属工作区 ID")
    project_key: str = Field(..., description="候选工具会话的项目键")
    display_name: str = Field(..., description="候选工作区显示名称")
    status: Literal["running", "active", "detached"] = Field(..., description="候选工具会话状态")
    node_id: UUID = Field(..., description="承载远端封装器的 Node ID")
    runtime_backend: str = Field(..., description="候选会话使用的运行时后端")
    current_ego_browser_device_id: UUID | None = Field(
        default=None, description="当前活跃绑定使用的独立设备 ID"
    )
    current_ego_browser_device_name: str | None = Field(
        default=None, description="当前独立设备的显示名称"
    )
    binding_id: UUID | None = Field(default=None, description="独立浏览器绑定标识")
    controllable: bool = Field(..., description="候选会话当前是否允许认领")


class EgoBrowserBindingCandidateListData(_Schema):
    """
    绑定候选列表数据。
    """

    items: list[EgoBrowserBindingCandidateData] = Field(
        default_factory=list, description="可认领的远端会话候选列表"
    )


class EgoBrowserBindingCandidateListResponse(_Schema):
    """
    绑定候选列表响应。
    """

    data: EgoBrowserBindingCandidateListData = Field(..., description="绑定候选列表数据")
    request_id: str | None = Field(default=None, description="请求追踪 ID")


class EgoBrowserBindingData(_Schema):
    """
    ego-browser 绑定的生命周期和能力元数据。
    """

    id: UUID = Field(..., description="独立浏览器绑定标识")
    user_id: UUID = Field(..., description="绑定所属用户 ID")
    ego_browser_device_id: UUID = Field(..., description="关联的独立 ego-browser 设备 ID")
    encryption_public_key: str | None = Field(
        default=None, description="Bridge 密钥包装用 X25519 公钥"
    )
    tool_session_id: UUID = Field(..., description="关联的远端工具会话 ID")
    node_id: UUID = Field(..., description="承载远端封装器的 Node ID")
    status: EgoBrowserBindingStatus = Field(..., description="绑定生命周期状态")
    control_channel: Literal["ego_browser_bridge"] = Field(
        ..., description="绑定使用的独立控制通道"
    )
    relay_binding_kind: Literal["ego_browser"] = Field(
        ..., description="独立 ego-browser 中继绑定类型"
    )
    authorization_mode: EgoBrowserAuthorizationMode = Field(..., description="脚本全信任授权模式")
    authorization_policy_version: int = Field(..., description="授权策略版本")
    authorized_at: datetime = Field(..., description="用户确认全信任授权的时间")
    release_profile: EgoBrowserReleaseProfile = Field(
        ..., description="Bridge 构建与签名发布配置档案"
    )
    signer_certificate_sha256: str = Field(..., description="Bridge 签名证书 SHA-256 摘要")
    credential_profile: EgoBrowserCredentialProfile = Field(..., description="设备凭据存储配置档案")
    remote_platform: Literal["linux"] = Field(..., description="远端封装器所在平台")
    local_platform: Literal["macos"] = Field(..., description="Bridge 所在的本地平台")
    local_runtime_version: str | None = Field(..., description="Bridge 上报的本地运行时版本")
    ego_lite_runtime_version: str | None = Field(..., description="本地 ego-lite 运行时版本")
    skill_version: str | None = Field(..., description="调用 Bridge 的自动化技能版本")
    bridge_protocol_version: str = Field(..., description="Bridge 协议版本")
    task_space_label: str | None = Field(..., description="用于识别隔离上下文的脱敏任务空间标签")
    allowlist_revision: int = Field(..., description="文件允许列表修订版本")
    allowlist_roots_digest: str | None = Field(..., description="规范化允许列表根摘要")
    learning_bundle_digest: str | None = Field(..., description="学习软件包内容摘要")
    concurrency_mode: EgoBrowserConcurrencyMode = Field(..., description="浏览器请求并发隔离模式")
    max_parallel_requests: int = Field(..., description="单个绑定最大并行请求数")
    capabilities: list[str] = Field(..., description="经策略校验的 Bridge 能力列表")
    lease_until: datetime | None = Field(..., description="当前租约截止时间")
    lease_health: EgoBrowserLeaseHealth = Field(..., description="当前租约健康状态")
    lease_grace_until: datetime | None = Field(..., description="续租失败宽限截止时间")
    lease_renew_interval_seconds: int = Field(..., description="Bridge 自动续租间隔秒数")
    lease_renew_failure_grace_seconds: int = Field(..., description="续租失败后的宽限秒数")
    absolute_ttl_until: datetime = Field(..., description="绑定绝对 TTL 截止时间")
    generation: int = Field(..., description="绑定当前代次（兼容字段）")
    binding_generation: int = Field(..., description="绑定当前代次")
    connected_at: datetime | None = Field(..., description="当前代次首次激活时间")
    stopped_at: datetime | None = Field(..., description="绑定进入终态的时间")
    stop_reason: str | None = Field(..., description="绑定进入终态或暂停的原因")
    revoked_at: datetime | None = Field(..., description="绑定永久撤销时间")
    created_at: datetime = Field(..., description="绑定创建时间")
    updated_at: datetime = Field(..., description="绑定最后更新时间")


class EgoBrowserBindingResponse(_Schema):
    """
    单个绑定响应。
    """

    data: EgoBrowserBindingData = Field(..., description="单个绑定响应数据")
    request_id: str | None = Field(default=None, description="请求追踪 ID")


class EgoBrowserBindingListData(_Schema):
    """
    绑定列表响应数据。
    """

    items: list[EgoBrowserBindingData] = Field(
        default_factory=list, description="ego-browser 绑定数据列表"
    )


class EgoBrowserBindingListResponse(_Schema):
    """
    绑定列表响应。
    """

    data: EgoBrowserBindingListData = Field(..., description="绑定列表响应数据")
    request_id: str | None = Field(default=None, description="请求追踪 ID")


class EgoBrowserActiveRequestData(_Schema):
    """
    可由用户取消的零内容 browser request 元数据。
    """

    id: UUID = Field(..., description="请求账本 ID")
    binding_id: UUID = Field(..., description="所属浏览器绑定 ID")
    generation: int = Field(..., ge=1, description="请求绑定代次（兼容字段）")
    binding_generation: int = Field(..., ge=1, description="请求绑定代次")
    request_id: str = Field(..., min_length=1, max_length=128, description="不透明请求 ID")
    sequence: int = Field(..., ge=1, description="代次内单调序号")
    message_type: Literal["execute"] = Field(..., description="原始请求消息类型")
    payload_bytes: int = Field(..., ge=1, description="不透明密文字节数")
    status: EgoBrowserRequestStatus = Field(..., description="请求取消或终态状态")
    created_at: datetime = Field(..., description="请求接受时间")


class EgoBrowserActiveRequestListData(_Schema):
    """
    当前可取消 browser request 列表数据。
    """

    items: list[EgoBrowserActiveRequestData] = Field(
        default_factory=list, description="当前活跃浏览器请求列表"
    )


class EgoBrowserActiveRequestListResponse(_Schema):
    """
    当前可取消 browser request 列表响应。
    """

    data: EgoBrowserActiveRequestListData = Field(..., description="活跃请求列表数据")
    request_id: str | None = Field(default=None, description="请求追踪 ID")


class EgoBrowserCancelRequest(_Schema):
    """
    取消一个确切 browser request 的控制请求。
    """

    generation: int = Field(default=1, ge=1, description="原始请求代次（兼容字段）")
    binding_generation: int | None = Field(default=None, ge=1, description="原始请求绑定代次")
    sequence: int = Field(..., ge=1, description="原始请求序号")

    @model_validator(mode="after")
    def normalize_binding_generation(self) -> "EgoBrowserCancelRequest":
        """
        兼容旧 generation 字段并固定其 binding 语义。

        :return "EgoBrowserCancelRequest": 规范化后的取消请求
        """

        if self.binding_generation is not None:
            if self.generation != 1 and self.generation != self.binding_generation:
                raise ValueError("generation and binding_generation disagree")
            self.generation = self.binding_generation
        return self


class EgoBrowserCancelResponse(_Schema):
    """
    请求取消状态响应。
    """

    data: EgoBrowserActiveRequestData = Field(..., description="取消后的请求元数据")
    request_id: str | None = Field(default=None, description="请求追踪 ID")


class EgoBrowserConnectedRequest(_Schema):
    """
    Bridge 上报本地运行时能力并激活绑定的请求。
    """

    generation: int = Field(default=1, ge=1, description="待激活的绑定代次（兼容字段）")
    binding_generation: int | None = Field(default=None, ge=1, description="待激活的绑定代次")
    encryption_public_key: str | None = Field(
        default=None, min_length=40, max_length=64, description="Bridge X25519 公钥"
    )
    bridge_protocol_version: str = Field(
        ..., min_length=1, max_length=64, description="Bridge 协议版本"
    )
    bridge_version: str | None = Field(default=None, max_length=64, description="Bridge 客户端版本")
    local_ego_browser_runtime_version: str = Field(
        ..., min_length=1, max_length=64, description="本地 ego-browser 运行时版本"
    )
    ego_lite_runtime_version: str = Field(
        ..., min_length=1, max_length=64, description="本地 ego-lite 运行时版本"
    )
    skill_version: str = Field(
        ..., min_length=1, max_length=64, description="调用 Bridge 的 Skill 版本"
    )
    release_profile: EgoBrowserReleaseProfile = Field(
        ..., description="Bridge 构建与签名发布配置档案"
    )
    signer_certificate_sha256: str = Field(
        ..., min_length=1, max_length=64, description="Bridge 签名证书 SHA-256 摘要"
    )
    credential_profile: EgoBrowserCredentialProfile = Field(..., description="设备凭据存储配置档案")
    allowlist_revision: int = Field(..., ge=1, description="文件允许列表修订版本")
    allowlist_roots_digest: str | None = Field(
        default=None, max_length=80, description="已验证规范化允许列表根摘要"
    )
    learning_bundle_digest: str | None = Field(
        default=None, max_length=80, description="学习软件包内容摘要"
    )
    max_parallel_requests: int = Field(..., ge=1, le=4, description="单个绑定最大并行请求数")
    capabilities: list[str] = Field(
        default_factory=list, max_length=32, description="经策略校验的 Bridge 能力列表"
    )
    local_browser_ready: bool = Field(default=True, description="本地浏览器运行时是否已通过探测")
    proof_challenge: str | None = Field(
        default=None, max_length=512, description="一次性设备 PoP 挑战值"
    )
    proof_signature: str | None = Field(
        default=None, max_length=512, description="设备对 PoP 挑战值的签名"
    )

    @model_validator(mode="after")
    def normalize_binding_generation(self) -> "EgoBrowserConnectedRequest":
        """
        兼容旧 generation 字段并固定其 binding 语义。

        :return "EgoBrowserConnectedRequest": 规范化后的连接请求
        """

        if self.binding_generation is not None:
            if self.generation != 1 and self.generation != self.binding_generation:
                raise ValueError("generation and binding_generation disagree")
            self.generation = self.binding_generation
        return self


class EgoBrowserRenewRequest(_Schema):
    """
    Bridge 续租当前绑定代次的请求。
    """

    generation: int = Field(default=1, ge=1, description="待续租的绑定代次（兼容字段）")
    binding_generation: int | None = Field(default=None, ge=1, description="待续租的绑定代次")
    allowlist_revision: int = Field(..., ge=1, description="文件允许列表修订版本")
    learning_bundle_digest: str | None = Field(
        default=None, max_length=80, description="学习 bundle 内容摘要"
    )
    proof_challenge: str | None = Field(
        default=None, max_length=512, description="一次性设备 PoP 挑战值"
    )
    proof_signature: str | None = Field(
        default=None, max_length=512, description="设备对 PoP 挑战值的签名"
    )

    @model_validator(mode="after")
    def normalize_binding_generation(self) -> "EgoBrowserRenewRequest":
        """
        兼容旧 generation 字段并固定其 binding 语义。

        :return "EgoBrowserRenewRequest": 规范化后的续租请求
        """

        if self.binding_generation is not None:
            if self.generation != 1 and self.generation != self.binding_generation:
                raise ValueError("generation and binding_generation disagree")
            self.generation = self.binding_generation
        return self


class EgoBrowserLifecycleRequest(_Schema):
    """
    停止或暂停绑定的生命周期请求。
    """

    generation: int = Field(default=1, ge=1, description="期望的绑定代次（兼容字段）")
    binding_generation: int | None = Field(default=None, ge=1, description="期望的绑定代次")
    reason: str = Field(
        default="user_stop", min_length=1, max_length=64, description="生命周期原因"
    )
    proof_challenge: str | None = Field(default=None, max_length=512, description="设备 PoP 挑战值")
    proof_signature: str | None = Field(default=None, max_length=512, description="设备 PoP 签名")

    @model_validator(mode="after")
    def require_complete_proof_pair(self) -> "EgoBrowserLifecycleRequest":
        """
        确保生命周期请求的 proof 字段成对出现。

        :return "EgoBrowserLifecycleRequest": 通过 proof 字段完整性校验的生命周期请求
        :raises ValueError: challenge 与签名未同时提供
        """

        if (self.proof_challenge is None) != (self.proof_signature is None):
            raise ValueError("proof_challenge and proof_signature must be supplied together")
        if self.binding_generation is not None:
            if self.generation != 1 and self.generation != self.binding_generation:
                raise ValueError("generation and binding_generation disagree")
            self.generation = self.binding_generation
        return self


class EgoBrowserResumeRequest(_Schema):
    """
    用户确认后恢复暂停绑定的请求。
    """

    generation: int = Field(default=1, ge=1, description="待恢复的绑定代次（兼容字段）")
    binding_generation: int | None = Field(default=None, ge=1, description="待恢复的绑定代次")
    user_confirmation: bool = Field(..., description="用户是否明确确认当前高权限操作")
    allowlist_revision: int = Field(..., ge=1, description="文件允许列表修订版本")
    learning_bundle_digest: str | None = Field(
        default=None, max_length=80, description="学习 bundle 内容摘要"
    )
    proof_challenge: str | None = Field(
        default=None, max_length=512, description="一次性设备 PoP 挑战值"
    )
    proof_signature: str | None = Field(
        default=None, max_length=512, description="设备对 PoP 挑战值的签名"
    )

    @model_validator(mode="after")
    def require_complete_proof_pair(self) -> "EgoBrowserResumeRequest":
        """
        确保恢复请求的 proof 字段成对出现。

        :return "EgoBrowserResumeRequest": 通过 proof 字段完整性校验的恢复请求
        :raises ValueError: challenge 与签名未同时提供
        """
        if (self.proof_challenge is None) != (self.proof_signature is None):
            raise ValueError("proof_challenge and proof_signature must be supplied together")
        if self.binding_generation is not None:
            if self.generation != 1 and self.generation != self.binding_generation:
                raise ValueError("generation and binding_generation disagree")
            self.generation = self.binding_generation
        return self

    @field_validator("user_confirmation")
    @classmethod
    def must_confirm(cls, value: bool) -> bool:
        """
        要求恢复操作包含显式用户确认。

        :param value (bool): 待校验或规范化的值
        :return bool: 通过显式确认校验的布尔值
        :raises ValueError: 用户未明确确认恢复操作
        """
        if not value:
            raise ValueError("explicit user_confirmation is required")
        return value


class EgoBrowserRelayTicketRequest(_Schema):
    """
    申请一次性中继票据的请求。
    """

    generation: int = Field(default=1, ge=1, description="申请票据的绑定代次（兼容字段）")
    binding_generation: int | None = Field(default=None, ge=1, description="申请票据的绑定代次")
    role: EgoBrowserRelayRole = Field(..., description="中继连接端角色")
    ego_browser_device_id: UUID | None = Field(
        default=None, description="关联的独立 ego-browser 设备 ID"
    )
    proof_challenge: str | None = Field(
        default=None, max_length=512, description="一次性设备 PoP 挑战值"
    )
    proof_signature: str | None = Field(
        default=None, max_length=512, description="设备对 PoP 挑战值的签名"
    )

    @model_validator(mode="after")
    def normalize_binding_generation(self) -> "EgoBrowserRelayTicketRequest":
        """
        兼容旧 generation 字段并固定其 binding 语义。

        :return "EgoBrowserRelayTicketRequest": 规范化后的 relay 票据请求
        """

        if self.binding_generation is not None:
            if self.generation != 1 and self.generation != self.binding_generation:
                raise ValueError("generation and binding_generation disagree")
            self.generation = self.binding_generation
        return self


class EgoBrowserRelayTicketData(_Schema):
    """
    一次性中继票据的返回元数据。
    """

    role: EgoBrowserRelayRole = Field(..., description="中继连接端角色")
    generation: int = Field(..., description="票据绑定的代次（兼容字段）")
    binding_generation: int = Field(..., description="票据绑定的代次")
    relay_binding_kind: Literal["ego_browser"] = Field(
        ..., description="独立 ego-browser 中继绑定类型"
    )
    relay_path: str = Field(..., description="建立中继 WebSocket 的 API 路径")
    relay_ticket: str = Field(..., description="仅返回一次的一次性中继票据")
    expires_at: datetime = Field(..., description="一次性中继票据过期时间")


class EgoBrowserRelayTicketResponse(_Schema):
    """
    中继票据响应。
    """

    data: EgoBrowserRelayTicketData = Field(..., description="一次性中继票据数据")
    request_id: str | None = Field(default=None, description="请求追踪 ID")


class EgoBrowserNodeBindingData(_Schema):
    """
    提供给承载封装器的 Node 的绑定元数据；不包含脚本或连接秘密。
    """

    binding_id: UUID = Field(..., description="独立浏览器绑定标识")
    ego_browser_device_id: UUID = Field(..., description="本地独立浏览器设备 ID")
    encryption_public_key: str | None = Field(default=None, description="Bridge X25519 加密公钥")
    tool_session_id: UUID = Field(..., description="远端工具会话 ID")
    node_id: UUID = Field(..., description="承载封装器的 Node ID")
    status: EgoBrowserBindingStatus = Field(..., description="绑定状态")
    control_channel: Literal["ego_browser_bridge"] = Field(..., description="独立控制通道")
    relay_binding_kind: Literal["ego_browser"] = Field(..., description="独立中继绑定类型")
    authorization_mode: EgoBrowserAuthorizationMode = Field(..., description="全信任授权模式")
    authorization_policy_version: int = Field(..., description="授权策略版本")
    generation: int = Field(..., description="当前绑定代次（兼容字段）")
    binding_generation: int = Field(..., description="当前绑定代次")
    release_profile: EgoBrowserReleaseProfile = Field(..., description="Bridge 发布配置档案")
    signer_certificate_sha256: str = Field(..., description="签名证书摘要")
    credential_profile: EgoBrowserCredentialProfile = Field(..., description="凭据存储配置档案")
    remote_platform: Literal["linux"] = Field(..., description="远端封装器平台")
    local_platform: Literal["macos"] = Field(..., description="本地 Bridge 平台")
    bridge_protocol_version: str = Field(..., description="Bridge 协议版本")
    local_runtime_version: str | None = Field(default=None, description="本地运行时版本")
    ego_lite_runtime_version: str | None = Field(default=None, description="ego-lite 运行时版本")
    skill_version: str | None = Field(default=None, description="自动化技能版本")
    task_space_label: str | None = Field(default=None, description="脱敏任务空间标签")
    allowlist_revision: int = Field(..., description="文件允许列表修订版本")
    allowlist_roots_digest: str | None = Field(default=None, description="允许列表根摘要")
    learning_bundle_digest: str | None = Field(default=None, description="学习软件包内容摘要")
    concurrency_mode: EgoBrowserConcurrencyMode = Field(..., description="默认并发模式")
    max_parallel_requests: int = Field(..., description="最大并发请求数")
    capabilities: list[str] = Field(default_factory=list, description="已确认能力")
    lease_until: datetime | None = Field(default=None, description="当前租约截止时间")
    lease_health: EgoBrowserLeaseHealth = Field(..., description="租约健康状态")
    lease_grace_until: datetime | None = Field(default=None, description="续租宽限截止时间")
    lease_renew_interval_seconds: int = Field(..., description="自动续租间隔")
    lease_renew_failure_grace_seconds: int = Field(..., description="续租失败宽限")
    absolute_ttl_until: datetime = Field(..., description="绝对 TTL 截止时间")


class EgoBrowserNodeBindingListData(_Schema):
    """
    Node 当前承载的 ego-browser 绑定列表。
    """

    items: list[EgoBrowserNodeBindingData] = Field(default_factory=list, description="绑定列表")


class EgoBrowserNodeBindingListResponse(_Schema):
    """
    Node 绑定列表响应。
    """

    data: EgoBrowserNodeBindingListData = Field(..., description="绑定列表数据")
    request_id: str | None = Field(default=None, description="请求 ID")


class EgoBrowserNodeRenewRequest(_Schema):
    """
    Node 代理续租绑定的请求。
    """

    generation: int = Field(default=1, ge=1, description="期望的绑定代次（兼容字段）")
    binding_generation: int | None = Field(default=None, ge=1, description="期望的绑定代次")
    allowlist_revision: int = Field(..., ge=1, description="期望的允许列表修订版本")
    learning_bundle_digest: str | None = Field(
        default=None, max_length=80, description="期望的学习 bundle 摘要"
    )

    @model_validator(mode="after")
    def normalize_binding_generation(self) -> "EgoBrowserNodeRenewRequest":
        """
        兼容旧 generation 字段并固定其 binding 语义。

        :return "EgoBrowserNodeRenewRequest": 规范化后的 Node 续租请求
        """

        if self.binding_generation is not None:
            if self.generation != 1 and self.generation != self.binding_generation:
                raise ValueError("generation and binding_generation disagree")
            self.generation = self.binding_generation
        return self


class EgoBrowserNodeRenewData(_Schema):
    """
    Node 代理续租结果。
    """

    binding_id: UUID = Field(..., description="独立浏览器绑定标识")
    generation: int = Field(..., description="当前代次（兼容字段）")
    binding_generation: int = Field(..., description="当前绑定代次")
    lease_until: datetime | None = Field(default=None, description="新的租约截止时间")
    lease_health: EgoBrowserLeaseHealth = Field(..., description="租约健康状态")
    lease_grace_until: datetime | None = Field(default=None, description="宽限截止时间")
    absolute_ttl_until: datetime = Field(..., description="绝对 TTL 截止时间")


class EgoBrowserNodeRenewResponse(_Schema):
    """
    Node 代理续租响应。
    """

    data: EgoBrowserNodeRenewData = Field(..., description="续租结果")
    request_id: str | None = Field(default=None, description="请求 ID")


class EgoBrowserAllowlistData(_Schema):
    """
    文件允许列表的修订版本和限制元数据。
    """

    allowlist_revision: int = Field(..., description="文件允许列表修订版本")
    roots_digest: str | None = Field(..., description="规范化允许列表根摘要")
    file_limits: dict[str, int] = Field(..., description="文件数量与字节数限制")


class EgoBrowserAllowlistResponse(_Schema):
    """
    文件允许列表响应。
    """

    data: EgoBrowserAllowlistData = Field(..., description="允许列表修订版本与文件限制数据")
    request_id: str | None = Field(default=None, description="请求追踪 ID")


class EgoBrowserAllowlistConfirmRequest(_Schema):
    """
    用户确认新文件允许列表修订版本的请求。
    """

    generation: int = Field(..., ge=1, description="期望的绑定代次（兼容字段）")
    binding_generation: int | None = Field(default=None, ge=1, description="期望的绑定代次")
    expected_revision: int = Field(..., ge=1, description="客户端期望替换的允许列表修订版本")
    roots_digest: str = Field(..., min_length=1, max_length=128, description="规范化允许列表根摘要")
    user_confirmation: bool = Field(..., description="用户是否明确确认当前高权限操作")
    proof_challenge: str | None = Field(
        default=None, max_length=512, description="一次性设备 PoP 挑战值"
    )
    proof_signature: str | None = Field(
        default=None, max_length=512, description="设备对 PoP 挑战值的签名"
    )

    @model_validator(mode="after")
    def normalize_binding_generation(self) -> "EgoBrowserAllowlistConfirmRequest":
        """
        兼容旧 generation 字段并固定其 binding 语义。

        :return "EgoBrowserAllowlistConfirmRequest": 规范化后的 allowlist 确认请求
        """

        if self.binding_generation is not None:
            if self.generation != 1 and self.generation != self.binding_generation:
                raise ValueError("generation and binding_generation disagree")
            self.generation = self.binding_generation
        return self

    @field_validator("user_confirmation")
    @classmethod
    def must_confirm(cls, value: bool) -> bool:
        """
        要求 allowlist 更新包含显式用户确认。

        :param value (bool): 待校验或规范化的值
        :return bool: 通过显式确认校验的布尔值
        :raises ValueError: 用户未明确确认 allowlist 更新
        """
        if not value:
            raise ValueError("explicit user_confirmation is required")
        return value


class EgoBrowserOuterEnvelope(_Schema):
    """
    服务端可见的外层信封；内层密文始终保持 opaque。
    """

    protocol: EgoBrowserProtocol = Field(..., description="外层中继协议版本")
    channel: Literal["ego_browser_bridge"] = Field(..., description="外层信封使用的独立控制通道")
    relay_binding_kind: Literal["ego_browser"] = Field(
        ..., description="独立 ego-browser 中继绑定类型"
    )
    type: Literal["execute", "execute_result", "cancel"] = Field(
        ..., description="外层执行、结果或取消消息类型"
    )
    request_id: str = Field(
        ..., min_length=1, max_length=128, description="端到端不透明浏览器请求 ID"
    )
    binding_id: str = Field(..., min_length=1, max_length=128, description="独立浏览器绑定标识")
    generation: int = Field(..., ge=1, description="信封绑定的代次")
    sequence: int = Field(..., ge=1, description="当前代次和方向内的单调序号")
    direction: Literal["request", "response"] = Field(..., description="外层信封的请求或响应方向")
    payload_bytes: int = Field(..., ge=0, description="不透明密文载荷的解码后字节数")
    nonce: str = Field(..., description="内层 AEAD 使用的规范 Base64URL 随机数")
    ciphertext: str = Field(..., description="服务端保持不透明的规范 Base64URL 密文")
    auth_tag: str = Field(..., description="内层 AEAD 的规范 Base64URL 认证标签")
    key_wrap: str = Field(default="", description="每请求密钥包装密文")

    @model_validator(mode="after")
    def validate_direction_type(self) -> "EgoBrowserOuterEnvelope":
        """
        确保外层消息类型与传输方向一致。

        :return "EgoBrowserOuterEnvelope": 通过消息方向与类型校验的外层信封
        :raises ValueError: 消息类型与传输方向不匹配
        """
        if (self.direction == "request" and self.type not in {"execute", "cancel"}) or (
            self.direction == "response" and self.type != "execute_result"
        ):
            raise ValueError("message type does not match direction")
        return self


class EgoBrowserPolicyData(_Schema):
    """
    当前 Bridge 策略与限制。
    """

    enabled: bool = Field(..., description="部署是否启用 ego-browser 桥接服务")
    enrollment_enabled: bool = Field(
        default=True, description="是否允许设备登记、状态查询和凭据刷新"
    )
    execution_admission: bool = Field(default=False, description="是否允许 claim、relay 和远端执行")
    protocol: EgoBrowserProtocol = Field(..., description="外层中继协议版本")
    authorization_mode: EgoBrowserAuthorizationMode = Field(..., description="脚本全信任授权模式")
    authorization_policy_version: int = Field(..., description="授权策略版本")
    remote_platform: Literal["linux"] = Field(..., description="远端封装器所在平台")
    local_platform: Literal["macos"] = Field(..., description="Bridge 所在的本地平台")
    lease_seconds: int = Field(..., description="每次成功续租授予的租约秒数")
    lease_renew_interval_seconds: int = Field(..., description="Bridge 自动续租间隔秒数")
    lease_renew_failure_grace_seconds: int = Field(..., description="续租失败后的宽限秒数")
    admission_min_remaining_seconds: int = Field(..., description="允许新请求时租约至少剩余的秒数")
    absolute_ttl_seconds: int = Field(..., description="绑定不可延长的绝对存活秒数")
    max_parallel_requests: int = Field(..., description="单个绑定最大并行请求数")
    max_frame_bytes: int = Field(..., description="中继允许的单帧最大字节数")
    max_script_bytes: int = Field(..., description="单次脚本载荷最大字节数")


class EgoBrowserMachineStateData(_Schema):
    """
    Server 可证明的五状态投影；本机不可观察的值保持 unknown。
    """

    installed: bool | None = Field(
        default=None, description="本机 release 验证状态；Server 无本机证据时为 null"
    )
    enabled: bool | None = Field(
        default=None, description="本机功能和 release 启用状态；Server 无本机证据时为 null"
    )
    registered: bool = Field(..., description="是否存在 active Server Device")
    available: bool | None = Field(
        default=None,
        description="是否满足 claim 前置条件；本机 readiness 不可观察时为 null",
    )
    connected: bool | None = Field(
        default=None,
        description="是否满足执行条件；本机 admission 不可观察时为 null",
    )


class EgoBrowserLifecycleStatusData(_Schema):
    """
    不把 Server 记录猜测成本机状态的只读生命周期摘要。
    """

    state: EgoBrowserMachineStateData = Field(..., description="统一五状态投影")
    scope: Literal["current_user", "all_users"] = Field(..., description="状态聚合范围")
    local_observation: Literal["unknown"] = Field(
        default="unknown", description="Server 无法直接观察本机 release 和 admission"
    )
    stale: bool = Field(default=False, description="Server 查询结果是否来自缓存")
    checked_at: datetime = Field(..., description="Server 计算该投影的时间")


class EgoBrowserLifecycleStatusResponse(_Schema):
    """
    统一五状态响应。
    """

    data: EgoBrowserLifecycleStatusData = Field(..., description="生命周期状态数据")
    request_id: str | None = Field(default=None, description="请求追踪 ID")


class EgoBrowserPolicyResponse(_Schema):
    """
    策略响应。
    """

    data: EgoBrowserPolicyData = Field(..., description="当前 Bridge 策略与限制")
    request_id: str | None = Field(default=None, description="请求追踪 ID")


class EgoBrowserErrorResponse(_Schema):
    """
    ego-browser 接口错误响应。
    """

    error: dict[str, object] = Field(..., description="结构化 API 错误对象")
    request_id: str | None = Field(default=None, description="请求追踪 ID")
