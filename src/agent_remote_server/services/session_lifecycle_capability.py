"""
严格规范化已安装 Helper 提供的逐后端停止意图能力。
"""


def normalize_session_lifecycle(value: object) -> dict[str, object]:
    """
    仅保留字段及 JSON 类型都精确匹配版本一的后端条目。

    :param value (object): 不可信节点能力投影
    :return dict[str, object]: 有效后端能力，不推断进程已停止
    """
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, object] = {}
    for backend in ("native", "docker_sandbox"):
        entry = value.get(backend)
        if (
            isinstance(entry, dict)
            and set(entry) == {"supported", "protocol_version"}
            and entry["supported"] is True
            and type(entry["protocol_version"]) is int
            and entry["protocol_version"] == 1
        ):
            normalized[backend] = {"supported": True, "protocol_version": 1}
    return normalized
