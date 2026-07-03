from fastapi import APIRouter

from agent_remote_server import __version__

api_router = APIRouter(prefix="/api/v1")


@api_router.get("/version", tags=["system"])
async def version_info() -> dict[str, object]:
    """
    返回服务和协议版本信息

    :return dict: 版本信息响应
    """

    return {
        "data": {
            "service": "agent-remote-server",
            "version": __version__,
            "protocol_version": "0.1.0",
        }
    }
