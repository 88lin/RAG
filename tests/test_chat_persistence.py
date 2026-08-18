"""T5 会话落库测试

验收标准 C3/C4/C5：
  C3  一次问答在 runs 留一行，status=ok
  C4  evidence 表记录检索结果；answer 里引用的 chunk 标记 used_in_answer=True
  C5  messages 表留 user/assistant 两条记录，新 session_scope 后仍可读

附加：
  - 落库失败不向上抛（不中断 SSE 流）
  - 双路由（citation / smart）均落库，route 字段值正确
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from backend.services.chat_service import _persist_run_async


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _make_results(n: int = 3) -> list[dict]:
    return [
        {
            "id": f"chunk_{i}",
            "metadata": {"file": f"doc_{i}.md", "category": "uploaded"},
            "relevance": round(0.9 - i * 0.1, 2),
            "retrieved_by": ["vector"],
        }
        for i in range(n)
    ]


def _make_citations(results: list[dict], used: int = 1) -> list[dict]:
    return [
        {"number": i + 1, "chunk_id": r["id"], "file": r["metadata"]["file"]}
        for i, r in enumerate(results[:used])
    ]


# ---------------------------------------------------------------------------
# C3：一次问答 → runs 留一行 status=ok
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c3_run_row_created(db):
    """C3: 一次 Q&A 结束后 runs 表里有一行 status=ok，耗时字段正确写入。"""
    results = _make_results(2)
    await _persist_run_async(
        session_id="sess-c3",
        query="测试问题",
        route="citation",
        results=results,
        full_answer="答案文本",
        citations=_make_citations(results),
        total_ms=1200,
        first_token_ms=300,
    )

    from backend.db.session import session_scope
    from backend.repositories import RunRepository

    async with session_scope() as s:
        runs = await RunRepository(s).list_recent(limit=20)

    matching = [r for r in runs if r.query == "测试问题" and r.session_id == "sess-c3"]
    assert len(matching) == 1
    run = matching[0]
    assert run.status == "ok"
    assert run.route == "citation"
    assert run.total_ms == 1200
    assert run.first_token_ms == 300


# ---------------------------------------------------------------------------
# C4：evidence 落库，used_in_answer 标记正确
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c4_evidence_recorded(db):
    """C4: 检索到 3 条，引用 2 条 → used_in_answer 按实际引用标记。"""
    results = _make_results(3)
    citations = _make_citations(results, used=2)

    await _persist_run_async(
        session_id="sess-c4",
        query="证据测试",
        route="citation",
        results=results,
        full_answer="引用了两处",
        citations=citations,
        total_ms=800,
        first_token_ms=None,
    )

    from backend.db.session import session_scope
    from backend.repositories import EvidenceRepository, RunRepository

    async with session_scope() as s:
        run_repo = RunRepository(s)
        runs = await run_repo.list_recent(limit=20)
        run = next((r for r in runs if r.query == "证据测试"), None)
        assert run is not None

        evidence = await EvidenceRepository(s).list_for_run(run.id)

    assert len(evidence) == 3
    used = [e for e in evidence if e.used_in_answer]
    unused = [e for e in evidence if not e.used_in_answer]
    assert len(used) == 2
    assert len(unused) == 1
    assert sorted(e.rank for e in evidence) == [0, 1, 2]


# ---------------------------------------------------------------------------
# C5：messages 持久化，新 session_scope 仍可读
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c5_messages_persisted(db):
    """C5: 新事务内仍能读到 user/assistant 消息，assistant 消息链接 run_id。"""
    results = _make_results(1)
    await _persist_run_async(
        session_id="sess-c5",
        query="持久化问题",
        route="smart",
        results=results,
        full_answer="持久化答案",
        citations=[],
        total_ms=500,
        first_token_ms=100,
    )

    from backend.db.session import session_scope
    from backend.repositories import SessionRepository

    async with session_scope() as s:
        msgs = await SessionRepository(s).history("sess-c5")

    roles = [m.role for m in msgs]
    assert "user" in roles
    assert "assistant" in roles
    user_msg = next(m for m in msgs if m.role == "user")
    asst_msg = next(m for m in msgs if m.role == "assistant")
    assert user_msg.content == "持久化问题"
    assert asst_msg.content == "持久化答案"
    assert asst_msg.run_id is not None


# ---------------------------------------------------------------------------
# 落库失败不抛异常（SSE 不中断）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_failure_does_not_raise(db, monkeypatch):
    """模拟 session_scope 抛异常，验证 _persist_run_async 静默吞掉。"""
    from contextlib import asynccontextmanager
    import backend.db.session as db_session_module

    @asynccontextmanager
    async def _bad_scope():
        raise RuntimeError("数据库故障（模拟）")
        yield  # noqa: unreachable — makes it a valid asynccontextmanager body

    monkeypatch.setattr(db_session_module, "session_scope", _bad_scope)

    # 不应抛出
    await _persist_run_async(
        session_id="sess-err",
        query="x",
        route="smart",
        results=[],
        full_answer="y",
        citations=[],
        total_ms=0,
        first_token_ms=None,
    )


# ---------------------------------------------------------------------------
# route 字段区分 citation / smart
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_field_smart(db):
    """route='smart' 时 runs.route 写的是 'smart'。"""
    await _persist_run_async(
        session_id="sess-smart",
        query="智能模式问题",
        route="smart",
        results=[],
        full_answer="通用知识答案",
        citations=[],
        total_ms=300,
        first_token_ms=None,
    )

    from backend.db.session import session_scope
    from backend.repositories import RunRepository

    async with session_scope() as s:
        runs = await RunRepository(s).list_recent(limit=20)
    run = next((r for r in runs if r.query == "智能模式问题"), None)
    assert run is not None
    assert run.route == "smart"


# ---------------------------------------------------------------------------
# first_token_ms=None 时不写 0（不是快速缓存命中）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_token_ms_none_when_no_token(db):
    """first_token_ms=None 传入时，数据库里记 NULL 而非 0。"""
    await _persist_run_async(
        session_id="sess-no-token",
        query="无首 token 耗时",
        route="smart",
        results=[],
        full_answer="",
        citations=[],
        total_ms=100,
        first_token_ms=None,
    )

    from backend.db.session import session_scope
    from backend.repositories import RunRepository

    async with session_scope() as s:
        runs = await RunRepository(s).list_recent(limit=20)
    run = next((r for r in runs if r.query == "无首 token 耗时"), None)
    assert run is not None
    assert run.first_token_ms is None
