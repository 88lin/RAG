"""
相关性与融合排序 —— 全系统分数口径的唯一来源

本模块存在的理由是消除三个历史缺陷：

1. `SIMILARITY_THRESHOLD` 一个数字承担两种物理量：
   在一处当作 hybrid_score 下限（越大越好），在另一处当作余弦距离上限（越小越好）。
2. Hybrid 融合把两种不同量纲的分数加权相加：向量侧用绝对距离转换，
   BM25 侧用 `score / max_score` 相对归一化。后者使"本批最好的"恒为 1.0，
   即使它完全不相关，因此权重参数没有物理意义。
3. 展示层用 `or` 链在 rerank_score / hybrid_score / 1-distance 之间回退，
   量纲混用，且 0.0 会被判为 falsy 而跳过。

模块内两个函数职责严格分离，不可混用：

    rrf_fuse()          -> 只用于排序。分数量级约 0.016~0.033，无相关性语义，不可展示。
    compute_relevance() -> 只用于展示与阈值判断。恒在 [0,1]，越大越相关。

纯函数，无 IO，无全局状态。
"""
import math
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# RRF 平滑常数，来自 Cormack et al. 2009 (SIGIR)。
# 作用是压平头部名次差距：k=0 时第 1 名 1.0、第 2 名 0.5，头部权重过大；
# k=60 时两者为 0.0164 与 0.0161，差距温和，
# 使"两路都排中游"能胜过"一路第一、另一路缺席"，这正是融合想要的语义。
RRF_K_DEFAULT = 60


def compute_relevance(result: Dict[str, Any]) -> float:
    """计算单条检索结果的相关性，用于展示与阈值判断。
    
    优先级：rerank_logit > cosine_distance > 无信息。
    rerank 优先的理由是 cross-encoder 让 query 与 doc 相互注意，
    比双塔向量的独立编码更准，是精排阶段的结论。

    ┌─ 两条映射的物理依据 ────────────────────────────────────────┐
    │ rerank_logit → sigmoid(logit)                              │
    │   bge-reranker 以二分类交叉熵训练，logit 过 sigmoid 后就是   │
    │   模型自身估计的"这对 query-doc 相关"的概率。               │
    │   这不是我们编的映射，是模型原生语义，因此可直接展示。       │
    │                                                            │
    │ cosine_distance → 1 - d/2                                  │
    │   归一化向量的余弦距离 ∈ [0,2]，线性映射到 [1,0]。          │
    └────────────────────────────────────────────────────────────┘

    参数:
        result: 检索结果字典。读取 `rerank_logit`（float）
                与 `cosine_distance`（float）两个可选键。

    返回:
        float — 恒在 [0.0, 1.0]，越大越相关。
        无有效分数信息时返回 0.0（语义为"无证据表明相关"）。
        绝不返回 None：该值会流向阈值比较与前端渲染，
        None 会在下游炸成 TypeError。

    实现要求:
        1. logit 可达 ±11，极端情况下朴素 `exp(-x)` 会 OverflowError，
           必须分支处理正负号。
        2. 距离可能因浮点误差或非归一化向量越界，必须夹紧到 [0,1]。
        3. 距离可能是 None（ChromaDB 已观察到该行为）或非数值，
           必须视为"无信息"而非抛异常。
        4. 判断字段存在性时不要用真值测试 —— `0.0` 是合法的 logit
           （对应 relevance 0.5），用 `or`/`if x` 会把它误判为缺失。
           这正是本模块要修掉的原始 bug。
    """

    # bool 是 int 的子类，isinstance(True, int) 为真。
    # 显式排除，避免 rerank_logit=True 被当成 1.0。
    def _as_number(value: Any) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    # 逐级回退：rerank_logit 存在但值不可用时，仍应尝试 cosine_distance，
    # 而不是直接判定为"无信息"。
    logit = _as_number(result.get("rerank_logit"))
    if logit is not None:
        # 分支处理正负号：logit 可达 ±11，极端值下朴素 exp(-x) 会 OverflowError。
        # logit >= 0 时 exp(-logit) 收敛到 0；logit < 0 时改用 exp(logit)，同样收敛。
        if logit >= 0:
            return 1.0 / (1.0 + math.exp(-logit))
        exp_logit = math.exp(logit)
        return exp_logit / (1.0 + exp_logit)

    distance = _as_number(result.get("cosine_distance"))
    if distance is not None:
        # 夹紧的是输出的 relevance，不是输入的距离。
        # 余弦距离值域为 [0,2]，若先把距离夹到 [0,1]，则 [1,2] 区间的距离
        # 会全部塌陷到 relevance=0.5 —— "完全相反"与"正交"变得同样相关，
        # 且恰好卡在 ANSWERABLE_MIN_RELEVANCE 门槛上。
        return max(0.0, min(1.0, 1.0 - distance / 2.0))

    return 0.0


def rrf_fuse(
    *ranked_lists: Sequence[str],
    k: int = RRF_K_DEFAULT,
) -> List[Tuple[str, float]]:
    """Reciprocal Rank Fusion —— 只用排名融合多路检索结果。

    ┌─ 为什么用 RRF 取代加权归一化 ──────────────────────────────┐
    │ 向量的余弦距离与 BM25 的 TF-IDF 得分是不同量纲，            │
    │ 没有数学依据能把它们加权相加。任何归一化都是在强行造可比性。 │
    │                                                            │
    │ RRF 只看"排第几"，绕开整个问题：                            │
    │   score(d) = Σ_i 1 / (k + rank_i(d))                       │
    │                                                            │
    │ BM25 原始分是 3.7 还是 3700 完全不影响结果。               │
    │ 因此不需要归一化，也不需要调 VECTOR_WEIGHT / BM25_WEIGHT。 │
    └────────────────────────────────────────────────────────────┘

    参数:
        *ranked_lists: 若干路已排好序的 doc_id 序列，每路第 0 个元素是该路第 1 名。
                       空序列会被安全忽略。
        k: 平滑常数，必须 > 0。

    返回:
        List[Tuple[str, float]] — (doc_id, rrf_score)，按分数降序。
        分数仅供排序，无相关性语义，不可展示给用户，
        不可与 compute_relevance() 的返回值比较或混用。

    异常:
        ValueError — k <= 0。k=0 会退化为朴素倒数排名使头部权重过大，
                     k<0 可能除零。

    实现要求:
        1. rank 从 1 开始计（第 0 个元素是第 1 名）。
        2. 同一路内出现重复 doc_id 时只按其最好排名计一次，
           否则一路内重复即可刷分。跨路重复是正常累加。
        3. 排序需稳定，便于测试与复现。
    """
    if k <= 0:
        raise ValueError(f"RRF 平滑常数 k 必须 > 0，当前为 {k}")

    scores: Dict[str, float] = {}
    for ranked_list in ranked_lists:
        # 每路独立去重：同一路内重复出现的 doc_id 只按其最好排名计一次，
        # 否则一路内重复即可刷分。跨路重复是正常累加。
        seen: Set[str] = set()
        for rank, doc_id in enumerate(ranked_list, start=1):
            if doc_id in seen:
                continue
            seen.add(doc_id)
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)

    # Python 的 sorted 是稳定排序，同分项保持首次出现顺序，便于测试与复现。
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


