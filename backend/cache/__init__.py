"""缓存层：Redis 客户端。

全部功能在 Redis 不可用时降级运行 —— Redis 不是事实来源，
其中内容可丢弃后重建。
"""

from .redis_client import (
    KB_VERSION_KEY,
    bump_kb_version,
    close,
    delete,
    get_client,
    get_int,
    get_json,
    get_kb_version,
    hgetall,
    hset_mapping,
    incr,
    ping,
    set_json,
)

__all__ = [
    "KB_VERSION_KEY",
    "bump_kb_version",
    "close",
    "delete",
    "get_client",
    "get_int",
    "get_json",
    "get_kb_version",
    "hgetall",
    "hset_mapping",
    "incr",
    "ping",
    "set_json",
]
