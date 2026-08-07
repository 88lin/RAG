"""
检索方案（variant）定义

每个 variant 是一组检索配置，评测的目的是量化它们的差异。
定义集中在这里而不散在脚本里，理由是报告、runner、语料构建
三处都要引用同一份定义，重复定义必然漂移。

collection 隔离：不同 embedding 模型的向量维度不同（384 vs 512），
混入同一 collection 会直接报错；维度相同则静默返回垃圾结果。
M0 已实现按模型名派生 collection 名，这里复用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

MINILM = "all-MiniLM-L6-v2"
BGE_ZH = "BAAI/bge-small-zh-v1.5"


@dataclass(frozen=True)
class Variant:
    """一个检索方案。

    字段:
        id: 报告中的标识
        embedding_model: 用于向量召回；bm25 方案也需要它来定位 collection
                         （BM25 索引从 collection 的文档构建）
        use_vector: 是否启用向量召回
        use_bm25: 是否启用 BM25 召回
        use_rerank: 是否启用 cross-encoder 精排
        note: 报告中说明该方案存在的意义
    """

    id: str
    embedding_model: str
    use_vector: bool
    use_bm25: bool
    use_rerank: bool
    note: str

    @property
    def fusion(self) -> str:
        """融合方式，供报告显示。"""
        if self.use_vector and self.use_bm25:
            return "rrf"
        return "none"

    def describe(self) -> str:
        parts: List[str] = []
        if self.use_vector:
            parts.append(f"vector({self.embedding_model.split('/')[-1]})")
        if self.use_bm25:
            parts.append("bm25")
        pipeline = " + ".join(parts) or "none"
        if self.use_vector and self.use_bm25:
            pipeline = f"RRF({pipeline})"
        if self.use_rerank:
            pipeline += " -> rerank"
        return pipeline


# 五个方案的设计意图：
#   前两个隔离出"换 embedding 模型"的收益（唯一变量是模型）
#   bm25 给出关键词路的独立贡献，作为 RRF 增益的分母
#   rrf 是 M0 的默认配置
#   rrf_rerank 量化精排的收益与延迟代价
VARIANTS: Dict[str, Variant] = {
    "vector_minilm": Variant(
        id="vector_minilm",
        embedding_model=MINILM,
        use_vector=True,
        use_bm25=False,
        use_rerank=False,
        note="baseline。英文模型跑中文语料，用于量化换模型的收益",
    ),
    "vector_bge": Variant(
        id="vector_bge",
        embedding_model=BGE_ZH,
        use_vector=True,
        use_bm25=False,
        use_rerank=False,
        note="单路向量上限。与 vector_minilm 的唯一差异是模型",
    ),
    "bm25": Variant(
        id="bm25",
        embedding_model=BGE_ZH,  # 仅用于定位 collection，不做向量召回
        use_vector=False,
        use_bm25=True,
        use_rerank=False,
        note="关键词路的独立贡献，作为 RRF 增益的对照",
    ),
    "rrf": Variant(
        id="rrf",
        embedding_model=BGE_ZH,
        use_vector=True,
        use_bm25=True,
        use_rerank=False,
        note="M0 的默认配置。RRF 只用排名，无权重参数",
    ),
    "rrf_rerank": Variant(
        id="rrf_rerank",
        embedding_model=BGE_ZH,
        use_vector=True,
        use_bm25=True,
        use_rerank=True,
        note="精排增益与延迟代价。cross-encoder 让 query 与 doc 相互注意",
    ),
}

DEFAULT_ORDER = [
    "vector_minilm",
    "vector_bge",
    "bm25",
    "rrf",
    "rrf_rerank",
]


def get(variant_id: str) -> Variant:
    if variant_id not in VARIANTS:
        raise ValueError(
            f"未知 variant={variant_id!r}，合法值: {sorted(VARIANTS)}"
        )
    return VARIANTS[variant_id]


def eval_collection_name(embedding_model: str) -> str:
    """评测语料的 collection 名。

    与生产 collection 分开（前缀 eval_）：评测语料是 T2Ranking 的
    抽样段落，不应混入用户的知识库，否则生产检索会命中评测数据。
    """
    import re

    slug = re.sub(r"[^a-z0-9]+", "_", embedding_model.lower()).strip("_")
    return f"eval_t2ranking__{slug}"
