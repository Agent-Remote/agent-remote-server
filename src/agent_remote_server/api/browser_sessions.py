import asyncio
import base64
import ssl
from collections.abc import Sequence
from typing import Annotated, Any, cast
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from uuid import UUID

import httpx
import websockets
from fastapi import APIRouter, Depends, Query, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin, Subprotocol

from agent_remote_server.api.deps import get_current_user, get_session, get_settings
from agent_remote_server.config import Settings
from agent_remote_server.context import get_request_id
from agent_remote_server.db import create_session_factory
from agent_remote_server.errors import ApiError
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

STREAM_COOKIE_PREFIX = "ar_browser_embed_"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}


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
) -> RedirectResponse:
    """
    打开远端临时浏览器代理入口

    :param browser_session_id (UUID): 浏览器 session ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param token (str): 短期连接 token
    :return RedirectResponse: 带目录语义的代理入口
    """

    endpoint = await BrowserSessionService(session, settings).stream_endpoint(
        browser_session_id=browser_session_id,
        token=token,
    )
    query_items = [("token", token)]
    if endpoint is not None:
        upstream = _UpstreamEndpoint(endpoint)
        query_items.extend(_browser_connect_query(browser_session_id, upstream, token))
    return RedirectResponse(
        url=(
            f"/api/v1/browser-sessions/{browser_session_id}/stream/?"
            f"{urlencode(query_items, doseq=True, quote_via=quote)}"
        ),
        status_code=307,
    )


@router.api_route(
    "/{browser_session_id}/stream/{proxy_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def browser_stream_proxy(
    browser_session_id: UUID,
    proxy_path: str,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """
    代理远端 Kasm 浏览器 HTTP 请求

    :param browser_session_id (UUID): 浏览器 session ID
    :param proxy_path (str): 代理路径
    :param request (Request): 原始请求
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :return Response: 上游响应
    """

    token = _stream_token(browser_session_id, request)
    service = BrowserSessionService(session, settings)
    endpoint = await service.stream_endpoint(browser_session_id=browser_session_id, token=token)
    if endpoint is None:
        html = await service.stream_html(browser_session_id=browser_session_id, token=token)
        return HTMLResponse(html)
    upstream = _UpstreamEndpoint(endpoint)
    upstream_url = _upstream_http_url(upstream, browser_session_id, proxy_path, request, token)
    async with httpx.AsyncClient(verify=False, follow_redirects=False, timeout=30.0) as client:
        upstream_response = await client.request(
            request.method,
            upstream_url,
            headers=_proxy_request_headers(request, upstream.basic_authorization),
            content=await request.body(),
        )
    response = Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=_proxy_response_headers(upstream_response),
        media_type=upstream_response.headers.get("content-type"),
    )
    if "token" in request.query_params:
        response.set_cookie(
            key=_stream_cookie_name(browser_session_id),
            value=token,
            max_age=300,
            httponly=True,
            samesite="lax",
            path=f"/api/v1/browser-sessions/{browser_session_id}/stream",
        )
    return response


@router.websocket("/{browser_session_id}/stream/{proxy_path:path}")
async def browser_stream_websocket(
    browser_session_id: UUID,
    proxy_path: str,
    websocket: WebSocket,
) -> None:
    """
    代理远端 Kasm 浏览器 WebSocket 请求

    :param browser_session_id (UUID): 浏览器 session ID
    :param proxy_path (str): 代理路径
    :param websocket (WebSocket): 浏览器 WebSocket
    """

    try:
        token = _stream_token(browser_session_id, websocket)
        settings = websocket.app.state.settings
        session_factory = getattr(websocket.app.state, "session_factory", None)
        if session_factory is None:
            session_factory = create_session_factory(settings)
            websocket.app.state.session_factory = session_factory
        async with session_factory() as session:
            endpoint = await BrowserSessionService(session, settings).stream_endpoint(
                browser_session_id=browser_session_id,
                token=token,
            )
        if endpoint is None:
            await websocket.close(code=1008)
            return
        upstream = _UpstreamEndpoint(endpoint)
        upstream_url = _upstream_websocket_url(upstream, proxy_path, websocket)
    except ApiError:
        await websocket.close(code=1008)
        return
    ssl_context = ssl._create_unverified_context() if upstream_url.startswith("wss://") else None
    try:
        async with websockets.connect(
            upstream_url,
            additional_headers={"authorization": upstream.basic_authorization},
            origin=cast(Origin, f"{upstream.scheme}://{upstream.netloc}"),
            subprotocols=_websocket_subprotocols(websocket),
            ssl=ssl_context,
            max_size=None,
        ) as upstream_socket:
            await websocket.accept(subprotocol=upstream_socket.subprotocol)
            await _proxy_websocket(websocket, upstream_socket)
    except ConnectionClosed:
        return
    except Exception:
        await websocket.close(code=1011)


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


@router.delete("/{browser_session_id}", response_model=EmptyResponse)
async def delete_browser_session(
    browser_session_id: UUID,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> EmptyResponse:
    """
    删除终态浏览器 session

    :param browser_session_id (UUID): 浏览器 session ID
    :param settings (Settings): 应用配置
    :param session (AsyncSession): 数据库会话
    :param user (User): 当前用户
    :return EmptyResponse: 空响应
    """

    await BrowserSessionService(session, settings).delete_browser_session(
        user=user, browser_session_id=browser_session_id
    )
    return EmptyResponse(request_id=get_request_id())


class _UpstreamEndpoint:
    """
    Kasm 浏览器上游连接信息
    """

    def __init__(self, endpoint: str) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ApiError(
                code="BROWSER_SESSION_CONNECT_DENIED",
                message="Browser stream endpoint is invalid.",
                status_code=502,
            )
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port
        self.username = parts.username or "kasm_user"
        self.password = parts.password or "agent-remote"
        self.base_path = parts.path if parts.path and parts.path != "/" else ""

    @property
    def netloc(self) -> str:
        """
        返回不包含凭证的 host:port

        :return str: 上游 netloc
        """

        if self.port is None:
            return self.host
        return f"{self.host}:{self.port}"

    @property
    def basic_authorization(self) -> str:
        """
        返回 Kasm Basic Auth 头

        :return str: Authorization 头值
        """

        raw = f"{self.username}:{self.password}".encode()
        return f"Basic {base64.b64encode(raw).decode()}"


async def _proxy_websocket(websocket: WebSocket, upstream_socket: Any) -> None:
    async def client_to_upstream() -> None:
        """
        转发浏览器 WebSocket 消息到 Kasm 上游
        """

        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                await upstream_socket.close()
                return
            if "bytes" in message and message["bytes"] is not None:
                await upstream_socket.send(message["bytes"])
            elif "text" in message and message["text"] is not None:
                await upstream_socket.send(message["text"])

    async def upstream_to_client() -> None:
        """
        转发 Kasm 上游 WebSocket 消息到浏览器
        """

        async for message in upstream_socket:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)

    tasks = {
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        task.result()


def _stream_token(browser_session_id: UUID, request: Request | WebSocket) -> str:
    token = request.query_params.get("token") or request.cookies.get(
        _stream_cookie_name(browser_session_id)
    )
    if not token:
        raise ApiError(
            code="BROWSER_SESSION_CONNECT_DENIED",
            message="Browser stream token is required.",
            status_code=403,
        )
    return token


def _stream_cookie_name(browser_session_id: UUID) -> str:
    return f"{STREAM_COOKIE_PREFIX}{str(browser_session_id).replace('-', '')[:24]}"


def _upstream_http_url(
    upstream: _UpstreamEndpoint,
    browser_session_id: UUID,
    proxy_path: str,
    request: Request,
    token: str,
) -> str:
    query_items = [
        (key, value) for key, value in request.query_params.multi_items() if key != "token"
    ]
    if proxy_path == "":
        query_items = _ensure_browser_connect_query(
            query_items=query_items,
            browser_session_id=browser_session_id,
            upstream=upstream,
            token=token,
        )
    return urlunsplit(
        (
            upstream.scheme,
            upstream.netloc,
            _join_upstream_path(upstream.base_path, proxy_path),
            urlencode(query_items, doseq=True),
            "",
        )
    )


def _upstream_websocket_url(
    upstream: _UpstreamEndpoint,
    proxy_path: str,
    websocket: WebSocket,
) -> str:
    query = urlencode(
        [(key, value) for key, value in websocket.query_params.multi_items() if key != "token"],
        doseq=True,
    )
    scheme = "wss" if upstream.scheme == "https" else "ws"
    return urlunsplit(
        (
            scheme,
            upstream.netloc,
            _join_upstream_path(upstream.base_path, proxy_path),
            query,
            "",
        )
    )


def _browser_connect_query(
    browser_session_id: UUID, upstream: _UpstreamEndpoint, token: str
) -> list[tuple[str, str]]:
    ws_path = f"api/v1/browser-sessions/{browser_session_id}/stream/websockify?token={token}"
    return [
        ("autoconnect", "1"),
        ("password", upstream.password),
        ("path", ws_path),
    ]


def _ensure_browser_connect_query(
    *,
    query_items: list[tuple[str, str]],
    browser_session_id: UUID,
    upstream: _UpstreamEndpoint,
    token: str,
) -> list[tuple[str, str]]:
    existing_keys = {key for key, _ in query_items}
    for key, value in _browser_connect_query(browser_session_id, upstream, token):
        if key not in existing_keys:
            query_items.append((key, value))
    return query_items


def _websocket_subprotocols(websocket: WebSocket) -> Sequence[Subprotocol] | None:
    raw_value = websocket.headers.get("sec-websocket-protocol")
    if raw_value is None:
        return None
    protocols = [
        cast(Subprotocol, value.strip()) for value in raw_value.split(",") if value.strip()
    ]
    return protocols or None


def _join_upstream_path(base_path: str, proxy_path: str) -> str:
    normalized_base = base_path.rstrip("/")
    normalized_proxy = proxy_path.lstrip("/")
    if not normalized_proxy:
        return normalized_base + "/" if normalized_base else "/"
    if normalized_base:
        return f"{normalized_base}/{normalized_proxy}"
    return f"/{normalized_proxy}"


def _proxy_request_headers(request: Request, authorization: str) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS | {"host", "cookie", "authorization"}
    }
    headers["authorization"] = authorization
    return headers


def _proxy_response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
