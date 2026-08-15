"""backend/cache/redis_client.py 单元测试

**测试重点是降级，不是 Redis 本身。** Redis 不是事实来源 ——
其中全部内容可丢弃后重建，因此每个调用点都必须能在 Redis 不可用时继续工作。
把 Redis 当强依赖的系统，其可用性上限等于 Redis 的可用性。

因此这里覆盖三种不可用形态，它们的失败方式不同：

1. `REDIS_ENABLED=false` —— 根本没配，`get_client()` 返回 None
2. 建客户端时抛异常 —— 配了但 URL 不合法
3. 调用时抛 `RedisError` —— 连上过但中途挂了（最常见，也最容易漏）

不连真实 Redis：用假客户端注入。真实实例属于集成测试，
而降级路径恰恰是"真实实例不存在"时的行为，本就不该依赖它。
"""

from __future__ import annotations

import json

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from backend.cache import redis_client


# ============================================================
# 假客户端
# ============================================================

class FakeRedis:
    """够用的内存假实现。只实现被测到的命令。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.hashes: dict[str, dict] = {}
        self.expires: dict[str, int] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        if ex is not None:
            self.expires[key] = ex
        return True

    async def incr(self, key):
        self.store[key] = str(int(self.store.get(key, 0)) + 1)
        return int(self.store[key])

    async def expire(self, key, ttl):
        self.expires[key] = ttl
        return True

    async def delete(self, *keys):
        return sum(1 for k in keys if self.store.pop(k, None) is not None)

    async def hset(self, key, mapping=None):
        self.hashes.setdefault(key, {}).update(mapping or {})
        return len(mapping or {})

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def ping(self):
        return True

    def pipeline(self, transaction=False):
        return FakePipeline(self)


class FakePipeline:
    """把命令排队，execute 时按序执行并返回结果列表。"""

    def __init__(self, client: FakeRedis):
        self.client = client
        self.queue: list[tuple[str, tuple, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.queue.append((name, args, kwargs))
            return self
        return record

    async def execute(self):
        results = []
        for name, args, kwargs in self.queue:
            results.append(await getattr(self.client, name)(*args, **kwargs))
        self.queue.clear()
        return results


class BrokenRedis(FakeRedis):
    """连上过但中途挂了 —— 每个命令都抛 RedisError。

    这是生产中最常见的形态：客户端对象存在、看着正常，
    直到真正发命令才失败。只测 `get_client() is None` 覆盖不到它。
    """

    def _boom(self, *args, **kwargs):
        raise RedisConnectionError("连接已断开")

    get = set = incr = expire = delete = hset = hgetall = ping = _boom

    def pipeline(self, transaction=False):
        raise RedisConnectionError("连接已断开")


@pytest.fixture
def fake(monkeypatch):
    """注入可用的假客户端。"""
    client = FakeRedis()
    monkeypatch.setattr(redis_client, "_client", client)
    monkeypatch.setattr(redis_client, "_warned_unavailable", False)
    import config
    monkeypatch.setattr(config, "REDIS_ENABLED", True)
    return client


@pytest.fixture
def broken(monkeypatch):
    """注入会抛异常的假客户端。"""
    client = BrokenRedis()
    monkeypatch.setattr(redis_client, "_client", client)
    monkeypatch.setattr(redis_client, "_warned_unavailable", False)
    import config
    monkeypatch.setattr(config, "REDIS_ENABLED", True)
    return client


# ============================================================
# get_client
# ============================================================

class TestGetClient:
    def test_returns_none_when_disabled(self, redis_disabled):
        """未启用时返回 None 而非抛异常。

        返回 Optional 是为了把判断收在一处 ——
        否则每个调用点都要写 try/except。
        """
        assert redis_client.get_client() is None

    def test_creation_failure_degrades_to_none(self, monkeypatch):
        """建客户端失败也要降级，不能让异常冒到请求处理里。"""
        import config

        monkeypatch.setattr(config, "REDIS_ENABLED", True)
        monkeypatch.setattr(redis_client, "_client", None)

        def boom(*args, **kwargs):
            raise ValueError("非法 URL")

        monkeypatch.setattr(redis_client.aioredis, "from_url", boom)
        assert redis_client.get_client() is None

    def test_client_is_reused(self, fake):
        """客户端复用 —— 每次新建会耗尽连接池。"""
        assert redis_client.get_client() is redis_client.get_client()


# ============================================================
# ping
# ============================================================

class TestPing:
    @pytest.mark.asyncio
    async def test_ping_ok(self, fake):
        assert await redis_client.ping() is True

    @pytest.mark.asyncio
    async def test_ping_false_when_disabled(self, redis_disabled):
        assert await redis_client.ping() is False

    @pytest.mark.asyncio
    async def test_ping_false_when_broken(self, broken):
        assert await redis_client.ping() is False

    @pytest.mark.asyncio
    async def test_warns_once_not_every_call(self, broken, monkeypatch):
        """连不上时只警告一次，否则每个请求刷一行日志。

        日志刷屏本身会造成故障：磁盘打满、真正有用的日志被冲走。
        """
        calls = []
        monkeypatch.setattr(
            redis_client.logger, "warning",
            lambda *args, **kwargs: calls.append(args),
        )
        for _ in range(5):
            await redis_client.ping()
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_warn_flag_resets_after_recovery(self, monkeypatch):
        """恢复后再次断开要能重新警告 —— 否则第二次故障静默无声。"""
        import config
        monkeypatch.setattr(config, "REDIS_ENABLED", True)
        monkeypatch.setattr(redis_client, "_warned_unavailable", True)
        monkeypatch.setattr(redis_client, "_client", FakeRedis())

        assert await redis_client.ping() is True
        assert redis_client._warned_unavailable is False


# ============================================================
# JSON 读写
# ============================================================

class TestJson:
    @pytest.mark.asyncio
    async def test_roundtrip(self, fake):
        payload = {"hits": [{"chunk_id": "c1", "score": 0.87}], "中文": "值"}
        assert await redis_client.set_json("k", payload) is True
        assert await redis_client.get_json("k") == payload

    @pytest.mark.asyncio
    async def test_chinese_not_escaped(self, fake):
        """ensure_ascii=False —— 转义后体积翻倍，且 redis-cli 里没法读。"""
        await redis_client.set_json("k", {"文档": "海事"})
        assert "海事" in fake.store["k"]

    @pytest.mark.asyncio
    async def test_miss_returns_none(self, fake):
        assert await redis_client.get_json("不存在") is None

    @pytest.mark.asyncio
    async def test_corrupt_value_treated_as_miss(self, fake):
        """脏数据（格式变更后的旧值）当未命中处理，新值写入时覆盖它。

        这里抛异常会让一次格式变更导致全站 500，
        而正确行为只是"这次缓存没用上"。
        """
        fake.store["k"] = "{不是合法 JSON"
        assert await redis_client.get_json("k") is None

    @pytest.mark.asyncio
    async def test_unserializable_value_returns_false(self, fake):
        """含不可序列化对象是调用方的 bug，但不该让请求失败。"""
        assert await redis_client.set_json("k", {"obj": object()}) is False

    @pytest.mark.asyncio
    async def test_default_ttl_applied(self, fake):
        """必须带 TTL：缓存无过期会持续增长直到打满内存，
        而 Redis 默认 noeviction 策略下写入会开始报错。"""
        import config
        await redis_client.set_json("k", {"a": 1})
        assert fake.expires["k"] == config.CACHE_TTL_RETRIEVAL

    @pytest.mark.asyncio
    async def test_explicit_ttl_overrides_default(self, fake):
        await redis_client.set_json("k", {"a": 1}, ttl=60)
        assert fake.expires["k"] == 60

    @pytest.mark.asyncio
    async def test_degrades_when_disabled(self, redis_disabled):
        assert await redis_client.get_json("k") is None
        assert await redis_client.set_json("k", {"a": 1}) is False

    @pytest.mark.asyncio
    async def test_degrades_when_broken(self, broken):
        assert await redis_client.get_json("k") is None
        assert await redis_client.set_json("k", {"a": 1}) is False


# ============================================================
# 计数器
# ============================================================

class TestIncr:
    @pytest.mark.asyncio
    async def test_increments_from_zero(self, fake):
        assert await redis_client.incr("c") == 1
        assert await redis_client.incr("c") == 2

    @pytest.mark.asyncio
    async def test_ttl_set_via_pipeline(self, fake):
        """INCR 与 EXPIRE 打包成一次往返。

        两条命令不需要原子性：即使 EXPIRE 丢失，
        下一个窗口的 INCR 会再设一次。
        """
        assert await redis_client.incr("rl:1.2.3.4:99", ttl=60) == 1
        assert fake.expires["rl:1.2.3.4:99"] == 60

    @pytest.mark.asyncio
    async def test_returns_none_when_unavailable(self, broken):
        """返回 None 而非 0。

        这个区分是限流迁移（T4）的前提：0 表示"这个窗口还没有请求"，
        None 表示"不知道" —— 后者必须降级放行，前者不能。
        混成一个值会让 Redis 一挂就把所有流量当成新窗口。
        """
        assert await redis_client.incr("c") is None
        assert await redis_client.incr("c", ttl=60) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self, redis_disabled):
        assert await redis_client.incr("c") is None


class TestGetInt:
    @pytest.mark.asyncio
    async def test_reads_value(self, fake):
        fake.store["n"] = "42"
        assert await redis_client.get_int("n") == 42

    @pytest.mark.asyncio
    async def test_missing_returns_default(self, fake):
        assert await redis_client.get_int("无", default=7) == 7

    @pytest.mark.asyncio
    async def test_non_numeric_returns_default(self, fake):
        """脏数据不该让调用方拿到 ValueError。"""
        fake.store["n"] = "abc"
        assert await redis_client.get_int("n", default=3) == 3

    @pytest.mark.asyncio
    async def test_degrades_to_default(self, broken):
        assert await redis_client.get_int("n", default=5) == 5

    @pytest.mark.asyncio
    async def test_zero_is_preserved_not_defaulted(self, fake):
        """存的 0 要读回 0，不能落到 default。

        这是 CLAUDE.md 那条"禁止用 or 链回退"在缓存层的同类问题：
        `int(raw) or default` 会把合法的 0 判为缺失。
        """
        fake.store["n"] = "0"
        assert await redis_client.get_int("n", default=99) == 0


# ============================================================
# delete / hash
# ============================================================

class TestDelete:
    @pytest.mark.asyncio
    async def test_deletes_existing(self, fake):
        fake.store["a"] = "1"
        assert await redis_client.delete("a") == 1

    @pytest.mark.asyncio
    async def test_empty_args_is_noop(self, fake):
        """不带 key 调用直接返回 0 —— Redis 的 DEL 不接受空参数会报错。"""
        assert await redis_client.delete() == 0

    @pytest.mark.asyncio
    async def test_degrades_to_zero(self, broken):
        assert await redis_client.delete("a") == 0


class TestHash:
    @pytest.mark.asyncio
    async def test_roundtrip_with_ttl(self, fake):
        ok = await redis_client.hset_mapping(
            "ingest:t1", {"status": "running", "progress": 42}, ttl=3600
        )
        assert ok is True
        assert await redis_client.hgetall("ingest:t1") == {
            "status": "running", "progress": "42",
        }
        assert fake.expires["ingest:t1"] == 3600

    @pytest.mark.asyncio
    async def test_nested_values_are_json_encoded(self, fake):
        """dict/list 转 JSON 字符串 —— Redis 的 hash 值只能是字符串。"""
        await redis_client.hset_mapping("k", {"errors": ["a", "b"]})
        assert json.loads(fake.hashes["k"]["errors"]) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_degrades(self, broken):
        assert await redis_client.hset_mapping("k", {"a": 1}) is False
        assert await redis_client.hgetall("k") == {}

    @pytest.mark.asyncio
    async def test_missing_hash_returns_empty(self, fake):
        assert await redis_client.hgetall("无") == {}


# ============================================================
# 知识库版本（缓存失效机制）
# ============================================================

class TestKbVersion:
    @pytest.mark.asyncio
    async def test_starts_at_zero(self, fake):
        assert await redis_client.get_kb_version() == 0

    @pytest.mark.asyncio
    async def test_bump_increments(self, fake):
        assert await redis_client.bump_kb_version() == 1
        assert await redis_client.get_kb_version() == 1

    @pytest.mark.asyncio
    async def test_version_has_no_ttl(self, fake):
        """版本号不能过期。

        它过期归零会让旧 key 重新变得"有效"，
        那些缓存对应的是已经变更过的知识库内容。
        """
        await redis_client.bump_kb_version()
        assert redis_client.KB_VERSION_KEY not in fake.expires

    @pytest.mark.asyncio
    async def test_bump_degrades_to_none(self, broken):
        """Redis 挂了时自增返回 None，调用方不该因此让文档上传失败。"""
        assert await redis_client.bump_kb_version() is None

    @pytest.mark.asyncio
    async def test_version_zero_when_unavailable(self, redis_disabled):
        """不可用时版本为 0，缓存 key 仍能拼出来（只是永远不命中）。"""
        assert await redis_client.get_kb_version() == 0


class TestClose:
    @pytest.mark.asyncio
    async def test_close_clears_client(self, fake, monkeypatch):
        closed = []
        monkeypatch.setattr(
            fake, "aclose", lambda: _record(closed), raising=False
        )
        await redis_client.close()
        assert redis_client._client is None

    @pytest.mark.asyncio
    async def test_close_survives_error(self, monkeypatch):
        """关闭失败不该影响进程退出。"""
        class Stubborn(FakeRedis):
            async def aclose(self):
                raise RedisError("关不掉")

        monkeypatch.setattr(redis_client, "_client", Stubborn())
        await redis_client.close()
        assert redis_client._client is None

    @pytest.mark.asyncio
    async def test_close_when_no_client(self, monkeypatch):
        monkeypatch.setattr(redis_client, "_client", None)
        await redis_client.close()


async def _record(bucket: list) -> None:
    bucket.append(True)
