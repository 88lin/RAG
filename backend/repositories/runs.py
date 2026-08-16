"""执行轨迹的读写

`runs` / `run_steps` / `tool_calls` 由同一个 Repository 管，不各建一个。
它们是一个**聚合**：步骤与工具调用不能脱离 run 独立存在，
删 run 就该连它们一起删（外键已配 ondelete CASCADE）。
按聚合而非按表划分 Repository，边界才和数据的生命周期对齐。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import desc, select, update
from sqlalchemy.orm import selectinload

from ..db.models import Run, RunStep, ToolCall, utcnow
from .base import BaseRepository

# 终态集合。running 之外的状态都表示这次执行已经结束。
# 不用枚举类型：M3 会扩展状态集合，而 PG 的 ALTER TYPE 需要一次迁移。
TERMINAL_STATUSES = frozenset({"ok", "error", "cancelled", "no_answer"})


class RunRepository(BaseRepository):
    """一次端到端执行的轨迹。"""

    async def create(
        self,
        query: str,
        *,
        trace_id: Optional[str] = None,
        session_id: Optional[str] = None,
        route: Optional[str] = None,
    ) -> Run:
        """新建一条 running 状态的 run，返回已带 id 的对象。

        trace_id 不传则生成。允许传入是为了让调用方能先把 trace_id 写进
        日志与 SSE 事件，再落库 —— 出错时日志里的 id 和库里的对得上。
        """
        run = Run(
            trace_id=trace_id or str(uuid.uuid4()),
            query=query,
            session_id=session_id,
            route=route,
            status="running",
        )
        self.session.add(run)
        # flush 而非 commit：拿到自增 id，但事务仍开着，出错可整体回滚
        await self.session.flush()
        return run

    async def finish(
        self,
        run_id: int,
        *,
        status: str = "ok",
        total_ms: Optional[int] = None,
        first_token_ms: Optional[int] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        error: Optional[str] = None,
    ) -> bool:
        """标记 run 结束。返回是否命中了一行。

        用 UPDATE 语句而不是"先查出对象再改属性"：后者要两次往返，
        且在并发下会读到过期快照。

        耗时由调用方传入而非在这里算：跨方言的时间差运算写法不同
        （SQLite 用 julianday，PG 用 interval），而调用方本来就持有
        计时器。Repository 不该为此引入方言分支。
        """
        values: Dict[str, Any] = {
            "status": status,
            "finished_at": utcnow(),
        }
        # 逐个判 None 而非用 `or` 链或字典推导过滤假值：
        # total_ms=0 与 first_token_ms=0 都是合法值（极快的缓存命中），
        # 用真值判断会把它们当成"没传"而丢掉。
        if total_ms is not None:
            values["total_ms"] = total_ms
        if first_token_ms is not None:
            values["first_token_ms"] = first_token_ms
        if prompt_tokens is not None:
            values["prompt_tokens"] = prompt_tokens
        if completion_tokens is not None:
            values["completion_tokens"] = completion_tokens
        if error is not None:
            values["error"] = error

        result = await self.session.execute(
            update(Run).where(Run.id == run_id).values(**values)
        )
        return result.rowcount > 0

    async def add_step(
        self,
        run_id: int,
        *,
        seq: int,
        node: str,
        ms: Optional[int] = None,
        state_snapshot: Optional[Dict[str, Any]] = None,
    ) -> RunStep:
        """追加一个执行节点。

        `(run_id, seq)` 有唯一约束，重复写入会抛 IntegrityError ——
        **这里故意不捕获**。同一个 seq 写两次是编排层的 bug，
        吞掉它只会让轨迹静默缺失一步，而轨迹完整性正是本项目的卖点。
        """
        step = RunStep(
            run_id=run_id,
            seq=seq,
            node=node,
            ms=ms,
            # 显式给 {} 而非依赖列默认值：{} 表示"这一步没有快照"，
            # NULL 表示"没记录过"，两者语义不同，不该混
            state_snapshot=state_snapshot if state_snapshot is not None else {},
        )
        self.session.add(step)
        await self.session.flush()
        return step

    async def add_tool_call(
        self,
        run_id: int,
        *,
        seq: int,
        tool: str,
        args: Optional[Dict[str, Any]] = None,
        result_summary: Optional[str] = None,
        ms: Optional[int] = None,
        ok: bool = True,
        idempotency_key: Optional[str] = None,
    ) -> ToolCall:
        """记录一次工具调用。

        `idempotency_key` 目前只是存下来，**没有任何判重逻辑** ——
        当前该列只有普通索引不是唯一约束，靠它做"先查再写"在并发下
        两个请求会同时查空、同时执行。键的组成与正确的执行模式见
        docs/decisions/ADR-003-tool-call-idempotency-key.md，M3 落地。
        """
        call = ToolCall(
            run_id=run_id,
            seq=seq,
            tool=tool,
            args=args if args is not None else {},
            result_summary=result_summary,
            ms=ms,
            ok=ok,
            idempotency_key=idempotency_key,
        )
        self.session.add(call)
        await self.session.flush()
        return call

    async def get_by_trace(self, trace_id: str) -> Optional[Run]:
        """按 trace_id 取完整轨迹。M4 面板回放的数据源。

        **三个集合关系用 selectinload 而非 joinedload。**
        joinedload 会把它们 JOIN 进同一条 SQL，多个集合相乘产生笛卡尔积
        —— 10 步 × 5 次工具调用 × 20 条证据 = 1000 行，全是重复数据。
        selectinload 对每个关系单发一条 `WHERE run_id IN (...)`，
        共 4 条 SQL 但无冗余。

        预加载不是可选的性能优化：async SQLAlchemy 下访问未加载的关系
        会抛 MissingGreenlet，调用方拿到对象后碰 `run.steps` 就崩。
        """
        result = await self.session.execute(
            select(Run)
            .where(Run.trace_id == trace_id)
            .options(
                selectinload(Run.steps),
                selectinload(Run.tool_calls),
                selectinload(Run.evidence),
            )
        )
        return result.scalar_one_or_none()

    async def list_recent(self, limit: int = 20) -> Sequence[Run]:
        """最近的 run，倒序。面板列表用。

        **不预加载关联**：列表页只显示 trace_id、状态、耗时这些标量字段，
        为 20 条 run 各拉一遍步骤与证据是纯浪费。需要详情时再调
        get_by_trace —— 这个区分是故意的，不是遗漏。
        """
        result = await self.session.execute(
            select(Run).order_by(desc(Run.created_at)).limit(limit)
        )
        return result.scalars().all()

    async def list_unfinished(self, older_than: datetime) -> Sequence[Run]:
        """仍是 running 且早于给定时间的 run。

        用途是进程崩溃后的清理：这些 run 的 finish() 永远不会被调用，
        留在库里会让"平均耗时"这类聚合失真。谁来调、多久扫一次是
        service 的决定，Repository 只提供查询。
        """
        result = await self.session.execute(
            select(Run)
            .where(Run.status == "running", Run.created_at < older_than)
            .order_by(Run.created_at)
        )
        return result.scalars().all()

    async def count_by_status(self) -> Dict[str, int]:
        """各状态的 run 数量。供 /stats 与面板顶部统计用。"""
        from sqlalchemy import func

        result = await self.session.execute(
            select(Run.status, func.count()).group_by(Run.status)
        )
        return {status: count for status, count in result.all()}
