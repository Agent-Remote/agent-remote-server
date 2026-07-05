from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import get_current_user, get_session, get_settings
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.models import BrowserSession, User
from agent_remote_server.schemas.auth import EmptyResponse
from agent_remote_server.schemas.browser_sessions import (
    BrowserConnectInfoData,
    BrowserConnectInfoResponse,
    BrowserSessionData,
    BrowserSessionListData,
    BrowserSessionListResponse,
    BrowserSessionResponse,
    CreateBrowserSessionRequest,
)
from agent_remote_server.services.browser_sessions import BrowserSessionService

router = APIRouter(prefix="/browser-sessions", tags=["browser-sessions"])


def browser_session_data(browser_session: BrowserSession) -> BrowserSessionData:
    """
    转换浏览器 session 响应数据

    :param browser_session (BrowserSession): 浏览器 session 实体
    :return BrowserSessionData: 浏览器 session 响应数据
    """

    create_task_id = f"create_browser_session:{browser_session.id}"
    stop_task_id = f"stop_browser_session:{browser_session.id}"
    return BrowserSessionData(
        id=browser_session.id,
        user_id=browser_session.user_id,
        tool_account_id=browser_session.tool_account_id,
        node_id=browser_session.node_id,
        status=browser_session.status,
        region_code=browser_session.region_code,
        timezone=browser_session.timezone,
        locale=browser_session.locale,
        target_url=browser_session.target_url,
        container_id=browser_session.container_id,
        ttl_seconds=browser_session.ttl_seconds,
        expires_at=browser_session.expires_at,
        stopped_at=browser_session.stopped_at,
        create_task_id=create_task_id,
        stop_task_id=stop_task_id if browser_session.status == "stopping" else None,
        created_at=browser_session.created_at,
        updated_at=browser_session.updated_at,
    )


@router.get("", response_model=BrowserSessionListResponse)
async def list_browser_sessions(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> BrowserSessionListResponse:
    """
    列出远端临时浏览器 session

    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :return BrowserSessionListResponse: 浏览器 session 列表响应
    """

    items = await BrowserSessionService(session, settings).list_browser_sessions(user=user)
    return BrowserSessionListResponse(
        data=BrowserSessionListData(items=[browser_session_data(item) for item in items]),
        request_id=get_request_id(),
    )


@router.post("", response_model=BrowserSessionResponse)
async def create_browser_session(
    payload: CreateBrowserSessionRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> BrowserSessionResponse:
    """
    创建远端临时浏览器 session

    :param payload (CreateBrowserSessionRequest): 创建请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :return BrowserSessionResponse: 浏览器 session 响应
    """

    browser_session = await BrowserSessionService(session, settings).create_browser_session(
        user=user,
        tool_account_id=payload.tool_account_id,
        target_url=str(payload.target_url) if payload.target_url is not None else None,
        region_code=payload.region_code,
        timezone=payload.timezone,
        locale=payload.locale,
        ttl_seconds=payload.ttl_seconds,
    )
    return BrowserSessionResponse(
        data=browser_session_data(browser_session),
        request_id=get_request_id(),
    )


@router.get("/{browser_session_id}", response_model=BrowserSessionResponse)
async def get_browser_session(
    browser_session_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> BrowserSessionResponse:
    """
    读取远端临时浏览器 session

    :param browser_session_id (UUID): 浏览器 session ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :return BrowserSessionResponse: 浏览器 session 响应
    """

    browser_session = await BrowserSessionService(session, settings).get_browser_session(
        user=user,
        browser_session_id=browser_session_id,
    )
    return BrowserSessionResponse(
        data=browser_session_data(browser_session),
        request_id=get_request_id(),
    )


@router.get("/{browser_session_id}/stream", response_class=HTMLResponse)
async def browser_stream(
    browser_session_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    token: Annotated[str, Query(min_length=1)],
) -> HTMLResponse:
    """
    打开远端临时浏览器内嵌页面

    :param browser_session_id (UUID): 浏览器 session ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param token (str): 短期连接 token
    :return HTMLResponse: 内嵌页面
    """

    html = await BrowserSessionService(session, settings).stream_html(
        browser_session_id=browser_session_id,
        token=token,
    )
    return HTMLResponse(html)


@router.post("/{browser_session_id}/connect-info", response_model=BrowserConnectInfoResponse)
async def browser_connect_info(
    browser_session_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> BrowserConnectInfoResponse:
    """
    签发远端浏览器短期内嵌连接信息

    :param browser_session_id (UUID): 浏览器 session ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :return BrowserConnectInfoResponse: 连接信息响应
    """

    info = await BrowserSessionService(session, settings).connect_info(
        user=user,
        browser_session_id=browser_session_id,
    )
    return BrowserConnectInfoResponse(
        data=BrowserConnectInfoData(
            browser_session_id=info.browser_session_id,
            status=info.status,
            embed_url=info.embed_url,
            expires_at=info.expires_at,
        ),
        request_id=get_request_id(),
    )


@router.post("/{browser_session_id}/stop", response_model=EmptyResponse)
async def stop_browser_session(
    browser_session_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> EmptyResponse:
    """
    停止远端临时浏览器 session

    :param browser_session_id (UUID): 浏览器 session ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :return EmptyResponse: 空响应
    """

    await BrowserSessionService(session, settings).stop_browser_session(
        user=user,
        browser_session_id=browser_session_id,
    )
    return EmptyResponse(request_id=get_request_id())
