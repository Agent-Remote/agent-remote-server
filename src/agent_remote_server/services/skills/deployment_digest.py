"""
匹配 Go Helper 对完整部署输入的规范 JSON 摘要，保持整数和 Unicode 字节一致。
"""

import hashlib
import json

from agent_remote_server.schemas.skill_deployment_content import SkillDeploymentContent


def deployment_input_digest(content: SkillDeploymentContent) -> str:
    """
    固定模型字段与数组次序，复现 Go encoding/json 的排序和 HTML 转义。

    :param content (SkillDeploymentContent): 原始完整部署输入
    :return str: Helper 可交叉验证的 SHA256 摘要
    """
    encoded = json.dumps(
        content.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    for value, escaped in (
        ("&", r"\u0026"),
        ("<", r"\u003c"),
        (">", r"\u003e"),
        ("\u2028", r"\u2028"),
        ("\u2029", r"\u2029"),
    ):
        encoded = encoded.replace(value, escaped)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
