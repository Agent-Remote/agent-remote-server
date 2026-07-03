from typing import Literal

from pydantic import BaseModel, Field


class HealthComponent(BaseModel):
    """
    单个健康检查组件状态
    """

    status: Literal["ok", "error"] = Field(..., description="组件健康状态")
    latency_ms: float | None = Field(default=None, description="检查耗时毫秒数")
    error: str | None = Field(default=None, description="错误摘要")


class HealthResponse(BaseModel):
    """
    健康检查响应
    """

    status: Literal["ok", "degraded"] = Field(..., description="整体健康状态")
    service: str = Field(..., description="服务名称")
    version: str = Field(..., description="服务版本")
    environment: str = Field(..., description="运行环境")
    request_id: str | None = Field(default=None, description="请求 ID")
    components: dict[str, HealthComponent] = Field(default_factory=dict, description="组件状态映射")
