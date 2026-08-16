"""Repository 层的共同契约

Repository 只做一件事：**对象与数据库行之间的翻译**。
它不知道任何业务规则，不判断"证据够不够"、"该不该重试"。
那些属于 service 层。

## 三条硬约束

1. **不管理事务，不 commit。**
   session 由调用方传入，事务边界由调用方决定。理由是原子性需求是
   业务级的 —— "创建 run 和写入 evidence 要么都成功要么都失败"这种
   要求只有 service 知道。若每个方法自己 commit，跨表原子性就没了，
   而且失败时会留下半成功状态。
   提交由 `session_scope()` 在退出时统一做。

2. **需要自增 id 时用 `flush()` 而非 `commit()`。**
   flush 把待写入发给数据库拿回主键，但不结束事务 —— 出错仍可整体回滚。

3. **查询集合关系必须显式预加载。**
   async SQLAlchemy 下访问未加载的关系会抛 `MissingGreenlet`（懒加载
   要在访问属性时临时发 SQL，异步上下文里做不到）。这不是可选的性能
   优化，是"能不能跑"的问题。

## 为什么不直接在 service 里写 SQL

- M4 的面板要复用同一套查询。散在 service 里就得再写一遍，
  两份 SQL 查同一批表，改一处忘另一处是迟早的事。
- `chat_service.py` 已经 561 行，再塞十几处 SQL 会变成没人敢动的文件。
- service 的测试可以塞假 Repository，不必起数据库。

反过来说：**只有一处调用且不会被复用的查询，直接写在 service 里更诚实。**
Repository 里全是一行透传的 `get_by_id` 时，这层就只是噪音。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class BaseRepository:
    """持有 session 的基类。没有共同行为，只固化"session 由外部传入"这一点。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
