"""
检索层评测指标 —— 纯函数，无 IO，无全局状态

自己实现而不用 Ragas/DeepEval 的理由：
Recall@k / MRR / nDCG 都是确定性的集合运算，可单测、可复现、可逐条解释。
让 LLM judge 介入只会引入不必要的方差。
生成层的忠实度、答案正确性才交给 Ragas —— 那些确实需要语义判断。

相关性口径：qrels 只标注"相关/不相关"（无等级），故一律按二元处理。
nDCG 的 IDCG 因此按 min(len(gold), k) 个理想位置计算。

约定（所有函数一致）：
  - k <= 0 抛 ValueError（无意义的参数，静默返回 0 会掩盖调用方 bug）
  - gold 为空返回 0.0，不除零。无答案查询应当用无答案识别率单独评估，
    不能混进 Recall 平均值 —— 否则一批无答案查询会把整体指标拉低，
    看起来像检索变差了。
  - retrieved 中重复的 doc_id 只计一次命中，但仍占据排名位置
    （与检索系统的真实行为一致）
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Set


def _validate_k(k: int) -> None:
    if k <= 0:
        raise ValueError(f"k 必须 > 0，当前为 {k}")


def recall_at_k(retrieved: Sequence[str], gold: Set[str], k: int) -> float:
    """Recall@k —— 前 k 条结果覆盖了多少比例的 gold。

    参数:
        retrieved: 检索结果的 doc_id 序列，第 0 个是第 1 名
        gold: 人工标注的相关 doc_id 集合
        k: 截断位置

    返回:
        float ∈ [0,1]。gold 为空时返回 0.0。
    """
    _validate_k(k)
    if not gold:
        return 0.0

    top_k = set(retrieved[:k])
    return len(top_k & gold) / len(gold)


def mrr_at_k(retrieved: Sequence[str], gold: Set[str], k: int) -> float:
    """MRR@k —— 第一个命中位置的倒数。

    只看最靠前的那个 gold：衡量"用户要往下翻多少条才能看到正确答案"。
    与 Recall 互补 —— Recall 高但 MRR 低意味着正确文档都在靠后位置。

    返回:
        float ∈ [0,1]。前 k 条无命中时返回 0.0。
    """
    _validate_k(k)
    if not gold:
        return 0.0

    for rank, doc_id in enumerate(retrieved[:k], start=1):
        if doc_id in gold:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], gold: Set[str], k: int) -> float:
    """nDCG@k —— 考虑全部命中位置的折损累积增益，二元相关性。

    与 MRR 的区别：MRR 只看第一个命中，nDCG 关心所有 gold 的整体排布。
    两个都报是因为它们回答不同问题。

    DCG  = Σ_{命中位置 i} 1 / log2(i + 1)
    IDCG = Σ_{i=1..min(|gold|, k)} 1 / log2(i + 1)

    IDCG 必须按 min(|gold|, k) 截断：若 gold 有 20 个而 k=5，
    用 20 个理想位置做分母会让 nDCG 永远上不去，指标失去可比性。

    返回:
        float ∈ [0,1]。
    """
    _validate_k(k)
    if not gold:
        return 0.0

    dcg = 0.0
    counted: Set[str] = set()
    for rank, doc_id in enumerate(retrieved[:k], start=1):
        # 重复的 doc_id 不重复计入增益，但它占掉的排名位置不退还
        if doc_id in gold and doc_id not in counted:
            counted.add(doc_id)
            dcg += 1.0 / math.log2(rank + 1)

    ideal_hits = min(len(gold), k)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))

    return dcg / idcg if idcg > 0 else 0.0


def latency_percentiles(samples: Sequence[float]) -> Dict[str, float]:
    """延迟分位统计。

    报告必须给出 P50/P95 而不只是平均值：平均值会被长尾掩盖，
    而用户体验取决于尾部延迟。Rerank 的代价尤其体现在 P95。

    采用最近秩插值（与 numpy 的 linear 插值一致），
    样本少时不会产生越界索引。

    参数:
        samples: 延迟样本（单位由调用方决定，建议毫秒）

    返回:
        含 p50 / p95 / p99 / mean / max / count 的字典。
        空输入返回全 0 —— 某个 variant 全部失败时仍要能出报告。
    """
    if not samples:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0,
                "mean": 0.0, "max": 0.0, "count": 0}

    ordered = sorted(samples)
    n = len(ordered)

    def percentile(q: float) -> float:
        if n == 1:
            return float(ordered[0])
        pos = q * (n - 1)
        low = math.floor(pos)
        high = math.ceil(pos)
        if low == high:
            return float(ordered[int(pos)])
        weight = pos - low
        return float(ordered[low] * (1 - weight) + ordered[high] * weight)

    return {
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "mean": sum(ordered) / n,
        "max": float(ordered[-1]),
        "count": n,
    }


def aggregate(
    per_query: List[Dict[str, float]],
    keys: Sequence[str],
) -> Dict[str, float]:
    """对逐条指标取宏平均（macro-average）。

    用宏平均而非微平均：每条 query 权重相同，
    否则 gold 多的 query（本数据集最多 23 个）会主导整体分数。

    参数:
        per_query: 每条 query 的指标字典
        keys: 需要聚合的指标名

    返回:
        指标名 -> 平均值。per_query 为空时返回全 0。
    """
    if not per_query:
        return {key: 0.0 for key in keys}

    return {
        key: sum(item.get(key, 0.0) for item in per_query) / len(per_query)
        for key in keys
    }
