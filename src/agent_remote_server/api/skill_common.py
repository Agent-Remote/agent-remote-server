"""
集中约束技能 API 身份、事务和不泄露内容的错误映射。
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from agent_remote_server.api.deps import (
    get_current_token,
    get_current_user,
    get_session,
    get_settings,
)
from agent_remote_server.config import Settings
from agent_remote_server.models import AuthToken, User
from agent_remote_server.schemas.skill_library import SkillLibraryRequest
from agent_remote_server.schemas.skill_results import SkillErrorView, SkillMutationData, SkillResult
from agent_remote_server.services.skills.content import SkillContentError, SkillContentService
from agent_remote_server.services.skills.library import SkillLibraryService
from agent_remote_server.skill_manager.storage.objects import PrivateObjectStore


@dataclass
class SkillApiContext:
    """
    由认证依赖构造，业务请求不能替换用户身份。
    """

    session: AsyncSession
    user: User
    settings: Settings

    def content(self) -> SkillContentService:
        """
        创建当前事务的私有内容服务。

        :return SkillContentService: 受用户授权约束的内容服务
        """
        return SkillContentService(
            self.session,
            PrivateObjectStore(self.settings.skill_storage_root),
            self.settings.skill_storage_policy,
        )

    def library(self) -> SkillLibraryService:
        """
        创建当前事务的用户库服务。

        :return SkillLibraryService: 库配置事务服务
        """
        return SkillLibraryService(
            self.session, PrivateObjectStore(self.settings.skill_storage_root), self.settings
        )

    async def execute(self, request: SkillLibraryRequest) -> SkillResult[SkillMutationData]:
        """
        命令成功受理后显式提交，任何异常由请求会话统一回滚。

        :param request (SkillLibraryRequest): 用户库命令
        :return SkillResult[SkillMutationData]: 已落库的受理结果
        """
        result = await self.library().execute(self.user.id, request)
        await self.session.commit()
        return result


def get_skill_context(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[User, Depends(get_current_user)],
    token: Annotated[AuthToken, Depends(get_current_token)],
) -> SkillApiContext:
    """
    本地登录用户才能管理技能，设备和节点凭据不能借用用户身份。

    :param session (AsyncSession): 请求事务
    :param settings (Settings): 部署配置
    :param user (User): 活跃认证用户
    :param token (AuthToken): 已验证凭据
    :return SkillApiContext: 可信的请求上下文
    """
    if token.token_type != "user":
        raise SkillContentError("USER_TOKEN_REQUIRED", "skill management requires a user token")
    if not settings.skill_manager_enabled:
        raise SkillContentError("SKILL_MANAGER_DISABLED", "skill management API is not enabled")
    return SkillApiContext(session, user, settings)


async def skill_error_handler(request: Request, error: Exception) -> JSONResponse:
    """
    将已知业务失败映射到稳定封套，不回显私有请求正文。

    :param request (Request): 当前 HTTP 请求
    :param error (Exception): 不含正文的业务错误
    :return JSONResponse: 稳定错误封套
    """
    if not isinstance(error, SkillContentError):
        raise error
    code = error.code
    status = 409
    if code.endswith("_NOT_FOUND"):
        status = 404
    elif code == "USER_TOKEN_REQUIRED":
        status = 403
    elif code == "SKILL_MANAGER_DISABLED":
        status = 503
    elif code == "QUOTA_EXCEEDED" or code == "CONTENT_TOO_LARGE":
        status = 413
    elif code in {"INVALID_REQUEST", "INVALID_PACKAGE", "INVALID_SKILL_FORMAT", "CONTENT_INVALID"}:
        status = 422
    body = SkillResult[None](
        status="failed",
        committed=False,
        data=None,
        errors=[SkillErrorView(code=code, message=str(error), details=error.details)],
    )
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))
