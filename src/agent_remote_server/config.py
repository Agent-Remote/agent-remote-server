from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    服务端运行配置
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = Field(default="agent-remote-server", description="应用名称")
    environment: str = Field(
        default="development",
        validation_alias="AGENT_REMOTE_ENV",
        description="运行环境",
    )
    public_base_url: str = Field(
        default="http://localhost:8000",
        validation_alias="PUBLIC_BASE_URL",
        description="公开访问基础地址",
    )
    cors_allowed_origins: list[str] = Field(
        default=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        validation_alias="CORS_ALLOWED_ORIGINS",
        description="允许跨域访问的前端来源",
    )
    database_url: str = Field(
        default="postgresql+asyncpg://agent_remote:agent_remote@localhost:5432/agent_remote",
        validation_alias="DATABASE_URL",
        description="PostgreSQL 异步连接地址",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        validation_alias="REDIS_URL",
        description="Redis 连接地址",
    )
    secret_key: str = Field(
        default="dev-only-change-me",
        validation_alias="AGENT_REMOTE_SECRET_KEY",
        description="应用加密主密钥",
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL", description="日志级别")
    request_id_header: str = Field(default="x-request-id", description="请求 ID 头名称")
    dependency_check_timeout_seconds: float = Field(default=2.0, description="依赖检查超时时间")
    access_token_ttl_seconds: int = Field(default=3600, description="访问令牌有效秒数")
    device_token_ttl_seconds: int = Field(
        default=2_592_000,
        description="设备令牌有效秒数",
    )
    cli_login_ttl_seconds: int = Field(default=600, description="CLI 登录码有效秒数")
    cli_login_poll_interval_seconds: int = Field(default=5, description="CLI 登录轮询间隔秒数")
    node_task_lease_seconds: int = Field(default=30, description="节点任务租约秒数")
    node_offline_after_seconds: int = Field(default=120, description="节点离线判定秒数")
    port_forwarding_enabled: bool = Field(default=True, description="是否允许 session 端口转发")
    port_forward_min_port: int = Field(default=1024, ge=1, le=65535, description="允许的最小端口")
    port_forward_max_port: int = Field(default=65535, ge=1, le=65535, description="允许的最大端口")
    port_forward_max_per_user: int = Field(default=10, ge=1, description="单用户最大转发数")
    port_forward_max_per_device: int = Field(default=10, ge=1, description="单设备最大转发数")
    port_forward_max_per_session: int = Field(default=5, ge=1, description="单 session 最大转发数")
    port_forward_max_streams: int = Field(
        default=128, ge=1, le=1024, description="单转发最大 stream 数"
    )
    port_forward_default_ttl_seconds: int = Field(
        default=28_800, ge=60, description="端口转发默认有效秒数"
    )
    port_forward_max_ttl_seconds: int = Field(
        default=86_400, ge=60, description="端口转发最大有效秒数"
    )
    port_forward_connection_token_ttl_seconds: int = Field(
        default=60, ge=10, description="一次性连接 token 有效秒数"
    )
    port_forward_lease_seconds: int = Field(default=60, ge=10, description="Node 授权租约秒数")
    port_forward_control_plane_grace_seconds: int = Field(
        default=300, ge=0, description="控制面不可用宽限秒数"
    )
    port_forward_bytes_per_second: int = Field(
        default=0, ge=0, description="端口转发每方向带宽上限"
    )
    port_forward_cleanup_interval_seconds: int = Field(
        default=30, ge=1, description="端口转发生命周期对账间隔秒数"
    )
    port_forward_create_rate_limit_per_minute: int = Field(
        default=30, ge=1, description="单用户设备每分钟创建转发上限"
    )
    port_forward_redeem_rate_limit_per_minute: int = Field(
        default=120, ge=1, description="单 Node 设备转发每分钟兑换上限"
    )

    @model_validator(mode="after")
    def validate_port_forward_policy(self) -> "Settings":
        """校验端口转发配置中的关联边界。"""

        if self.port_forward_min_port > self.port_forward_max_port:
            raise ValueError("port forward minimum port must not exceed maximum port")
        if self.port_forward_default_ttl_seconds > self.port_forward_max_ttl_seconds:
            raise ValueError("port forward default TTL must not exceed maximum TTL")
        return self


@lru_cache
def get_settings() -> Settings:
    """
    获取缓存后的应用配置

    :return Settings: 应用配置实例
    """

    return Settings()
