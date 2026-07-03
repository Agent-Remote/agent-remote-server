from fastapi import Request

from agent_remote_server.config import Settings


def get_settings(request: Request) -> Settings:
    """
    获取应用配置

    :param request (Request): 当前请求对象

    :return Settings: 应用配置实例
    """

    return request.app.state.settings
