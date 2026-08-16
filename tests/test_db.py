"""backend/db 单元测试

这份测试同时是数据库层的规格说明。覆盖四类不变量：

1. **建表与写读往返** —— 模型能在 SQLite 上建出来（PG/SQLite 共用一套模型，
   JSONB/JSON 的方言切换不能在建表期就炸）
2. **约束真的生效** —— unique/外键级联不是注释里的愿望，是数据库拦得住的东西
3. **事务边界** —— 异常必须回滚，不能留半成功状态
4. **降级** —— healthcheck 在库连不上时返回 False 而不是抛异常

不测 PostgreSQL 特有行为（JSONB 按字段查询、ARRAY），
那需要真实 PG 实例，属于集成测试范畴。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.db.models import (
    Base,
    Citation,
    EvalResult,
    EvalRun,
    Evidence,
    Feedback,
    IngestTask,
    Message,
    Run,
    RunStep,
    Session,
    ToolCall,
    utcnow,
)


# ============================================================
# 建表
# ============================================================

class TestSchema:
    """模型能在 SQLite 上建出来，且表集合与计划书一致。"""

    @pytest.mark.asyncio
    async def test_all_tables_created(self, db):
        from sqlalchemy import inspect

        engine = db.get_engine()
        async with engine.connect() as conn:
            names = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_table_names()
            )

        expected = {
            "sessions", "messages",
            "runs", "run_steps", "tool_calls", "evidence", "citations",
            "feedback", "eval_runs", "eval_results",
            "ingest_tasks",
        }
        assert expected <= set(names), f"缺表: {expected - set(names)}"

    def test_metadata_table_count(self):
        """11 张表。数量变了要么是新增需求，要么是误删，都该显式改这里。"""
        assert len(Base.metadata.tables) == 11


# ============================================================
# 写读往返
# ============================================================

class TestRunRoundtrip:
    @pytest.mark.asyncio
    async def test_run_gets_trace_id_and_default_status(self, db):
        """trace_id 有默认值（uuid4），status 默认 running。

        默认值必须由模型给：让每个调用点自己生成 uuid，
        迟早有一处忘了传而写进 NULL。
        """
        async with db.session_scope() as s:
            run = Run(query="测试查询")
            s.add(run)
            await s.flush()
            trace_id, status, run_id = run.trace_id, run.status, run.id

        assert len(trace_id) == 36
        assert status == "running"
        assert run_id is not None

    @pytest.mark.asyncio
    async def test_timestamps_are_timezone_aware(self, db):
        """时间戳带时区。

        naive datetime 与带时区的列混用时，比较处会静默出错 ——
        不报错，只是结果不对，这是最难查的一类 bug。
        """
        async with db.session_scope() as s:
            run = Run(query="q")
            s.add(run)
            await s.flush()
            run_id = run.id

        async with db.session_scope() as s:
            got = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
            assert got.created_at.tzinfo is not None
            assert got.created_at.utcoffset() == timezone.utc.utcoffset(None)

    @pytest.mark.asyncio
    async def test_json_field_roundtrip(self, db):
        """JSONField 在 SQLite 上退化为 JSON，嵌套结构要能原样取回。

        这条守的是 JSONField.load_dialect_impl 的方言分支 ——
        写死 JSONB 会在 SQLite 建表时就报错。
        """
        snapshot = {
            "node": "retrieve",
            "hits": [{"chunk_id": "c1", "score": 0.87}],
            "nested": {"深度": {"中文键": True}},
        }
        async with db.session_scope() as s:
            run = Run(query="q")
            s.add(run)
            await s.flush()
            s.add(RunStep(run_id=run.id, seq=0, node="retrieve",
                          ms=12, state_snapshot=snapshot))
            run_id = run.id

        async with db.session_scope() as s:
            step = (await s.execute(
                select(RunStep).where(RunStep.run_id == run_id)
            )).scalar_one()
            assert step.state_snapshot == snapshot

    @pytest.mark.asyncio
    async def test_empty_dict_json_is_not_null(self, db):
        """空 JSON 存的是 {} 而非 NULL。

        两者语义不同：{} 是"没有内容"，NULL 是"没记录过"。
        用 `or` 链读取时 NULL 与 {} 会被混为一谈，这正是
        CLAUDE.md 禁止用 `or` 做回退的同类问题。
        """
        async with db.session_scope() as s:
            run = Run(query="q")
            s.add(run)
            await s.flush()
            s.add(RunStep(run_id=run.id, seq=0, node="n"))
            run_id = run.id

        async with db.session_scope() as s:
            step = (await s.execute(
                select(RunStep).where(RunStep.run_id == run_id)
            )).scalar_one()
            assert step.state_snapshot == {}
            assert step.state_snapshot is not None


class TestEvidence:
    @pytest.mark.asyncio
    async def test_relevance_zero_is_preserved(self, db):
        """relevance=0.0 必须原样存回，不能被当成"缺失"。

        这是 scoring 那条教训在存储层的回归测试：`0.0` 是合法分数
        （sigmoid 前的极低分），用 `or` 链或 falsy 判断会把它吞掉。
        """
        async with db.session_scope() as s:
            run = Run(query="q")
            s.add(run)
            await s.flush()
            s.add(Evidence(run_id=run.id, chunk_id="c0", rank=0, relevance=0.0))
            run_id = run.id

        async with db.session_scope() as s:
            ev = (await s.execute(
                select(Evidence).where(Evidence.run_id == run_id)
            )).scalar_one()
            assert ev.relevance is not None
            assert float(ev.relevance) == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_used_in_answer_defaults_false(self, db):
        """"检索到"与"被答案用上"是两件事，默认是前者。"""
        async with db.session_scope() as s:
            run = Run(query="q")
            s.add(run)
            await s.flush()
            s.add(Evidence(run_id=run.id, chunk_id="c1", rank=0))
            run_id = run.id

        async with db.session_scope() as s:
            ev = (await s.execute(
                select(Evidence).where(Evidence.run_id == run_id)
            )).scalar_one()
            assert ev.used_in_answer is False


class TestCitation:
    @pytest.mark.asyncio
    async def test_verified_defaults_to_null_not_false(self, db):
        """未校验（NULL）与校验未通过（False）是不同语义。

        默认值给 False 会让"还没跑 citation_verify"看起来像
        "跑了且没通过"，M4 面板会把它标红 —— 这是错的。
        """
        async with db.session_scope() as s:
            sess = Session(id="s1")
            s.add(sess)
            await s.flush()
            msg = Message(session_id="s1", role="assistant", content="答案")
            s.add(msg)
            await s.flush()
            s.add(Citation(message_id=msg.id, sentence_idx=0, chunk_id="c1"))
            msg_id = msg.id

        async with db.session_scope() as s:
            cit = (await s.execute(
                select(Citation).where(Citation.message_id == msg_id)
            )).scalar_one()
            assert cit.verified is None
            assert cit.verify_score is None


# ============================================================
# 约束
# ============================================================

class TestConstraints:
    @pytest.mark.asyncio
    async def test_run_trace_id_is_unique(self, db):
        """trace_id 重复必须被数据库拒绝 —— 它是对外的唯一标识。"""
        async with db.session_scope() as s:
            s.add(Run(query="q1", trace_id="dup-trace"))

        with pytest.raises(IntegrityError):
            async with db.session_scope() as s:
                s.add(Run(query="q2", trace_id="dup-trace"))

    @pytest.mark.asyncio
    async def test_run_step_seq_unique_per_run(self, db):
        """同一 run 内 seq 唯一。重复写入是 bug，让数据库拦住。"""
        async with db.session_scope() as s:
            run = Run(query="q")
            s.add(run)
            await s.flush()
            run_id = run.id
            s.add(RunStep(run_id=run_id, seq=0, node="a"))

        with pytest.raises(IntegrityError):
            async with db.session_scope() as s:
                s.add(RunStep(run_id=run_id, seq=0, node="b"))

    @pytest.mark.asyncio
    async def test_same_seq_allowed_across_runs(self, db):
        """唯一性限定在 run 内 —— 每个 run 的 seq 都从 0 开始。"""
        async with db.session_scope() as s:
            r1, r2 = Run(query="q1"), Run(query="q2")
            s.add_all([r1, r2])
            await s.flush()
            s.add_all([
                RunStep(run_id=r1.id, seq=0, node="n"),
                RunStep(run_id=r2.id, seq=0, node="n"),
            ])

        async with db.session_scope() as s:
            count = (await s.execute(select(func.count()).select_from(RunStep))).scalar_one()
            assert count == 2

    @pytest.mark.asyncio
    async def test_tool_call_seq_unique_per_run(self, db):
        async with db.session_scope() as s:
            run = Run(query="q")
            s.add(run)
            await s.flush()
            run_id = run.id
            s.add(ToolCall(run_id=run_id, seq=0, tool="calculate"))

        with pytest.raises(IntegrityError):
            async with db.session_scope() as s:
                s.add(ToolCall(run_id=run_id, seq=0, tool="search_knowledge_base"))

    @pytest.mark.asyncio
    async def test_idempotency_key_is_not_unique_yet(self, db):
        """当前 idempotency_key 只有普通索引，允许重复。

        这条测试记录的是**现状而非目标**：M3 若要用它防重复副作用，
        必须先改成唯一约束，并把执行模式改为"先插占位行、靠冲突判重"。
        现在这样"查一下有没有 → 没有就执行"在并发下两个请求会同时查空、
        同时执行。改造时这条测试应当被替换为断言 IntegrityError。
        """
        async with db.session_scope() as s:
            run = Run(query="q")
            s.add(run)
            await s.flush()
            s.add_all([
                ToolCall(run_id=run.id, seq=0, tool="calculate", idempotency_key="k"),
                ToolCall(run_id=run.id, seq=1, tool="calculate", idempotency_key="k"),
            ])

        async with db.session_scope() as s:
            count = (await s.execute(
                select(func.count()).select_from(ToolCall)
                .where(ToolCall.idempotency_key == "k")
            )).scalar_one()
            assert count == 2


class TestCascade:
    @pytest.mark.asyncio
    async def test_deleting_run_removes_children(self, db):
        """删 run 应级联清掉步骤、工具调用、证据。

        不级联的话孤儿行会越积越多且无人清理 —— 这类表在
        跑过几万次 run 后会变成排查时的噪声。
        """
        async with db.session_scope() as s:
            run = Run(query="q")
            s.add(run)
            await s.flush()
            s.add_all([
                RunStep(run_id=run.id, seq=0, node="n"),
                ToolCall(run_id=run.id, seq=0, tool="t"),
                Evidence(run_id=run.id, chunk_id="c", rank=0),
            ])
            run_id = run.id

        async with db.session_scope() as s:
            run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
            await s.delete(run)

        async with db.session_scope() as s:
            for model in (RunStep, ToolCall, Evidence):
                count = (await s.execute(
                    select(func.count()).select_from(model)
                )).scalar_one()
                assert count == 0, f"{model.__tablename__} 有孤儿行"

    @pytest.mark.asyncio
    async def test_deleting_session_removes_messages(self, db):
        async with db.session_scope() as s:
            sess = Session(id="s1")
            s.add(sess)
            await s.flush()
            s.add(Message(session_id="s1", role="user", content="你好"))

        async with db.session_scope() as s:
            sess = (await s.execute(
                select(Session).where(Session.id == "s1")
            )).scalar_one()
            await s.delete(sess)

        async with db.session_scope() as s:
            count = (await s.execute(
                select(func.count()).select_from(Message)
            )).scalar_one()
            assert count == 0


# ============================================================
# 事务边界
# ============================================================

class TestSessionScope:
    @pytest.mark.asyncio
    async def test_exception_rolls_back(self, db):
        """异常必须回滚，不留半成功状态。

        对应 Review 第 4 问："写入失败会不会留下半成功状态"。
        这里的答案是不会 —— 由 session_scope 保证，不靠调用方自觉。
        """
        class Boom(Exception):
            pass

        with pytest.raises(Boom):
            async with db.session_scope() as s:
                s.add(Run(query="会被回滚"))
                await s.flush()
                raise Boom()

        async with db.session_scope() as s:
            count = (await s.execute(select(func.count()).select_from(Run))).scalar_one()
            assert count == 0

    @pytest.mark.asyncio
    async def test_integrity_error_rolls_back_whole_batch(self, db):
        """批量写入中有一条违反约束，整批都不该落库。

        部分成功比全部失败更糟：数据处于中间态，
        重试逻辑无法判断该从哪继续。
        """
        async with db.session_scope() as s:
            s.add(Run(query="q1", trace_id="t1"))

        with pytest.raises(IntegrityError):
            async with db.session_scope() as s:
                s.add_all([
                    Run(query="q2", trace_id="t2"),
                    Run(query="q3", trace_id="t1"),  # 冲突
                ])

        async with db.session_scope() as s:
            count = (await s.execute(select(func.count()).select_from(Run))).scalar_one()
            assert count == 1

    @pytest.mark.asyncio
    async def test_sequential_scopes_are_independent(self, db):
        """一个 scope 失败不影响后续 scope。

        守的是"连接带着失败的事务回到池里"这个坑：
        漏掉 rollback 时，后续请求拿到该连接会报
        "current transaction is aborted"。
        """
        with pytest.raises(IntegrityError):
            async with db.session_scope() as s:
                s.add_all([
                    Run(query="a", trace_id="same"),
                    Run(query="b", trace_id="same"),
                ])

        async with db.session_scope() as s:
            s.add(Run(query="后续请求", trace_id="ok"))

        async with db.session_scope() as s:
            got = (await s.execute(
                select(Run).where(Run.trace_id == "ok")
            )).scalar_one()
            assert got.query == "后续请求"


class TestEngineLifecycle:
    def test_engine_is_singleton(self, db):
        """engine 复用。每请求新建会耗尽数据库连接数（PG 默认上限 100）。"""
        assert db.get_engine() is db.get_engine()

    def test_sessionmaker_is_singleton(self, db):
        assert db.get_sessionmaker() is db.get_sessionmaker()

    @pytest.mark.asyncio
    async def test_dispose_allows_recreate(self, db):
        """dispose 后再取应拿到新 engine，而不是已关闭的那个。"""
        first = db.get_engine()
        await db.dispose_engine()
        second = db.get_engine()
        assert first is not second

    @pytest.mark.asyncio
    async def test_init_models_is_idempotent(self, db):
        """重复建表不报错 —— create_all 只建不存在的表。

        注意这也是它的局限：模型改了它不会更新已存在的表，
        所以生产路径只能是 alembic upgrade head。
        """
        await db.init_models()
        await db.init_models()


class TestSqlitePragmas:
    """`_apply_sqlite_pragmas` 修正的三个默认行为。

    这些是连接级设置，删掉监听器不会有任何语法错误，只会让行为悄悄
    退回 SQLite 的默认值 —— 而默认值与 PostgreSQL 不一致。
    """

    @pytest.mark.asyncio
    async def test_foreign_keys_are_enforced(self, db):
        """指向不存在父行的外键必须被拒绝。

        SQLite 默认**不强制**外键，`ondelete="CASCADE"` 只是装饰。
        没有 `PRAGMA foreign_keys=ON` 时这条插入会成功，
        留下一条指向空气的消息。
        """
        with pytest.raises(IntegrityError):
            async with db.session_scope() as s:
                s.add(Message(session_id="根本不存在", role="user", content="x"))

    @pytest.mark.asyncio
    async def test_database_level_cascade_works(self, db):
        """批量 DELETE 时靠数据库外键级联，不靠 ORM。

        与 test_deleting_session_removes_messages 的区别很重要：
        那条用 `session.delete(obj)`，走的是 SQLAlchemy 在 Python 里
        逐个删的 ORM 级联 —— **即使数据库根本不强制外键它也会通过**。
        这条用 DELETE 语句，只有数据库真的级联才成立。
        """
        from sqlalchemy import delete

        async with db.session_scope() as s:
            s.add(Session(id="s1"))
            await s.flush()
            s.add(Message(session_id="s1", role="user", content="x"))

        async with db.session_scope() as s:
            await s.execute(delete(Session).where(Session.id == "s1"))

        async with db.session_scope() as s:
            count = (await s.execute(
                select(func.count()).select_from(Message)
            )).scalar_one()
            assert count == 0, "数据库级联没生效，外键约束可能没打开"

    @pytest.mark.asyncio
    async def test_savepoint_rolls_back_independently(self, db):
        """内层 SAVEPOINT 回滚不影响外层事务。

        pysqlite 的隐式事务管理会破坏这个语义 —— 不关掉它的话，
        内层回滚后数据仍在，`get_or_create` 的并发处理就是错的。
        """
        async with db.session_scope() as s:
            s.add(Run(query="外层保留", trace_id="outer"))
            await s.flush()

            try:
                async with s.begin_nested():
                    s.add(Run(query="内层丢弃", trace_id="outer"))  # 撞唯一约束
            except IntegrityError:
                pass

            s.add(Run(query="内层之后", trace_id="after"))

        async with db.session_scope() as s:
            traces = (await s.execute(select(Run.trace_id))).scalars().all()
            assert sorted(traces) == ["after", "outer"]

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, db):
        """WAL 模式：读不阻塞写。默认的 rollback journal 下两者互斥。"""
        from sqlalchemy import text

        async with db.session_scope() as s:
            mode = (await s.execute(text("PRAGMA journal_mode"))).scalar_one()
            assert mode.lower() == "wal"


class TestHealthcheck:
    @pytest.mark.asyncio
    async def test_healthcheck_ok(self, db):
        assert await db.healthcheck() is True

    @pytest.mark.asyncio
    async def test_healthcheck_returns_false_when_unreachable(self, monkeypatch):
        """库连不上时返回 False 而不是抛异常。

        健康检查抛异常会让 /health 端点自己 500，
        那时监控看到的是"服务挂了"而非"数据库挂了"，指错方向。
        """
        import config
        from backend.db import session as db_session

        # 指向一个不存在的目录，建连接必失败
        monkeypatch.setattr(
            config, "DATABASE_URL",
            "sqlite+aiosqlite:///./__no_such_dir__/x.db",
        )
        monkeypatch.setattr(db_session, "_engine", None)
        monkeypatch.setattr(db_session, "_sessionmaker", None)

        try:
            assert await db_session.healthcheck() is False
        finally:
            await db_session.dispose_engine()


# ============================================================
# 并发（对应 Review 第 3 问）
# ============================================================

class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_writes_each_get_own_session(self, db):
        """并发写入各自拿到独立 session。

        AsyncSession 不是任务安全的，共享会让并发请求的事务互相污染。
        session_scope 每次进入都新建一个，这条测试守住该行为。

        注意 SQLite 是单写者模型，这里验证的是 session 隔离而非
        真正的写并发能力 —— 后者需要 PG。
        """
        async def write(i: int) -> None:
            async with db.session_scope() as s:
                s.add(Run(query=f"q{i}", trace_id=f"trace-{i}"))

        await asyncio.gather(*(write(i) for i in range(10)))

        async with db.session_scope() as s:
            count = (await s.execute(select(func.count()).select_from(Run))).scalar_one()
            assert count == 10


# ============================================================
# 其余表的最小往返
# ============================================================

class TestRemainingTables:
    @pytest.mark.asyncio
    async def test_ingest_task_defaults(self, db):
        """摄入任务默认 pending，终态字段留空。

        进度写 Redis（高频，不该打库），终态写这里 ——
        Redis 丢了要能从 PG 知道任务是否完成。
        """
        async with db.session_scope() as s:
            s.add(IngestTask(id="task-1", filename="a.pdf", size_bytes=1024))

        async with db.session_scope() as s:
            task = (await s.execute(
                select(IngestTask).where(IngestTask.id == "task-1")
            )).scalar_one()
            assert task.status == "pending"
            assert task.category == "uploaded"
            assert task.chunk_count is None
            assert task.finished_at is None

    @pytest.mark.asyncio
    async def test_eval_run_with_results(self, db):
        """评测结果的 metrics 用 JSON：加一个指标不该需要一次迁移。"""
        async with db.session_scope() as s:
            ev_run = EvalRun(suite="t2ranking", variant="rrf")
            s.add(ev_run)
            await s.flush()
            s.add(EvalResult(
                eval_run_id=ev_run.id, qid="q1",
                metrics={"recall@5": 0.708, "mrr@10": 0.902},
            ))
            ev_run_id = ev_run.id

        async with db.session_scope() as s:
            res = (await s.execute(
                select(EvalResult).where(EvalResult.eval_run_id == ev_run_id)
            )).scalar_one()
            assert res.metrics["recall@5"] == pytest.approx(0.708)

    @pytest.mark.asyncio
    async def test_feedback_accepts_negative_rating(self, db):
        """rating 是 -1/0/1，负值必须能存 —— 用无符号类型会在这里炸。"""
        async with db.session_scope() as s:
            sess = Session(id="s1")
            s.add(sess)
            await s.flush()
            msg = Message(session_id="s1", role="assistant", content="a")
            s.add(msg)
            await s.flush()
            s.add(Feedback(message_id=msg.id, rating=-1, comment="不准确"))
            msg_id = msg.id

        async with db.session_scope() as s:
            fb = (await s.execute(
                select(Feedback).where(Feedback.message_id == msg_id)
            )).scalar_one()
            assert fb.rating == -1

    @pytest.mark.asyncio
    async def test_message_run_id_is_optional(self, db):
        """用户消息没有对应的 run，run_id 必须允许为空。"""
        async with db.session_scope() as s:
            s.add(Session(id="s1"))
            await s.flush()
            s.add(Message(session_id="s1", role="user", content="问题"))

        async with db.session_scope() as s:
            msg = (await s.execute(select(Message))).scalar_one()
            assert msg.run_id is None


class TestUtcnow:
    def test_returns_aware_utc(self):
        now = utcnow()
        assert now.tzinfo is not None
        assert now.utcoffset().total_seconds() == 0

    def test_differs_from_naive_utcnow(self):
        """utcnow() 不是 datetime.utcnow() —— 后者返回 naive，
        与带时区的列混用时会在比较处静默出错。"""
        assert utcnow().tzinfo is not None
        assert datetime.now(timezone.utc).tzinfo is not None
