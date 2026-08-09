"""缓存与协调层：Redis 客户端、分布式锁。

全部功能在 Redis 不可用时降级运行 —— Redis 不是事实来源。
"""

from .lock import RedisLock, guard
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
    "RedisLock",
    "guard",
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
