"""测试夹具

核心问题：`backend/db/session.py` 的 engine 与 sessionmaker 是**进程级全局单例**
（连接池必须复用）。测试要换库就必须连同这两个全局一起重置，
否则第一个测试建的 engine 会被后面所有测试沿用，`DATABASE_URL` 改了也没用。

因此每个用到数据库的测试拿一个独立的临时 SQLite 文件，用完清干净。
不用 `sqlite+aiosqlite:///:memory:` —— 内存库随连接消亡，
而连接池取到不同连接时会看到不同的空库，表现为"表不存在"这种假失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    """指向临时目录的 SQLite URL。tmp_path 由 pytest 负责清理。"""
    return f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest_asyncio.fixture
async def db(monkeypatch, sqlite_url: str):
    """建好表的临时数据库。yield 出 session 模块本身，便于测试调其函数。

    退出时 dispose engine：不关连接池，Windows 上临时文件会因被占用
    而无法删除，pytest 清理 tmp_path 时报 PermissionError。
    """
    import config
    from backend.db import session as db_session

    monkeypatch.setattr(config, "DATABASE_URL", sqlite_url)

    # 重置全局单例，确保拿到指向临时库的新 engine
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_sessionmaker", None)

    await db_session.init_models()
    try:
        yield db_session
    finally:
        await db_session.dispose_engine()


@pytest.fixture
def redis_disabled(monkeypatch):
    """把 Redis 关掉，用于验证降级路径。

    降级是设计要求而非容错补丁：把 Redis 当强依赖，
    系统可用性上限就等于 Redis 的可用性。
    """
    import config
    from backend.cache import redis_client

    monkeypatch.setattr(config, "REDIS_ENABLED", False)
    monkeypatch.setattr(redis_client, "_client", None)
    return redis_client
