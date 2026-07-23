from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import (
    get_current_token,
    get_current_user,
    get_session,
    get_settings,
    require_admin,
)
from agent_remote_server.api.developer_credentials import profile_data
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.models import AuthToken, ToolAccount, User
from agent_remote_server.schemas.developer_credentials import (
    BindDeveloperCredentialProfileRequest,
    DeveloperCredentialProfileResponse,
)
from agent_remote_server.schemas.tool_accounts import (
    BindingStatusResponse,
    CreateToolAccountRequest,
    RuntimeMigrationRequest,
    RuntimeMigrationResponse,
    ToolAccountConfigImportRequest,
    ToolAccountConfigImportResponse,
    ToolAccountData,
    ToolAccountListData,
    ToolAccountListResponse,
    ToolAccountResponse,
    UpdateToolAccountRequest,
)
from agent_remote_server.services.developer_credentials import DeveloperCredentialService
from agent_remote_server.services.tool_accounts import ToolAccountService

router = APIRouter(prefix="/tool-accounts", tags=["tool-accounts"])


@router.post("/{tool_account_id}/runtime-migration", response_model=RuntimeMigrationResponse)
async def migrate_tool_account_runtime(
    tool_account_id: UUID,
    payload: RuntimeMigrationRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[User, Depends(require_admin)],
) -> RuntimeMigrationResponse:
    """
    由管理员启动工具账户运行时迁移

    :param tool_account_id (UUID): 工具账户 ID
    :param payload (RuntimeMigrationRequest): 迁移请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param admin (User): 当前管理员

    :return RuntimeMigrationResponse: 迁移任务数据
    """

    result = await ToolAccountService(session, settings).migrate_runtime(
        actor=admin,
        account_id=tool_account_id,
        target_backend=payload.target_runtime_backend,
    )
    return RuntimeMigrationResponse(data=result, request_id=get_request_id())


def tool_account_data(account: ToolAccount) -> ToolAccountData:
    """
    转换工具账户响应数据

    :param account (ToolAccount): 工具账户实体

    :return ToolAccountData: 工具账户响应数据
    """

    return ToolAccountData(
        id=account.id,
        user_id=account.user_id,
        tool_type=account.tool_type,
        display_name=account.display_name,
        status=account.status,
        region_code=account.region_code,
        timezone=account.timezone,
        locale=account.locale,
        preferred_node_tags=account.preferred_node_tags,
        affinity_node_id=account.affinity_node_id,
        runtime_backend=account.runtime_backend,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


@router.get("", response_model=ToolAccountListResponse)
async def list_tool_accounts(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> ToolAccountListResponse:
    """
    列出工具账户

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return ToolAccountListResponse: 工具账户列表响应
    """

    accounts = await ToolAccountService(session, settings).list_accounts(user=user)
    return ToolAccountListResponse(
        data=ToolAccountListData(items=[tool_account_data(account) for account in accounts]),
        request_id=get_request_id(),
    )


@router.post("", response_model=ToolAccountResponse)
async def create_tool_account(
    payload: CreateToolAccountRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> ToolAccountResponse:
    """
    创建工具账户

    :param payload (CreateToolAccountRequest): 创建请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return ToolAccountResponse: 工具账户响应
    """

    account = await ToolAccountService(session, settings).create_account(
        user=user,
        tool_type=payload.tool_type,
        display_name=payload.display_name,
        region_code=payload.region_code,
        timezone=payload.timezone,
        locale=payload.locale,
        preferred_node_tags=payload.preferred_node_tags,
    )
    return ToolAccountResponse(data=tool_account_data(account), request_id=get_request_id())


@router.get("/{tool_account_id}", response_model=ToolAccountResponse)
async def get_tool_account(
    tool_account_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> ToolAccountResponse:
    """
    读取工具账户

    :param tool_account_id (UUID): 工具账户 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return ToolAccountResponse: 工具账户响应
    """

    account = await ToolAccountService(session, settings).get_account(
        user=user,
        account_id=tool_account_id,
    )
    return ToolAccountResponse(data=tool_account_data(account), request_id=get_request_id())


@router.patch("/{tool_account_id}", response_model=ToolAccountResponse)
async def update_tool_account(
    tool_account_id: UUID,
    payload: UpdateToolAccountRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> ToolAccountResponse:
    """
    更新工具账户

    :param tool_account_id (UUID): 工具账户 ID
    :param payload (UpdateToolAccountRequest): 更新请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return ToolAccountResponse: 工具账户响应
    """

    account = await ToolAccountService(session, settings).update_account(
        user=user,
        account_id=tool_account_id,
        display_name=payload.display_name,
        status=payload.status,
        region_code=payload.region_code,
        timezone=payload.timezone,
        locale=payload.locale,
        preferred_node_tags=payload.preferred_node_tags,
    )
    return ToolAccountResponse(data=tool_account_data(account), request_id=get_request_id())


@router.post("/{tool_account_id}/bind/start", response_model=BindingStatusResponse)
async def start_tool_account_binding(
    tool_account_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> BindingStatusResponse:
    """
    启动工具账户绑定

    :param tool_account_id (UUID): 工具账户 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return BindingStatusResponse: 绑定状态响应
    """

    binding = await ToolAccountService(session, settings).start_binding(
        user=user,
        token=token,
        account_id=tool_account_id,
    )
    return BindingStatusResponse(data=binding.status, request_id=get_request_id())


@router.get("/{tool_account_id}/bind/status", response_model=BindingStatusResponse)
async def get_tool_account_binding_status(
    tool_account_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> BindingStatusResponse:
    """
    读取工具账户绑定状态

    :param tool_account_id (UUID): 工具账户 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return BindingStatusResponse: 绑定状态响应
    """

    status = await ToolAccountService(session, settings).get_binding_status(
        user=user,
        account_id=tool_account_id,
    )
    return BindingStatusResponse(data=status, request_id=get_request_id())


@router.post("/{tool_account_id}/bind/verify", response_model=BindingStatusResponse)
async def verify_tool_account_binding(
    tool_account_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> BindingStatusResponse:
    """
    校验工具账户绑定

    :param tool_account_id (UUID): 工具账户 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return BindingStatusResponse: 绑定状态响应
    """

    binding = await ToolAccountService(session, settings).verify_binding(
        user=user,
        account_id=tool_account_id,
    )
    return BindingStatusResponse(data=binding.status, request_id=get_request_id())


@router.post("/{tool_account_id}/disable", response_model=ToolAccountResponse)
async def disable_tool_account(
    tool_account_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> ToolAccountResponse:
    """
    禁用工具账户

    :param tool_account_id (UUID): 工具账户 ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户

    :return ToolAccountResponse: 工具账户响应
    """

    account = await ToolAccountService(session, settings).disable_account(
        user=user,
        account_id=tool_account_id,
    )
    return ToolAccountResponse(data=tool_account_data(account), request_id=get_request_id())


@router.post("/{tool_account_id}/config-imports", response_model=ToolAccountConfigImportResponse)
async def create_tool_account_config_import(
    tool_account_id: UUID,
    payload: ToolAccountConfigImportRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> ToolAccountConfigImportResponse:
    """
    创建工具账户配置导入计划
    """

    result = await ToolAccountService(session, settings).plan_config_import(
        user=user,
        account_id=tool_account_id,
        tool_type=payload.tool_type,
        include=payload.include,
        exclude=payload.exclude,
        files=payload.files,
        include_resume_history=payload.include_resume_history,
        dry_run=payload.dry_run,
    )
    return ToolAccountConfigImportResponse(data=result, request_id=get_request_id())


@router.post(
    "/{tool_account_id}/developer-credential-profile",
    response_model=DeveloperCredentialProfileResponse,
)
async def bind_developer_credential_profile(
    tool_account_id: UUID,
    payload: BindDeveloperCredentialProfileRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> DeveloperCredentialProfileResponse:
    """
    绑定开发凭据 profile
    """

    profile = await DeveloperCredentialService(session, settings).bind_to_tool_account(
        user=user,
        account_id=tool_account_id,
        profile_id=payload.profile_id,
    )
    return DeveloperCredentialProfileResponse(
        data=profile_data(profile), request_id=get_request_id()
    )


@router.delete("/{tool_account_id}/developer-credential-profile", status_code=204)
async def unbind_developer_credential_profile(
    tool_account_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> None:
    """
    解除开发凭据 profile 绑定
    """

    await DeveloperCredentialService(session, settings).unbind_from_tool_account(
        user=user,
        account_id=tool_account_id,
    )
