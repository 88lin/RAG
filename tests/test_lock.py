"""
backend/cache/lock.py 单元测试

这份测试同时是锁语义的规格说明。三组测试对应三类真实故障：

  TestMutualExclusion —— 两个进程同时获得锁（NX 缺失或先查后设）
  TestExpiry          —— 持有者崩溃导致死锁（TTL 缺失）
  TestOwnership       —— 误删他人的锁（释放时不校验 token）

用 fakeredis 而非 mock：锁的正确性依赖 SET NX PX 与 Lua 的真实语义，
mock 掉这些等于把被测逻辑替换成断言本身。
fakeredis 实现了 Redis 的命令语义与 Lua 解释器，能真实验证这些行为。
"""

import asyncio
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.cache import lock as lock_mod
from backend.cache.lock import RedisLock, guard


@pytest_asyncio.fixture
async def fake_redis(monkeypatch):
    """把 lock 模块使用的 Redis 客户端替换为 fakeredis。

    直接 patch redis_client.get_client 而非注入参数：
    锁的调用方（摄入 worker）不该被迫传一个客户端进来，
    生产代码保持简单，测试通过替换模块级依赖来隔离。
    """
    import fakeredis.aioredis

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(lock_mod.redis_client, "get_client", lambda: client)
    yield client
    await client.flushall()
    await client.aclose()


class TestMutualExclusion:
    """互斥：同一 key 同时只能有一个持有者。"""

    @pytest.mark.asyncio
    async def test_second_acquire_fails(self, fake_redis):
        first = RedisLock("lock:doc:a.md")
        second = RedisLock("lock:doc:a.md")

        assert await first.acquire() is True
        assert await second.acquire() is False

    @pytest.mark.asyncio
    async def test_different_keys_dont_block(self, fake_redis):
        """不同资源的锁互不影响。"""
        a = RedisLock("lock:doc:a.md")
        b = RedisLock("lock:doc:b.md")

        assert await a.acquire() is True
        assert await b.acquire() is True

    @pytest.mark.asyncio
    async def test_reacquire_after_release(self, fake_redis):
        first = RedisLock("lock:doc:a.md")
        await first.acquire()
        assert await first.release() is True

        second = RedisLock("lock:doc:a.md")
        assert await second.acquire() is True

    @pytest.mark.asyncio
    async def test_concurrent_acquire_only_one_wins(self, fake_redis):
        """并发获取时只有一个成功。

        这是 SET NX 存在的理由：若实现为「先 EXISTS 再 SET」，
        多个协程会同时通过 EXISTS 检查而双双获得锁。
        """
        locks = [RedisLock("lock:hot") for _ in range(20)]
        results = await asyncio.gather(*(item.acquire() for item in locks))
        assert sum(results) == 1


class TestExpiry:
    """过期：持有者崩溃后锁必须能自动释放，否则死锁。"""

    @pytest.mark.asyncio
    async def test_ttl_is_set(self, fake_redis):
        """锁必须带 TTL —— 无过期的锁在持有者崩溃后永久残留。"""
        item = RedisLock("lock:doc:a.md", ttl_ms=5000)
        await item.acquire()

        ttl = await fake_redis.pttl("lock:doc:a.md")
        assert 0 < ttl <= 5000

    @pytest.mark.asyncio
    async def test_acquirable_after_expiry(self, fake_redis):
        """过期后他人可获取，模拟持有者崩溃未释放的情形。"""
        crashed = RedisLock("lock:doc:a.md", ttl_ms=50)
        assert await crashed.acquire() is True
        # 不调用 release，模拟进程被 kill

        await asyncio.sleep(0.08)

        rescuer = RedisLock("lock:doc:a.md")
        assert await rescuer.acquire() is True

    @pytest.mark.asyncio
    async def test_invalid_ttl_rejected(self):
        """ttl <= 0 会让锁立即过期或永不过期，两者都是错的，
        在构造时就拒绝而不是等到运行时才发现。"""
        with pytest.raises(ValueError):
            RedisLock("lock:x", ttl_ms=0)
        with pytest.raises(ValueError):
            RedisLock("lock:x", ttl_ms=-1)


class TestOwnership:
    """归属校验：绝不能删掉他人的锁。"""

    @pytest.mark.asyncio
    async def test_release_only_own_lock(self, fake_redis):
        """核心回归测试。

        场景：A 持有锁但业务超时导致锁过期，B 获取了锁，
        此时 A 才执行释放。若直接 DEL，A 会删掉 B 的锁，
        于是 C 也能获得锁 —— 互斥彻底失效。
        """
        holder_a = RedisLock("lock:doc:a.md", ttl_ms=50)
        await holder_a.acquire()
        await asyncio.sleep(0.08)  # A 的锁过期

        holder_b = RedisLock("lock:doc:a.md", ttl_ms=5000)
        assert await holder_b.acquire() is True

        # A 迟到的释放不应删除 B 的锁
        assert await holder_a.release() is False

        # 验证 B 的锁仍在：第三方无法获取
        holder_c = RedisLock("lock:doc:a.md")
        assert await holder_c.acquire() is False

    @pytest.mark.asyncio
    async def test_tokens_are_unique(self, fake_redis):
        """每把锁的 token 必须不同。

        若用进程 id 之类的固定值作 token，同进程先后获取同一 key
        时，前一次的迟到释放会误删后一次的锁。
        """
        tokens = {RedisLock("lock:x").token for _ in range(100)}
        assert len(tokens) == 100

    @pytest.mark.asyncio
    async def test_release_without_acquire(self, fake_redis):
        """未持有时释放应返回 False 而非抛异常。"""
        item = RedisLock("lock:doc:a.md")
        assert await item.release() is False

    @pytest.mark.asyncio
    async def test_double_release_is_safe(self, fake_redis):
        """重复释放不应误删后续持有者的锁。"""
        first = RedisLock("lock:doc:a.md")
        await first.acquire()
        assert await first.release() is True
        assert await first.release() is False

        second = RedisLock("lock:doc:a.md")
        assert await second.acquire() is True
        # first 的第三次释放不能影响 second
        assert await first.release() is False
        assert await fake_redis.get("lock:doc:a.md") == second.token


class TestGuard:
    """上下文管理器：保证异常路径也释放。"""

    @pytest.mark.asyncio
    async def test_releases_on_success(self, fake_redis):
        async with guard("lock:doc:a.md") as got:
            assert got is True
        assert await fake_redis.get("lock:doc:a.md") is None

    @pytest.mark.asyncio
    async def test_releases_on_exception(self, fake_redis):
        """业务抛异常时锁仍须释放，否则一次报错会锁死该资源直到 TTL。"""
        with pytest.raises(RuntimeError):
            async with guard("lock:doc:a.md") as got:
                assert got is True
                raise RuntimeError("业务失败")

        assert await fake_redis.get("lock:doc:a.md") is None

    @pytest.mark.asyncio
    async def test_yields_false_when_held(self, fake_redis):
        """已被占用时 yield False 而非抛异常 ——
        获取失败是正常业务分支（他人正在处理），不是异常情况。"""
        blocker = RedisLock("lock:doc:a.md", ttl_ms=5000)
        await blocker.acquire()

        async with guard("lock:doc:a.md") as got:
            assert got is False

        # 未获得锁时不应释放他人的锁
        assert await fake_redis.get("lock:doc:a.md") == blocker.token


class TestDegradation:
    """降级：Redis 不可用时放行而非拒绝服务。

    设计前提是"锁只降低重复劳动，不保证唯一性"，
    唯一性由业务幂等（摄入按文件名先删后插）保证。
    因此 Redis 挂掉时放行是正确选择 —— 拒绝服务是更坏的结果。
    """

    @pytest.mark.asyncio
    async def test_acquire_succeeds_without_redis(self, monkeypatch):
        monkeypatch.setattr(lock_mod.redis_client, "get_client", lambda: None)

        item = RedisLock("lock:doc:a.md")
        assert await item.acquire() is True
        assert item.degraded is True

    @pytest.mark.asyncio
    async def test_all_acquire_when_degraded(self, monkeypatch):
        """降级时不提供互斥 —— 这是明确的取舍，不是 bug。"""
        monkeypatch.setattr(lock_mod.redis_client, "get_client", lambda: None)

        results = [await RedisLock("lock:same").acquire() for _ in range(5)]
        assert all(results)

    @pytest.mark.asyncio
    async def test_release_when_degraded(self, monkeypatch):
        """降级状态下释放返回 False（没有真实的锁可删），且不抛异常。"""
        monkeypatch.setattr(lock_mod.redis_client, "get_client", lambda: None)

        item = RedisLock("lock:doc:a.md")
        await item.acquire()
        assert await item.release() is False

    @pytest.mark.asyncio
    async def test_guard_works_when_degraded(self, monkeypatch):
        monkeypatch.setattr(lock_mod.redis_client, "get_client", lambda: None)

        async with guard("lock:doc:a.md") as got:
            assert got is True
