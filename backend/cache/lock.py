"""
基于 Redis 的互斥锁

三个必须做对的点，每一个都对应一类真实故障：

┌─ 1. 获取必须用 SET key token NX PX ttl，一条命令完成 ──────────┐
│  错误写法：先 EXISTS 再 SET。两条命令之间有窗口，                │
│  两个进程可能同时通过 EXISTS 检查，双双获得锁。                  │
│  NX（not exists）把"检查"与"设置"合成一次原子操作。              │
└──────────────────────────────────────────────────────────────┘

┌─ 2. 必须带过期时间（PX）────────────────────────────────────────┐
│  持有者崩溃或被 kill 时不会执行释放逻辑。没有 TTL 的锁            │
│  会永久残留，该资源此后再也无法被处理 —— 死锁。                  │
│  代价是：业务执行超过 TTL 时锁会提前释放，见下方"已知局限"。      │
└──────────────────────────────────────────────────────────────┘

┌─ 3. 释放必须校验归属，且校验与删除要原子 ──────────────────────┐
│  错误写法：直接 DEL key。若本进程的锁已因超时被释放、             │
│  并被另一个进程重新获取，DEL 会删掉别人的锁 ——                  │
│  于是第三个进程也能获得锁，互斥彻底失效。                        │
│  正确做法：SET 时写入唯一 token，释放时用 Lua 脚本               │
│  「比对 token 相同才删除」，Lua 在 Redis 中单线程执行，天然原子。 │
└──────────────────────────────────────────────────────────────┘

已知局限（必须清楚边界，否则会误用）：

- **单实例锁，非 Redlock。** 主从架构下，主节点确认 SET 后若在同步到
  从节点前宕机，故障转移后新主节点上没有这把锁，另一个进程能再次获得
  —— 此刻存在两个持有者。Redlock 用多个独立 Redis 实例的多数派确认来
  缓解，但本项目单实例部署，引入 Redlock 只增加复杂度而无实际收益。
- **不做锁续期（watchdog）。** 业务耗时超过 TTL 时锁会提前释放。
  正确的应对是把 TTL 设得足够长，而不是加续期线程 —— 续期线程本身
  会在进程卡死（而非崩溃）时继续续期，把锁变成死锁。
- **因此本锁只用于降低重复劳动，不用于保证唯一性。** 真正的唯一性由
  业务逻辑的幂等保证（文档摄入按文件名先删后插，重复执行结果相同）。
  把 Redis 锁当唯一性保证是分布式系统里最常见的错误之一。
- **Redis 不可用时降级为"获取成功"**，即无锁运行。理由同上：
  锁不是正确性的必要条件，而拒绝服务是更坏的结果。
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from redis.exceptions import RedisError

from . import redis_client

logger = logging.getLogger(__name__)

# 释放锁的 Lua 脚本：比对 token 与删除必须原子完成。
# Redis 单线程执行 Lua，脚本内不会被其他命令插入。
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# 默认持有时长。文档摄入（解析 + embedding）在慢盘上可能几十秒，
# 取 60s 留足余量 —— 宁可锁多持有一会儿，也不要中途被抢。
DEFAULT_TTL_MS = 60_000


class RedisLock:
    """一次性互斥锁。不可重入，不自动续期。

    典型用法用 `guard()` 上下文管理器，它保证异常路径也会释放。
    """

    def __init__(self, key: str, ttl_ms: int = DEFAULT_TTL_MS):
        if ttl_ms <= 0:
            raise ValueError(f"ttl_ms 必须 > 0，当前为 {ttl_ms}")
        self.key = key
        self.ttl_ms = ttl_ms
        # token 标识"这把锁是谁的"，释放时据此校验归属。
        # 用 uuid4 而非进程 id：同进程可能先后获取同一 key 的锁，
        # 若 token 相同，前一次的延迟释放会误删后一次的锁。
        self.token = str(uuid.uuid4())
        self._acquired = False
        self._degraded = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def degraded(self) -> bool:
        """是否处于降级（无锁）状态。调用方可据此决定是否记日志。"""
        return self._degraded

    async def acquire(self) -> bool:
        """尝试获取锁，不阻塞不重试。

        返回:
            True  —— 获得锁，或 Redis 不可用而降级放行
            False —— 锁已被他人持有

        不做阻塞等待：调用方（摄入 worker）遇到冲突时应当跳过或稍后重试，
        而不是占着协程等待。阻塞式获取还需要处理超时与惊群，
        复杂度不值得。
        """
        client = redis_client.get_client()
        if client is None:
            self._degraded = True
            self._acquired = True
            logger.debug("Redis 未启用，锁降级放行: %s", self.key)
            return True

        try:
            # nx=True 对应 NX，px 对应 PX。一条命令内完成"不存在则设置并加过期"
            ok = await client.set(self.key, self.token, nx=True, px=self.ttl_ms)
            self._acquired = bool(ok)
            if not self._acquired:
                logger.debug("锁已被占用: %s", self.key)
            return self._acquired
        except RedisError as exc:
            # 连接异常时降级：与 Redis 未启用同处理
            self._degraded = True
            self._acquired = True
            logger.warning("获取锁失败，降级放行 key=%s: %s", self.key, exc)
            return True

    async def release(self) -> bool:
        """释放锁。只在 token 匹配时才删除。

        返回:
            True  —— 成功删除自己的锁
            False —— 未持有、已过期被他人获取、或降级状态

        返回 False 不是错误：锁因超时被释放后由他人获取，
        此时本进程本就不该删除它。这个返回值可用于监控
        「业务耗时超过锁 TTL」的频率。
        """
        if not self._acquired:
            return False

        if self._degraded:
            self._acquired = False
            return False

        client = redis_client.get_client()
        if client is None:
            self._acquired = False
            return False

        try:
            removed = await client.eval(_RELEASE_SCRIPT, 1, self.key, self.token)
            self._acquired = False
            if not removed:
                # 说明锁已不属于自己 —— 业务耗时超过了 TTL。
                # 这是需要关注的信号：可能有并发重复执行。
                logger.warning(
                    "释放锁时发现已失去持有权（业务耗时可能超过 %sms）: %s",
                    self.ttl_ms, self.key,
                )
            return bool(removed)
        except RedisError as exc:
            self._acquired = False
            logger.warning("释放锁失败 key=%s: %s", self.key, exc)
            return False


@asynccontextmanager
async def guard(
    key: str,
    ttl_ms: int = DEFAULT_TTL_MS,
) -> AsyncIterator[bool]:
    """锁的上下文管理器。

    用法::

        async with guard(f"lock:ingest:{filename}") as got:
            if not got:
                return  # 他人正在处理
            ...业务...

    yield 布尔值而非抛异常：获取失败是正常的业务分支
    （另一个 worker 正在处理），不是异常情况。
    用异常表达会迫使调用方写 try/except 来处理正常流程。

    无论业务是否抛异常，退出时都会尝试释放 —— 这是用上下文管理器
    而非手工 acquire/release 的理由。
    """
    lock = RedisLock(key, ttl_ms=ttl_ms)
    got = await lock.acquire()
    try:
        yield got
    finally:
        if got:
            await lock.release()
