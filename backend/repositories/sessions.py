"""会话与消息的读写

替换 `chat_service.py` 里的三个内存字典（`sessions` / `resolvers` /
`session_timestamps`）。内存态的问题是进程重启即丢、多副本各存一份、
`cleanup_old_sessions` 还得有人记得调。

**本模块只负责存取，不负责把 Message 行还原成 ConversationManager。**
那个转换涉及"取最近几轮"、"如何拼上下文"这类业务判断，属于 service。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Sequence

from sqlalchemy import delete, desc, select, update
from sqlalchemy.exc import IntegrityError

from ..db.models import Message, Session, utcnow
from .base import BaseRepository


class SessionRepository(BaseRepository):
    """会话与其消息。messages 依附于 session，同一个聚合。"""

    async def get_or_create(
        self,
        session_id: str,
        *,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Session:
        """取会话，不存在则建。

        **并发安全的写法**：两个请求带同一个 session_id 同时进来时，
        "先 SELECT 没有就 INSERT"会两边都查空、两边都插、后者撞主键。

        这里用 SAVEPOINT（`begin_nested`）包住 INSERT：撞了就只回滚这个
        保存点，外层事务不受影响，然后 SELECT 拿另一个请求刚插入的行。
        不能直接 `session.rollback()` —— 那会把调用方在同一事务里做的
        其它写入一起丢掉，而 Repository 无权决定这件事。

        这与"不引入分布式锁"是同一条推理：唯一性由数据库主键保证，
        应用层只需要正确处理冲突。
        """
        existing = await self.session.get(Session, session_id)
        if existing is not None:
            return existing

        try:
            async with self.session.begin_nested():
                created = Session(id=session_id, meta=meta if meta is not None else {})
                self.session.add(created)
            return created
        except IntegrityError:
            # 另一个并发请求抢先插入了。保存点已回滚，外层事务仍可用。
            found = await self.session.get(Session, session_id)
            if found is None:
                # 冲突不是主键重复引起的（比如 meta 违反了某个约束），
                # 吞掉会让调用方拿到一个不存在的会话
                raise
            return found

    async def touch(self, session_id: str) -> bool:
        """更新最后活跃时间。返回是否命中。

        单独一个方法而不是塞进 get_or_create：读会话不代表有活动，
        而过期清理依据的是"最后一次交互"。
        """
        result = await self.session.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(last_active_at=utcnow())
        )
        return result.rowcount > 0

    async def append_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        run_id: Optional[int] = None,
    ) -> Message:
        """追加一条消息，并顺带刷新会话活跃时间。

        `run_id` 只有 assistant 消息有 —— 用户消息不由任何 run 产生。
        模型里它是 nullable 就是为此。
        """
        message = Message(
            session_id=session_id,
            role=role,
            content=content,
            run_id=run_id,
        )
        self.session.add(message)
        # 活跃时间与消息在同一事务里更新：分开会出现"有消息但会话看着过期"
        await self.session.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(last_active_at=utcnow())
        )
        await self.session.flush()
        return message

    async def history(
        self,
        session_id: str,
        *,
        limit: Optional[int] = None,
    ) -> Sequence[Message]:
        """按时间正序取历史消息。

        带 limit 时取的是**最近** limit 条，但返回仍是正序 ——
        对话上下文需要按时间读，而截断要从旧的那头砍。
        先倒序取 N 条再翻转，比正序取全部再切片少读很多行。

        走的是 ix_messages_session_created 复合索引。
        """
        if limit is None:
            result = await self.session.execute(
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(Message.created_at, Message.id)
            )
            return result.scalars().all()

        result = await self.session.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(desc(Message.created_at), desc(Message.id))
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))

    async def clear(self, session_id: str) -> bool:
        """删除会话及其全部消息。返回是否存在过。

        消息由外键 ondelete=CASCADE 连带删除，不在这里逐条删 ——
        应用层循环删除既慢又可能中途失败留下孤儿行。
        """
        existing = await self.session.get(Session, session_id)
        if existing is None:
            return False
        await self.session.delete(existing)
        await self.session.flush()
        return True

    async def cleanup_expired(self, timeout_seconds: int = 3600) -> int:
        """清理超时未活跃的会话，返回删除数量。

        用一条 DELETE 而非"查出来再逐个删"：后者要 N+1 次往返，
        且查询与删除之间的窗口里会话可能又活跃了。

        注意 ORM 级联（cascade="all, delete-orphan"）对批量 DELETE 语句
        不生效 —— 它靠的是把对象加载进 session。这里能连带删除消息，
        依赖的是**数据库外键上的 ondelete=CASCADE**，两者是不同机制。
        """
        cutoff = utcnow() - timedelta(seconds=timeout_seconds)
        result = await self.session.execute(
            delete(Session).where(Session.last_active_at < cutoff)
        )
        return result.rowcount

    async def count_active(self, within_seconds: int = 3600) -> int:
        """近期活跃会话数。替换 /stats 里的 `len(chat_service.sessions)`。"""
        from sqlalchemy import func

        cutoff = utcnow() - timedelta(seconds=within_seconds)
        result = await self.session.execute(
            select(func.count())
            .select_from(Session)
            .where(Session.last_active_at >= cutoff)
        )
        return int(result.scalar_one())
