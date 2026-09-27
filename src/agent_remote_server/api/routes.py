"""
提供路由 API。
"""

from fastapi import APIRouter

from agent_remote_server import __version__
from agent_remote_server.api import (
    audit_logs,
    auth,
    browser_sessions,
    developer_credentials,
    device_sessions,
    devices,
    ego_browser,
    network,
    node_api,
    node_skill_content,
    node_skill_deployment,
    node_skill_deployment_result,
    node_skill_deployment_termination,
    node_skill_finalization,
    node_skill_imports,
    node_skill_takeover,
    node_skill_termination,
    nodes,
    port_forwards,
    sessions,
    skill_conflict_content,
    skill_conflicts,
    skill_content,
    skill_deployment_retry,
    skill_migration,
    skill_migration_conflicts,
    skill_migration_resolution,
    skill_migration_resolution_content,
    skill_node_export,
    skill_preparation,
    skill_prune,
    skill_resolution_previews,
    skill_state_commands,
    skill_state_queries,
    skills,
    sync_sessions,
    tool_accounts,
    users,
    workspaces,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(audit_logs.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(devices.router)
api_router.include_router(device_sessions.router)
api_router.include_router(device_sessions.node_router)
api_router.include_router(ego_browser.router)
api_router.include_router(ego_browser.node_router)
api_router.include_router(network.router)
api_router.include_router(nodes.router)
api_router.include_router(workspaces.router)
api_router.include_router(sync_sessions.router)
api_router.include_router(tool_accounts.router)
api_router.include_router(sessions.router)
api_router.include_router(skill_content.router)
api_router.include_router(node_skill_content.router)
api_router.include_router(node_skill_deployment.router)
api_router.include_router(node_skill_deployment_result.router)
api_router.include_router(node_skill_deployment_termination.router)
api_router.include_router(node_skill_finalization.router)
api_router.include_router(node_skill_termination.router)
api_router.include_router(node_skill_takeover.router)
api_router.include_router(skills.router)
api_router.include_router(skill_deployment_retry.router)
api_router.include_router(skill_conflicts.router)
api_router.include_router(skill_conflict_content.router)
api_router.include_router(skill_state_queries.router)
api_router.include_router(skill_node_export.router)
api_router.include_router(skill_node_export.node_router)
api_router.include_router(skill_state_commands.router)
api_router.include_router(skill_prune.router)
api_router.include_router(skill_preparation.router)
api_router.include_router(skill_migration.router)
api_router.include_router(skill_migration_conflicts.router)
api_router.include_router(skill_migration_resolution_content.router)
api_router.include_router(skill_migration_resolution.router)
api_router.include_router(skill_resolution_previews.router)
api_router.include_router(port_forwards.router)
api_router.include_router(browser_sessions.router)
api_router.include_router(developer_credentials.router)
api_router.include_router(node_api.router)
api_router.include_router(node_skill_imports.router)
api_router.include_router(port_forwards.node_router)


@api_router.get("/version", tags=["system"])
async def version_info() -> dict[str, object]:
    """
    返回服务版本信息

    :return dict[str, object]: 版本信息响应
    """

    return {
        "data": {
            "service": "agent-remote-server",
            "version": __version__,
        }
    }
