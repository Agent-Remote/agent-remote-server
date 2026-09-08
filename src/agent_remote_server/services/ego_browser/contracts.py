"""定义 ego-browser 服务共享的稳定常量与返回类型。"""

import re
from dataclasses import dataclass
from datetime import datetime

from agent_remote_server.ego_browser.relay import EgoBrowserRelayRole
from agent_remote_server.models import EgoBrowserBinding, EgoBrowserDeviceCredential

REQUIRED_CAPABILITIES: tuple[str, ...] = (
    "ego_browser_script_execute_v1",
    "ego_browser_snapshot_v1",
    "ego_browser_screenshot_artifact_v1",
    "ego_browser_task_space_v1",
    "ego_browser_concurrency_v1",
)
POLICY_CAPABILITIES: tuple[str, ...] = (
    "ego_browser_file_allowlist_v1",
    "ego_browser_site_learning_v1",
)
KNOWN_CAPABILITIES = frozenset((*REQUIRED_CAPABILITIES, *POLICY_CAPABILITIES))
TERMINAL_STATUSES = {"stopped", "expired", "failed", "revoked"}
LIVE_FOR_CLAIM = {"pending_device", "connecting", "probing_local_browser", "active", "paused"}
SAFE_LABEL = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
SAFE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
NODE_CAPABILITY_FIELDS = frozenset(
    {
        "supported",
        "protocol_versions",
        "backends",
        "wrapper_version",
        "skill_version",
        "skill_tree_sha256",
        "remote_platform",
        "local_platform",
        "max_script_bytes",
        "max_execute_timeout_ms",
    }
)

REVOCATION_METRIC_REASONS = {
    "absolute_ttl": "absolute_ttl",
    "admin_cleanup": "admin",
    "admin_revoke": "admin",
    "allowlist_changed": "policy",
    "local_policy_changed": "policy",
    "device_key_rotated": "device_key",
    "device_revoked": "device",
    "lease_expired": "lease",
    "node_heartbeat_lost": "node",
    "node_reconcile": "node",
    "node_revoked": "node",
    "node_unavailable": "node",
    "pause": "pause",
    "request_cancel_unconfirmed": "pause",
    "renewal_grace_expired": "lease",
    "resume_generation_change": "resume",
    "tool_session_bulk_delete": "tool_session",
    "tool_session_delete": "tool_session",
    "tool_session_stop": "tool_session",
    "tool_session_stopped": "tool_session",
    "tool_session_terminal": "tool_session",
    "user_device_revoked": "device",
    "user_disabled": "user",
    "user_revoked": "user",
    "user_stop": "user",
}
CONTENT_FREE_REASONS = frozenset(
    (
        *REVOCATION_METRIC_REASONS,
        "admin_stop",
        "other",
        "task_space_monitor_unavailable",
        "task_space_takeover",
        "user_pause",
        "user_revoke",
    )
)


@dataclass(frozen=True)
class EgoBrowserClaimResult:
    """ego-browser 绑定认领结果。"""

    binding: EgoBrowserBinding


@dataclass(frozen=True)
class EgoBrowserRelayTicketResult:
    """一次性 ego-browser 中继票据结果。"""

    role: EgoBrowserRelayRole
    generation: int
    relay_ticket: str
    expires_at: datetime


@dataclass(frozen=True)
class EgoBrowserDeviceCredentialIssue:
    """独立设备客户端凭据签发结果；原始令牌不写入数据库。"""

    credential: EgoBrowserDeviceCredential
    raw_token: str
    expires_in: int


@dataclass(frozen=True)
class EgoBrowserProofChallengeIssue:
    """服务端签发的一次性设备 PoP challenge。"""

    challenge: str
    expires_at: datetime
