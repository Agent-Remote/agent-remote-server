from functools import lru_cache

from pydantic import Field
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
    cli_login_ttl_seconds: int = Field(default=600, description="CLI 登录码有效秒数")
    cli_login_poll_interval_seconds: int = Field(default=5, description="CLI 登录轮询间隔秒数")
    node_task_lease_seconds: int = Field(default=30, description="节点任务租约秒数")
    node_offline_after_seconds: int = Field(default=120, description="节点离线判定秒数")


@lru_cache
def get_settings() -> Settings:
    """
    获取缓存后的应用配置

    :return Settings: 应用配置实例
    """

    return Settings()
