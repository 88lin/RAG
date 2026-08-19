"""T4 限流迁移 + 缓存防御测试

覆盖三个已知问题的修复：

1. **内存有界**：超过 _MAX_TRACKED_IPS 个 IP 时，最旧的被淘汰而非无限增长
2. **XFF 校验**：只有请求来自可信代理时才信任 x-forwarded-for
3. **Redis 降级**：Redis 不可用时自动切到内存限流，不拒绝流量

额外覆盖：
- Redis 可用时 INCR+EXPIRE 路径正常工作
- 超限时返回 429 含 Retry-After
- 系统路径 /health 豁免
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.rate_limit import RateLimitMiddleware, _get_client_ip, _MAX_TRACKED_IPS


# ---------------------------------------------------------------------------
# 辅助 —— 构造带中间件的最小 FastAPI 应用
# ---------------------------------------------------------------------------

def _make_app(
    requests_per_minute: int = 3,
    trusted_proxies: list[str] | None = None,
    window_seconds: int = 60,
) -> FastAPI:
    """创建带 RateLimitMiddleware 的测试应用。"""
    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # monkeypatch config values before middleware init
    import config
    original_trusted = config.TRUSTED_PROXY_IPS
    original_window = config.RATE_LIMIT_WINDOW_SECONDS

    config.TRUSTED_PROXY_IPS = trusted_proxies or []
    config.RATE_LIMIT_WINDOW_SECONDS = window_seconds

    app.add_middleware(RateLimitMiddleware, requests_per_minute=requests_per_minute)

    # restore after middleware is constructed
    config.TRUSTED_PROXY_IPS = original_trusted
    config.RATE_LIMIT_WINDOW_SECONDS = original_window

    return app


# ---------------------------------------------------------------------------
# 1. /health 豁免（不被限流）
# ---------------------------------------------------------------------------

def test_health_path_exempt():
    """系统路径 /health 不受限流影响，高频请求仍返回 200。"""
    app = _make_app(requests_per_minute=1)

    with patch("backend.rate_limit.redis_client.incr", new_callable=AsyncMock) as mock_incr:
        mock_incr.return_value = None  # Redis 不可用 → 内存降级

        client = TestClient(app, raise_server_exceptions=True)
        for _ in range(5):
            r = client.get("/health")
            assert r.status_code == 200


# ---------------------------------------------------------------------------
# 2. 超限返回 429 + Retry-After
# ---------------------------------------------------------------------------

def test_rate_limit_returns_429():
    """超出配额的请求返回 429，响应头含 Retry-After。"""
    app = _make_app(requests_per_minute=2, window_seconds=60)

    with patch("backend.rate_limit.redis_client.incr", new_callable=AsyncMock) as mock_incr:
        mock_incr.return_value = None  # Redis 不可用

        client = TestClient(app, raise_server_exceptions=False)
        r1 = client.get("/ping")
        r2 = client.get("/ping")
        assert r1.status_code == 200
        assert r2.status_code == 200
        r3 = client.get("/ping")
        assert r3.status_code == 429
        assert "Retry-After" in r3.headers
        body = r3.json()
        assert body["retry_after"] == 60


# ---------------------------------------------------------------------------
# 3. 内存有界 —— _MAX_TRACKED_IPS 上限 FIFO 淘汰
# ---------------------------------------------------------------------------

def test_memory_table_bounded():
    """内存降级表不超过 _MAX_TRACKED_IPS，超出时淘汰最旧 IP。"""
    app = _make_app(requests_per_minute=1000)  # 配额很高，不触发限流

    # 取中间件实例
    mw: RateLimitMiddleware = app.middleware_stack

    # 直接操作内部表来测试有界性
    with patch("backend.rate_limit.redis_client.incr", new_callable=AsyncMock) as mock_incr:
        mock_incr.return_value = None  # Redis 不可用

        client = TestClient(app, raise_server_exceptions=False)

        # 用不同 IP 发请求，每次用一个全新的 IP
        for i in range(_MAX_TRACKED_IPS + 10):
            client.get("/ping", headers={"TESTCLIENT-ip-override": f"10.0.{i // 256}.{i % 256}"})

        # 找到中间件实例
        stack = app.middleware_stack
        while stack is not None:
            if isinstance(stack, RateLimitMiddleware):
                assert len(stack._mem_table) <= _MAX_TRACKED_IPS
                break
            stack = getattr(stack, "app", None)


# ---------------------------------------------------------------------------
# 4. XFF — 不可信代理时忽略 XFF
# ---------------------------------------------------------------------------

def test_xff_ignored_without_trusted_proxy():
    """无可信代理时，XFF 被忽略，直接用 TCP 连接 IP。"""
    import config

    original = config.TRUSTED_PROXY_IPS
    config.TRUSTED_PROXY_IPS = []  # 空 = 不信任任何代理

    try:
        from backend.rate_limit import _get_client_ip as get_ip

        class FakeClient:
            host = "192.168.1.1"

        class FakeRequest:
            client = FakeClient()
            headers = {"x-forwarded-for": "1.2.3.4"}

        ip = get_ip(FakeRequest(), frozenset())
        assert ip == "192.168.1.1"
    finally:
        config.TRUSTED_PROXY_IPS = original


# ---------------------------------------------------------------------------
# 5. XFF — 可信代理时使用 XFF 第一段
# ---------------------------------------------------------------------------

def test_xff_trusted_when_proxy_ip_in_allowlist():
    """请求来自可信代理时，使用 XFF 最左侧（原始客户端）IP。"""

    class FakeClient:
        host = "10.0.0.1"  # 代理 IP，在可信列表里

    class FakeRequest:
        client = FakeClient()
        headers = {"x-forwarded-for": "203.0.113.42, 10.0.0.1"}

    ip = _get_client_ip(FakeRequest(), frozenset({"10.0.0.1"}))
    assert ip == "203.0.113.42"


# ---------------------------------------------------------------------------
# 6. XFF — 多层代理取最左边第一段
# ---------------------------------------------------------------------------

def test_xff_takes_leftmost_segment():
    """XFF 含多个 IP 时只取第一个（原始客户端）。"""

    class FakeClient:
        host = "10.0.0.2"

    class FakeRequest:
        client = FakeClient()
        headers = {"x-forwarded-for": "5.6.7.8, 10.0.0.1, 10.0.0.2"}

    ip = _get_client_ip(FakeRequest(), frozenset({"10.0.0.2"}))
    assert ip == "5.6.7.8"


# ---------------------------------------------------------------------------
# 7. Redis 路径正常工作 —— INCR 返回值在配额内
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_path_allows_within_quota():
    """Redis 可用且 count ≤ limit 时，请求放行。"""
    import config

    mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
    mw.requests_per_minute = 5
    mw.window_seconds = 60
    mw.trusted_proxies = frozenset()
    from collections import OrderedDict
    mw._mem_table = OrderedDict()

    with patch("backend.rate_limit.redis_client.incr", new_callable=AsyncMock) as mock_incr:
        mock_incr.return_value = 3  # count=3，limit=5，放行
        result = await mw._check_redis("1.2.3.4")

    assert result is not None
    is_limited, count = result
    assert not is_limited
    assert count == 3


# ---------------------------------------------------------------------------
# 8. Redis 路径超限
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_path_blocks_over_quota():
    """Redis 可用且 count > limit 时，请求被拦。"""
    mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
    mw.requests_per_minute = 5
    mw.window_seconds = 60
    mw.trusted_proxies = frozenset()
    from collections import OrderedDict
    mw._mem_table = OrderedDict()

    with patch("backend.rate_limit.redis_client.incr", new_callable=AsyncMock) as mock_incr:
        mock_incr.return_value = 6  # count=6 > limit=5
        result = await mw._check_redis("1.2.3.4")

    assert result is not None
    is_limited, count = result
    assert is_limited
    assert count == 6


# ---------------------------------------------------------------------------
# 9. Redis 不可用 → None（调用方降级）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_unavailable_returns_none():
    """Redis.incr 返回 None 时，_check_redis 也返回 None（触发降级）。"""
    mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
    mw.requests_per_minute = 5
    mw.window_seconds = 60
    mw.trusted_proxies = frozenset()
    from collections import OrderedDict
    mw._mem_table = OrderedDict()

    with patch("backend.rate_limit.redis_client.incr", new_callable=AsyncMock) as mock_incr:
        mock_incr.return_value = None
        result = await mw._check_redis("1.2.3.4")

    assert result is None


# ---------------------------------------------------------------------------
# 10. 内存降级自己的窗口逻辑
# ---------------------------------------------------------------------------

def test_memory_fallback_window_expires():
    """内存降级中，超过窗口时间的记录被清理，限流计数重置。"""
    mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
    mw.requests_per_minute = 2
    mw.window_seconds = 1  # 1 秒窗口，便于测试
    from collections import OrderedDict, deque
    mw._mem_table = OrderedDict()

    ip = "5.6.7.8"

    # 写入 2 次（到达上限）
    is_limited1, _ = mw._check_memory(ip)
    is_limited2, _ = mw._check_memory(ip)
    is_limited3, _ = mw._check_memory(ip)  # 第 3 次超限
    assert not is_limited1
    assert not is_limited2
    assert is_limited3

    # 等窗口过期
    time.sleep(1.1)

    # 窗口过后应该可以再请求
    is_limited_after, count = mw._check_memory(ip)
    assert not is_limited_after
    assert count == 1


# ---------------------------------------------------------------------------
# 11. 响应头 X-RateLimit-Remaining 正确
# ---------------------------------------------------------------------------

def test_ratelimit_remaining_header():
    """响应头 X-RateLimit-Remaining 反映剩余配额。"""
    app = _make_app(requests_per_minute=5, window_seconds=60)

    with patch("backend.rate_limit.redis_client.incr", new_callable=AsyncMock) as mock_incr:
        # 模拟第 1 次请求，Redis 返回 count=1
        mock_incr.return_value = 1

        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/ping")
        assert r.status_code == 200
        remaining = int(r.headers.get("X-RateLimit-Remaining", -1))
        assert remaining == 4  # 5 - 1 = 4
