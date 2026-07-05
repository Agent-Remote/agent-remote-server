from fastapi import APIRouter

from agent_remote_server import __version__
from agent_remote_server.api import (
    auth,
    browser_sessions,
    devices,
    network,
    node_api,
    nodes,
    sessions,
    sync_sessions,
    tool_accounts,
    users,
    workspaces,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(devices.router)
api_router.include_router(network.router)
api_router.include_router(nodes.router)
api_router.include_router(workspaces.router)
api_router.include_router(sync_sessions.router)
api_router.include_router(tool_accounts.router)
api_router.include_router(sessions.router)
api_router.include_router(browser_sessions.router)
api_router.include_router(node_api.router)


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
