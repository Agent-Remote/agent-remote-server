import asyncio
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from agent_remote_server.config import Settings
from agent_remote_server.schemas.health import HealthComponent


class Base(DeclarativeBase):
    """
    SQLAlchemy 模型声明基类
    """

    pass


def create_engine(settings: Settings) -> AsyncEngine:
    """
    创建异步数据库引擎

    :param settings (Settings): 应用配置

    :return AsyncEngine: SQLAlchemy 异步引擎
    """

    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
    )


def create_session_factory(settings: Settings) -> async_sessionmaker:
    """
    创建异步数据库会话工厂

    :param settings (Settings): 应用配置

    :return async_sessionmaker: 异步会话工厂
    """

    return async_sessionmaker(
        bind=create_engine(settings),
        expire_on_commit=False,
    )


async def check_database(settings: Settings) -> HealthComponent:
    """
    检查 PostgreSQL 可用性

    :param settings (Settings): 应用配置

    :return HealthComponent: 数据库健康状态
    """

    started_at = time.perf_counter()
    engine = create_engine(settings)
    try:
        async with engine.connect() as connection:
            await asyncio.wait_for(
                connection.execute(text("select 1")),
                timeout=settings.dependency_check_timeout_seconds,
            )
    except Exception as exc:
        return HealthComponent(
            status="error",
            latency_ms=round((time.perf_counter() - started_at) * 1000, 3),
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        await engine.dispose()

    return HealthComponent(
        status="ok",
        latency_ms=round((time.perf_counter() - started_at) * 1000, 3),
    )
