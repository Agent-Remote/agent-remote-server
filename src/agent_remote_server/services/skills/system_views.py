"""
解释配置目录和原快照的系统版本引用，不探测或宣称模型加载。
"""

from typing import Literal

from agent_remote_server.config import Settings
from agent_remote_server.schemas.skill_effective import SystemSkillView
from agent_remote_server.services.skills.content import SkillContentError


def configured_systems(settings: Settings, effective: bool) -> list[SystemSkillView]:
    """
    系统项独立于用户库，设备项的节点发布版本在实际预约时固定。

    :param settings (Settings): 当前发布配置
    :param effective (bool): 是否解释账户或工具有效配置
    :return list[SystemSkillView]: 当前系统目录及明确条件
    """
    return [
        SystemSkillView(
            name="agent-remote-device",
            selected=None if settings.device_control_enabled else False,
            selection_reason="requires_device_control_capability_at_session_start"
            if settings.device_control_enabled
            else "device_control_disabled",
            release={"protocol_version": 1},
        ),
        SystemSkillView(
            name="ego-browser",
            selected=True if effective else None,
            selection_reason="configured_for_managed_session" if effective else "system_catalog",
            release={
                "version": settings.ego_browser_expected_skill_version,
                "commit": settings.ego_browser_expected_skill_commit,
                "tree_sha256": settings.ego_browser_expected_skill_tree_sha256,
            },
        ),
    ]


def saved_systems(references: dict[str, object]) -> list[SystemSkillView]:
    """
    只读取原始引用，旧字符串引用按原文保留，不用当前配置补全缺失字段。

    :param references (dict[str, object]): 原快照保存的系统引用
    :return list[SystemSkillView]: 精确历史系统选择
    """
    result = []
    for name, raw in sorted(references.items()):
        if name not in {"ego-browser", "agent-remote-device"}:
            raise SkillContentError("SNAPSHOT_METADATA_INVALID", "unrecognized saved system skill")
        release: dict[str, str | int] = {}
        if isinstance(raw, str):
            release["legacy_reference"] = raw
        elif isinstance(raw, dict) and len(raw) <= 8:
            for key, value in raw.items():
                if key not in {
                    "version",
                    "commit",
                    "tree_sha256",
                    "node_release_version",
                    "protocol_version",
                } or type(value) not in {str, int}:
                    raise SkillContentError(
                        "SNAPSHOT_METADATA_INVALID", "invalid saved system release"
                    )
                release[key] = value
        else:
            raise SkillContentError("SNAPSHOT_METADATA_INVALID", "invalid saved system release")
        if any(
            isinstance(value, str) and (len(value) > 256 or any(ord(c) < 32 for c in value))
            for value in release.values()
        ):
            raise SkillContentError("SNAPSHOT_METADATA_INVALID", "invalid saved system release")
        system_name: Literal["ego-browser", "agent-remote-device"] = (
            "ego-browser" if name == "ego-browser" else "agent-remote-device"
        )
        result.append(
            SystemSkillView(
                name=system_name,
                selected=True,
                selection_reason="session_snapshot",
                release=release,
            )
        )
    return result
