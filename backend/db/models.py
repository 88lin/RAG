"""
ORM 模型 —— 可追溯轨迹的持久化结构

设计原则：

1. **向量不进这里**。evidence 表只存 chunk_id 这个字符串指针，
   指向 ChromaDB 里的切片。本表存的是"这次 run 用了哪些证据、
   各自得分多少、有没有被答案采用"这层关系。
2. **JSON 字段用 JSONB（PG）而非 TEXT**：需要按字段查询，
   例如"找出所有调用了 calculate 且失败的 run"。
   SQLite 上退化为 JSON，功能受限但不影响本地开发。
3. **时间戳统一用带时区的 UTC**。跨时区部署时本地时间无法比较，
   而 naive datetime 在 PG 与 SQLite 上的行为不一致。
4. **外键带 ondelete**：删除 run 应级联清掉其步骤与证据，
   否则孤儿行会越积越多且无人清理。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, TypeDecorator


def utcnow() -> datetime:
    """带时区的当前时间。

    不用 datetime.utcnow()：它返回 naive datetime，
    与带时区的列混用时会在比较处静默出错。
    """
    return datetime.now(timezone.utc)


class JSONField(TypeDecorator):
    """在 PG 上用 JSONB、其他方言用 JSON。

    JSONB 支持按字段索引与查询，是 PG 上的正确选择；
    但 SQLite 不认识它，直接用会在建表时报错。
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


# 自增主键的类型。**不能直接用 BigInteger**：
# SQLite 的隐式自增要求列类型名恰好是 "INTEGER"，而 BigInteger 渲染成
# "BIGINT" —— 它仍有整数亲和性，但不是 rowid 别名，于是插入时主键拿不到
# 自增值，直接报 "NOT NULL constraint failed"。
# PG 上必须是 BIGINT：轨迹表按每次问答若干行的速度增长，
# INTEGER 的 21 亿上限不是安全余量。
# 因此按方言取变体，而不是二选一。
PrimaryKeyInt = BigInteger().with_variant(Integer, "sqlite")


class UtcDateTime(TypeDecorator):
    """始终返回带时区（UTC）的 datetime。

    **`DateTime(timezone=True)` 不足以保证这件事。** PG 的 TIMESTAMPTZ 本来
    就返回 aware datetime，但 SQLite 没有原生时间类型 —— 存的是字符串，
    读回来一律 naive，`timezone=True` 在它上面只是个被忽略的提示。

    结果是同一份代码在两个库上拿到不同类型的对象：
    `run.created_at > utcnow()` 在 PG 上正常，在 SQLite 上抛
    "can't compare offset-naive and offset-aware datetimes"。
    这类差异必须在类型层抹平，否则每个调用点都要自己判一次。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Optional[datetime], dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            # naive 一律按 UTC 解释，不猜本地时区：
            # 猜错会写进偏移几小时的时间戳，而这种错误在数据里看不出来
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: Optional[datetime], dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


# ============================================================
# 会话与消息
# ============================================================

class Session(Base):
    """一次对话会话。替换 chat_service 里的内存字典。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False
    )
    # 用于清理过期会话，每次交互更新
    last_active_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False, index=True
    )
    meta: Mapped[Dict[str, Any]] = mapped_column(JSONField, default=dict, nullable=False)

    messages: Mapped[List["Message"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class Message(Base):
    """一条消息。run_id 指向产生该回答的 run（用户消息为空）。"""

    __tablename__ = "messages"
    __table_args__ = (
        # 按会话取历史是主查询，且要按时间排序
        Index("ix_messages_session_created", "session_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyInt, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user / assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False
    )
    run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )

    session: Mapped["Session"] = relationship(back_populates="messages")
    citations: Mapped[List["Citation"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


# ============================================================
# 可追溯的核心
# ============================================================

class Run(Base):
    """一次端到端执行。

    trace_id 是对外暴露的标识：前端展示它、用户报障时提供它、
    面板按它回放。用 uuid 而非自增 id 是为了不暴露业务量级，
    也便于在日志里全局搜索。
    """

    __tablename__ = "runs"
    __table_args__ = (
        # 面板列出最近的 run
        Index("ix_runs_created_desc", "created_at"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyInt, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True,
        default=lambda: str(uuid.uuid4()),
    )
    session_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )

    route: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    # running / ok / error / cancelled —— 不用枚举类型：
    # 新增状态时 PG 的 ALTER TYPE 需要迁移，而这里状态集合会随 M3 扩展
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)

    total_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 首 token 延迟单独记：它决定用户的感知速度，与总耗时是两个指标
    first_token_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        UtcDateTime, nullable=True
    )

    steps: Mapped[List["RunStep"]] = relationship(
        back_populates="run", cascade="all, delete-orphan",
        order_by="RunStep.seq",
    )
    tool_calls: Mapped[List["ToolCall"]] = relationship(
        back_populates="run", cascade="all, delete-orphan",
        order_by="ToolCall.seq",
    )
    evidence: Mapped[List["Evidence"]] = relationship(
        back_populates="run", cascade="all, delete-orphan",
        order_by="Evidence.rank",
    )


class RunStep(Base):
    """执行轨迹中的一个节点。state_snapshot 是 M4 面板回放的数据源。"""

    __tablename__ = "run_steps"
    __table_args__ = (
        # 同一 run 内 seq 唯一：重复写入是 bug，让数据库拦住
        UniqueConstraint("run_id", "seq", name="uq_run_steps_run_seq"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyInt, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    node: Mapped[str] = mapped_column(String(64), nullable=False)
    ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    state_snapshot: Mapped[Dict[str, Any]] = mapped_column(
        JSONField, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False
    )

    run: Mapped["Run"] = relationship(back_populates="steps")


class ToolCall(Base):
    """一次工具调用。

    idempotency_key = sha1(tool + canonical_json(args) + kb_version)。
    它同时是防 Agent 死循环刷同一工具的兜底 —— 相同 key 的调用可以
    直接返回上次结果。
    """

    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_tool_calls_run_seq"),
        Index("ix_tool_calls_key", "idempotency_key"),
        # 按工具名与成败聚合，用于"哪个工具最常失败"这类查询
        Index("ix_tool_calls_tool_ok", "tool", "ok"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyInt, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    args: Mapped[Dict[str, Any]] = mapped_column(JSONField, default=dict, nullable=False)
    # 只存摘要不存全文：工具结果可能很大（web_search 的网页正文），
    # 全量入库会让这张表迅速膨胀
    result_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False
    )

    run: Mapped["Run"] = relationship(back_populates="tool_calls")


class Evidence(Base):
    """本次 run 用到的一条证据。

    relevance 与检索面板、引用卡片同一口径（rag.scoring.compute_relevance），
    存 Numeric 而非 Float：需要精确比较与聚合，浮点误差在 SQL 聚合里会放大。
    """

    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_chunk", "chunk_id"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyInt, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_id: Mapped[str] = mapped_column(String(128), nullable=False)
    file: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    relevance: Mapped[Optional[float]] = mapped_column(Numeric(6, 4), nullable=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    # 哪几路召回命中了它（vector / bm25），存逗号分隔字符串而非数组：
    # PG 的 ARRAY 类型在 SQLite 上不可用，而这个字段只用于展示
    retrieved_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 是否真被答案引用。区分"检索到"与"用上了"是评估检索精度的基础
    used_in_answer: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    run: Mapped["Run"] = relationship(back_populates="evidence")


class Citation(Base):
    """答案中的一处引用。

    verified / verify_score 由 M3 的 citation_verify 填充：
    用 cross-encoder 算 (答案句, 被引 chunk) 的支持度。
    在那之前保持 NULL —— NULL 表示"未校验"，与 False（"校验未通过"）
    是不同语义，不能用默认值混掉。
    """

    __tablename__ = "citations"
    __table_args__ = (
        Index("ix_citations_message", "message_id", "sentence_idx"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyInt, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    sentence_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_id: Mapped[str] = mapped_column(String(128), nullable=False)
    verified: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    verify_score: Mapped[Optional[float]] = mapped_column(Numeric(6, 4), nullable=True)

    message: Mapped["Message"] = relationship(back_populates="citations")


# ============================================================
# 反馈与评测
# ============================================================

class Feedback(Base):
    """用户对某条回答的反馈。"""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(PrimaryKeyInt, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # -1 / 0 / 1
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False
    )


class EvalRun(Base):
    """一次评测的元信息。与 M1 的 JSONL 产物对应，便于跨次对比。"""

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(PrimaryKeyInt, primary_key=True, autoincrement=True)
    suite: Mapped[str] = mapped_column(String(64), nullable=False)   # t2ranking / faithfulness
    variant: Mapped[str] = mapped_column(String(64), nullable=False)  # rrf / vector_bge ...
    started_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        UtcDateTime, nullable=True
    )
    meta: Mapped[Dict[str, Any]] = mapped_column(JSONField, default=dict, nullable=False)

    results: Mapped[List["EvalResult"]] = relationship(
        back_populates="eval_run", cascade="all, delete-orphan"
    )


class EvalResult(Base):
    """逐条评测结果。metrics 用 JSON：指标集合会随阶段变化，
    加一个指标不该需要一次迁移。"""

    __tablename__ = "eval_results"
    __table_args__ = (
        Index("ix_eval_results_run_qid", "eval_run_id", "qid"),
    )

    id: Mapped[int] = mapped_column(PrimaryKeyInt, primary_key=True, autoincrement=True)
    eval_run_id: Mapped[int] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
    )
    qid: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics: Mapped[Dict[str, Any]] = mapped_column(
        JSONField, default=dict, nullable=False
    )

    eval_run: Mapped["EvalRun"] = relationship(back_populates="results")


# ============================================================
# 异步摄入
# ============================================================

class IngestTask(Base):
    """一个文档摄入任务。

    进度写 Redis（高频更新，不该打数据库），终态写这里 ——
    Redis 丢了要能从 PG 知道任务是否完成。
    """

    __tablename__ = "ingest_tasks"
    __table_args__ = (
        Index("ix_ingest_tasks_status", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(64), default="uploaded", nullable=False)
    # pending / running / done / error
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    chunk_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=utcnow, nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        UtcDateTime, nullable=True
    )
