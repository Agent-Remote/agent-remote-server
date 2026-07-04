from dataclasses import dataclass

from agent_remote_server.errors import ApiError


@dataclass(frozen=True)
class ToolRuntimeTemplate:
    """
    工具运行模板
    """

    tool_type: str
    sandbox_agent: str
    command: list[str]
    verifier: str
    account_config_subdir: str


class ToolRegistry:
    """
    工具类型注册表
    """

    _templates: dict[str, ToolRuntimeTemplate] = {
        "claude": ToolRuntimeTemplate(
            tool_type="claude",
            sandbox_agent="claude",
            command=["claude", "login"],
            verifier="claude",
            account_config_subdir="claude",
        )
    }

    @classmethod
    def get(cls, tool_type: str) -> ToolRuntimeTemplate:
        """
        读取工具模板

        :param tool_type (str): 工具类型

        :return ToolRuntimeTemplate: 工具模板
        """

        template = cls._templates.get(tool_type)
        if template is None:
            raise ApiError(
                code="TOOL_TYPE_UNSUPPORTED",
                message="Tool type is not supported.",
                status_code=422,
            )
        return template

    @classmethod
    def supported_tool_types(cls) -> list[str]:
        """
        列出支持的工具类型

        :return list: 工具类型列表
        """

        return sorted(cls._templates)
