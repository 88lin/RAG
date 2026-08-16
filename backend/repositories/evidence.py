"""证据的读写

evidence 表记录的是"这次 run 用了哪些 chunk、各自得分多少、
有没有被答案采用"这层**关系**。向量本身留在 ChromaDB，
这里只存 chunk_id 这个字符串指针。

区分"检索到"与"被答案用上"是评估检索精度的基础：
召回 20 条但答案只引用 2 条，说明 top-k 设大了或排序不准。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import case, func, select, update

from ..db.models import Evidence
from .base import BaseRepository


class EvidenceRepository(BaseRepository):
    """一次 run 检索到的证据。"""

    async def bulk_insert(
        self,
        run_id: int,
        results: Iterable[Dict[str, Any]],
    ) -> List[Evidence]:
        """批量写入检索结果。

        `results` 是检索层返回的原始结构，本方法只做字段搬运，
        **不计算 relevance** —— 分数口径的唯一来源是 `rag/scoring.py`，
        在这里重算等于制造第二个真相源。调用方负责先算好。

        rank 用枚举序号而非从 results 里取：排名是"这批结果里的第几位"，
        由列表顺序定义。让调用方传 rank 会给出两者不一致的机会。
        """
        rows: List[Evidence] = []
        for rank, item in enumerate(results):
            relevance = item.get("relevance")
            rows.append(
                Evidence(
                    run_id=run_id,
                    chunk_id=str(item["chunk_id"]),
                    file=item.get("file"),
                    # 显式判 None：relevance=0.0 是合法分数（极低相关），
                    # 用 `item.get("relevance") or None` 会把它变成 NULL
                    relevance=relevance if isinstance(relevance, (int, float)) else None,
                    rank=rank,
                    # 逗号分隔而非 PG ARRAY：ARRAY 在 SQLite 上不可用，
                    # 而这个字段只用于展示，不参与查询
                    retrieved_by=_join_sources(item.get("retrieved_by")),
                    used_in_answer=False,
                )
            )

        if not rows:
            # add_all([]) 本身无害，但提前返回省掉一次 flush 往返
            return []

        self.session.add_all(rows)
        await self.session.flush()
        return rows

    async def mark_used(self, run_id: int, chunk_ids: Sequence[str]) -> int:
        """把答案实际引用的 chunk 标记为已采用，返回更新行数。

        生成结束、引用抽取完成后调用。分两步（先 bulk_insert 再 mark_used）
        而不是等答案生成完再一次性写入：证据要在检索阶段就落库，
        这样即使生成失败也能看到"当时检索到了什么"。
        """
        if not chunk_ids:
            return 0

        result = await self.session.execute(
            update(Evidence)
            .where(Evidence.run_id == run_id, Evidence.chunk_id.in_(chunk_ids))
            .values(used_in_answer=True)
        )
        return result.rowcount

    async def list_for_run(self, run_id: int) -> Sequence[Evidence]:
        """按排名取某次 run 的证据。"""
        result = await self.session.execute(
            select(Evidence).where(Evidence.run_id == run_id).order_by(Evidence.rank)
        )
        return result.scalars().all()

    async def usage_stats(self, run_id: int) -> Dict[str, int]:
        """检索到多少条、其中多少条被答案采用。

        这两个数之比是检索精度的直接观测量，M4 面板顶部要显示。
        """
        result = await self.session.execute(
            select(
                func.count().label("retrieved"),
                # 用 CASE 而非直接 SUM(used_in_answer)：
                # PG 不允许对 boolean 求和，SQLite 允许 —— 依赖后者的宽容
                # 会让代码在换库时才炸。coalesce 兜住零行时 SUM 返回 NULL。
                func.coalesce(
                    func.sum(case((Evidence.used_in_answer.is_(True), 1), else_=0)),
                    0,
                ).label("used"),
            ).where(Evidence.run_id == run_id)
        )
        row = result.one()
        return {"retrieved": int(row.retrieved), "used": int(row.used)}


def _join_sources(value: Any) -> Optional[str]:
    """把 retrieved_by 规整成逗号分隔字符串。

    检索层可能给 list（["vector", "bm25"]）也可能给 str，
    容错放在这里一次，好过每个调用点各判一遍。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, (list, tuple, set)):
        joined = ",".join(str(v) for v in value if v)
        return joined or None
    return str(value)
