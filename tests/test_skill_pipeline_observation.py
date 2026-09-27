"""
验证容量诊断只输出有限标签和数字，不回显节点探测中的内容。
"""

import json
from datetime import UTC, datetime, timedelta

from skill_pipeline_observation import readiness_observation


def test_readiness_observation_preserves_missing_and_failed_probe_evidence() -> None:
    """
    未知或失败报告保留为否定观察，并丢弃所有原始诊断文本。
    """
    result = readiness_observation(
        "untrusted-diagnostic",
        None,
        {
            "backends": "native",
            "skill_manager": {"native": "untrusted-diagnostic"},
            "probe_errors": ["untrusted-diagnostic"],
        },
    )
    assert result == {
        "status": "unknown",
        "heartbeat_age_seconds": None,
        "native_reported": False,
        "native_skill_reported": False,
        "probe_error_count": 1,
    }
    assert "untrusted-diagnostic" not in json.dumps(result)


def test_readiness_observation_reports_age_without_upgrading_capability() -> None:
    """
    区分最近报告和缺失后端，只记录能力字典存在而不推断兼容性。
    """
    for aware in (True, False):
        heartbeat = datetime.now(UTC) - timedelta(seconds=120)
        if not aware:
            heartbeat = heartbeat.replace(tzinfo=None)
        result = readiness_observation(
            "degraded", heartbeat, {"backends": [], "skill_manager": {"native": {}}}
        )
        assert result["heartbeat_age_seconds"] == 120
        assert result["native_reported"] is False
        assert result["native_skill_reported"] is True
        assert result["probe_error_count"] is None
