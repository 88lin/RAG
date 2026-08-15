"""Alembic 运行环境

两个关键点：

1. **数据库地址只从 `config.DATABASE_URL` 取**，不写进 `alembic.ini`。
   配置的唯一入口是 `config.py`（CLAUDE.md 架构约束），而且这样
   `alembic.ini` 里不会出现密码，可以安全提交。

2. **引擎是异步的**（`sqlite+aiosqlite` / `postgresql+asyncpg`），
   而 Alembic 的迁移执行是同步 API。因此用 `AsyncEngine` + `run_sync`
   把同步的迁移逻辑跑在异步连接上，而不是为迁移单独维护一份同步驱动 ——
   两套驱动意味着两套连接参数，迟早漂移。
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config


def _force_utf8_stdio() -> None:
    """把 stdout/stderr 切成 UTF-8。

    Windows 的 GBK 控制台遇中文即 UnicodeEncodeError，而迁移说明是中文。

    **用 reconfigure，不要新建 TextIOWrapper 包住 `.buffer`。**
    后者会替换 `sys.stdout` 这个对象本身：在测试里以 API 方式调 alembic 时，
    它把 pytest 的捕获流顶掉，第二次调用拿到的是已关闭的 buffer，
    报 "I/O operation on closed file"。reconfigure 是就地改编码。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # 捕获流可能不是 TextIOWrapper。这种情况下输出不经过控制台，
            # 编码本就不是问题，跳过即可
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


_force_utf8_stdio()

# alembic 从项目根目录调用，但不保证根目录在 sys.path 上
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from backend.db.models import Base  # noqa: E402

alembic_config = context.config

if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

# autogenerate 的比对基准
target_metadata = Base.metadata


def get_url() -> str:
    return config.DATABASE_URL


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _configure_options(**kwargs) -> dict:
    """两种模式共用的配置。

    render_as_batch 对 SQLite 是必须的：SQLite 的 ALTER TABLE 只支持
    加列与改表名，改列类型/加约束都做不到。batch 模式会建新表、拷数据、
    删旧表、改名。在 PG 上开着它只会让迁移文件多一层 with 缩进，无害，
    但为了让两边生成的迁移形状一致，这里按方言区分。
    """
    url = get_url()
    return {
        # 类型变更也纳入 autogenerate 的比对范围。默认关闭是因为
        # 各方言的类型反射有噪声，但漏掉类型变更会让迁移与模型悄悄不一致。
        "compare_type": True,
        # 服务端默认值的变更同样纳入比对
        "compare_server_default": True,
        "render_as_batch": _is_sqlite(url),
        **kwargs,
    }


def run_migrations_offline() -> None:
    """离线模式：不建连接，把 SQL 打到标准输出。

    用于需要 DBA 审核 SQL 再执行的场景，本项目主要用在线模式。
    """
    context.configure(
        **_configure_options(
            url=get_url(),
            target_metadata=target_metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """在一个已建立的同步连接上跑迁移。由 run_sync 调用。"""
    context.configure(**_configure_options(
        connection=connection,
        target_metadata=target_metadata,
    ))
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = alembic_config.get_section(alembic_config.config_ini_section, {})
    section["sqlalchemy.url"] = get_url()

    engine = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        # 迁移是一次性的短命进程，连接池没有意义，
        # 而 NullPool 能确保退出时不留悬挂连接
        poolclass=pool.NullPool,
    )

    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)

    # 不 dispose 会在进程退出时报 "Event loop is closed"
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
