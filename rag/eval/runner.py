"""
检索实验 runner

职责：对一个 variant 跑完全部评测 query，逐条记录检索结果与耗时，
落 JSONL。指标计算与报告生成不在这里 —— 分开的理由是可以事后重算指标
而不必重跑检索（检索是分钟级，重算是秒级）。

与生产检索路径的两点故意差异：

1. **不做相关性阈值过滤。** 生产要"宁缺毋滥"，评测要看原始排序质量。
   若先过滤，Recall@5 会被阈值间接决定，无法区分"检索差"和"阈值严"。
2. **检索 top_k 固定为较大值（默认 20）。** 指标按 k 截断在计算阶段做，
   一次检索可同时算 Recall@1/5/10，不必为每个 k 重跑。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

from ..embedder import Embedder
from ..retriever import Retriever
from ..scoring import compute_relevance, rrf_fuse
from ..vectordb import VectorDB
from .schema import EvalQuery
from .variants import Variant, eval_collection_name

# 每条 query 召回多少候选。取 20 使 Recall@10 仍有意义，
# 同时控制 rerank 的耗时（cross-encoder 是逐对打分，代价随候选数线性增长）。
RETRIEVE_TOP_K = 20


@dataclass
class QueryResult:
    """单条 query 的检索结果与耗时。

    retrieved_ids 按检索排名顺序，指标计算只依赖这个顺序。
    """

    qid: str
    query: str
    variant: str
    retrieved_ids: List[str]
    relevances: List[float]
    gold_doc_ids: List[str]
    latency_ms: float
    stage_ms: Dict[str, float]
    error: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "QueryResult":
        return cls(**json.loads(line))


class EvalRetriever:
    """按 variant 配置执行检索，绕过生产路径的阈值过滤。

    复用 M0 的 Retriever 做底层召回（向量、BM25、rerank），
    但自己组织融合与排序，以便分阶段计时。
    """

    def __init__(self, variant: Variant):
        self.variant = variant
        collection = eval_collection_name(variant.embedding_model)

        self.vectordb = VectorDB(collection_name=collection)
        self.vectordb.get_collection()
        self.embedder = Embedder(model_name=variant.embedding_model)
        # 不传 llm：评测不需要 multi-query 扩展，那会引入 LLM 调用的方差
        self.retriever = Retriever(self.vectordb, self.embedder, llm=None)

    @property
    def corpus_size(self) -> int:
        return self.vectordb.count()

    def search(self, query: str, top_k: int = RETRIEVE_TOP_K) -> tuple:
        """执行检索，返回 (ordered_ids, relevances, stage_ms)。"""
        stage_ms: Dict[str, float] = {}
        variant = self.variant

        vector_ids: List[str] = []
        bm25_ids: List[str] = []
        by_id: Dict[str, Dict] = {}

        if variant.use_vector:
            started = time.perf_counter()
            hits = self.retriever.retrieve(query, top_k=top_k)
            stage_ms["vector"] = (time.perf_counter() - started) * 1000
            vector_ids = [h["id"] for h in hits]
            for hit in hits:
                by_id[hit["id"]] = hit

        if variant.use_bm25:
            started = time.perf_counter()
            hits = self.retriever._bm25_search(query, top_k=top_k)
            stage_ms["bm25"] = (time.perf_counter() - started) * 1000
            bm25_ids = [h["id"] for h in hits]
            for hit in hits:
                by_id.setdefault(hit["id"], hit)

        # 融合
        started = time.perf_counter()
        if variant.use_vector and variant.use_bm25:
            fused = rrf_fuse(vector_ids, bm25_ids)
            ordered = [doc_id for doc_id, _ in fused]
        else:
            ordered = vector_ids or bm25_ids
        stage_ms["fuse"] = (time.perf_counter() - started) * 1000

        # 精排
        if variant.use_rerank and ordered:
            started = time.perf_counter()
            candidates = [by_id[doc_id] for doc_id in ordered if doc_id in by_id]
            reranked = self.retriever._rerank_results(
                query, candidates, top_k=len(candidates)
            )
            stage_ms["rerank"] = (time.perf_counter() - started) * 1000
            ordered = [item["id"] for item in reranked]
            by_id.update({item["id"]: item for item in reranked})

        relevances = [
            compute_relevance(by_id.get(doc_id, {})) for doc_id in ordered
        ]
        return ordered[:top_k], relevances[:top_k], stage_ms


def run_variant(
    variant: Variant,
    queries: Sequence[EvalQuery],
    out_path: Path,
    top_k: int = RETRIEVE_TOP_K,
    progress_every: int = 25,
) -> List[QueryResult]:
    """跑一个 variant 的全部 query，逐条写 JSONL。

    逐条写入而非最后批量写：跑到一半中断时已完成的部分仍可用，
    300 条 query 在开了 rerank 时可能要跑十几分钟。

    单条 query 失败不中断整轮 —— 记 error 字段继续，
    否则一条脏数据会废掉整次实验。失败条数会在汇总时报出。
    """
    engine = EvalRetriever(variant)
    print(f"[{variant.id}] {variant.describe()}")
    print(f"  collection 条目数: {engine.corpus_size:,}")

    results: List[QueryResult] = []
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8") as fh:
        for index, item in enumerate(queries, start=1):
            started = time.perf_counter()
            try:
                ordered, relevances, stage_ms = engine.search(item.query, top_k=top_k)
                error = None
            except Exception as exc:  # noqa: BLE001 - 单条失败不应中断整轮
                ordered, relevances, stage_ms = [], [], {}
                error = f"{type(exc).__name__}: {exc}"

            result = QueryResult(
                qid=item.qid,
                query=item.query,
                variant=variant.id,
                retrieved_ids=ordered,
                relevances=[round(value, 4) for value in relevances],
                gold_doc_ids=sorted(item.gold_doc_ids),
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                stage_ms={key: round(value, 2) for key, value in stage_ms.items()},
                error=error,
            )
            fh.write(result.to_json() + "\n")
            results.append(result)

            if index % progress_every == 0 or index == len(queries):
                failed = sum(1 for item in results if item.error)
                print(
                    f"  {index}/{len(queries)} 条完成"
                    + (f"，失败 {failed}" if failed else ""),
                    flush=True,
                )

    return results


def read_results(path: Path) -> Iterator[QueryResult]:
    """读回逐条结果，用于重算指标而不重跑检索。"""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield QueryResult.from_json(line)
