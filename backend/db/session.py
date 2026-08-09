"""
数据库连接与会话管理

engine 与 sessionmaker 是进程级单例：连接池必须复用，
每请求新建 engine 会耗尽数据库连接数（PG 默认上限 100）。

session 则必须每请求一个：SQLAlchemy 的 AsyncSession 不是线程/任务安全的，
共享会导致并发请求的事务互相污染。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import config
from .models import Base

logger = logging.getLogger(__name__)

_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def _create_engine() -> AsyncEngine:
    url = config.DATABASE_URL
    is_sqlite = url.startswith("sqlite")

    kwargs = {
        # 生产环境不要打开 echo：它会把每条 SQL 连参数一起打进日志，
        # 既刷屏又可能泄露用户查询内容
        "echo": False,
        # 连接在池里放太久会被数据库单方面关闭，取用时报
        # "server closed the connection unexpectedly"。
        # pre_ping 在每次取用前发一个轻量探测，代价远小于请求失败。
        "pool_pre_ping": True,
    }

    if is_sqlite:
        # SQLite 不支持连接池的并发语义，aiosqlite 用单连接串行化。
        # 这也是它只适合单副本开发的原因。
        logger.info("使用 SQLite（单副本开发）。多副本部署需改用 PostgreSQL。")
    else:
        kwargs.update({
            "pool_size": 10,
            # 突发流量时允许临时超出 pool_size，但总数受限，
            # 避免把数据库连接数打满
            "max_overflow": 5,
            # 主动回收超过 30 分钟的连接，绕开中间件（如 pgbouncer）
            # 的空闲超时
            "pool_recycle": 1800,
        })

    return create_async_engine(url, **kwargs)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = _create_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(),
            # 提交后不过期对象属性：否则读取已提交对象的字段会触发
            # 新的 SELECT，而此时 session 可能已关闭
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """事务边界。正常退出提交，异常回滚。

    用上下文管理器而非手工 commit/rollback：漏掉 rollback 会让
    连接带着失败的事务回到池里，后续请求拿到它会报
    "current transaction is aborted"。
    """
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入用。用法：session: AsyncSession = Depends(get_session)"""
    async with session_scope() as session:
        yield session


async def init_models() -> None:
    """建表。

    仅用于本地开发与测试 —— 生产用 Alembic 迁移。
    两者并存的风险是 schema 漂移：create_all 不会更新已存在的表结构，
    改了模型却只跑 create_all 会得到旧表。所以这里只在表不存在时有效。
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("数据库表已就绪: %s", config.DATABASE_URL.split("://")[0])


async def dispose_engine() -> None:
    """关闭连接池。在应用 lifespan 结束时调用。

    不关会导致进程退出时报 "Event loop is closed" ——
    连接的析构逻辑跑在已关闭的事件循环上。
    """
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
        logger.info("数据库连接池已关闭")


async def healthcheck() -> bool:
    """探测数据库可用性，供 /health 端点使用。"""
    from sqlalchemy import text

    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - 健康检查不应抛异常
        logger.warning("数据库健康检查失败: %s", exc)
        return False
