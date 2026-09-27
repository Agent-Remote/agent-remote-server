"""
定义内容服务与保活分析共用的稳定错误，避免两个业务模块互相初始化。
"""


class SkillContentError(ValueError):
    """
    不包含私有内容的稳定业务错误。
    """

    def __init__(
        self, code: str, message: str, *, details: dict[str, object] | None = None
    ) -> None:
        """
        保存用于 API 映射的稳定错误码。

        :param code (str): 稳定错误标识
        :param message (str): 可公开的诊断说明
        :param details (dict[str, object] | None): 已授权对象的结构化差异
        """
        super().__init__(message)
        self.code = code
        self.details = details or {}
