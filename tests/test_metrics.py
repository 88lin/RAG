"""
rag/eval/metrics.py 单元测试

这份测试同时是指标的规格说明。检索层指标自己实现而不用框架，
理由是它们是确定性的集合运算 —— 可单测、可复现、可解释，
不该让 LLM judge 介入。

qrels 只提供二元相关性（出现即相关，无等级），
因此 nDCG 的 IDCG 按 min(len(gold), k) 计算。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from rag.eval.metrics import (
    latency_percentiles,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
)


class TestRecallAtK:
    """Recall@k = 命中的 gold 数 / gold 总数。"""

    def test_all_gold_in_top_k(self):
        assert recall_at_k(["a", "b", "c"], {"a", "b"}, k=3) == pytest.approx(1.0)

    def test_half_gold_hit(self):
        assert recall_at_k(["a", "x", "y"], {"a", "b"}, k=3) == pytest.approx(0.5)

    def test_no_hit(self):
        assert recall_at_k(["x", "y"], {"a"}, k=2) == 0.0

    def test_k_truncates(self):
        """第 3 位的 gold 在 k=2 时不算命中。"""
        assert recall_at_k(["x", "y", "a"], {"a"}, k=2) == 0.0
        assert recall_at_k(["x", "y", "a"], {"a"}, k=3) == pytest.approx(1.0)

    def test_k_larger_than_results(self):
        """k 超过结果数不应报错，按实际结果算。"""
        assert recall_at_k(["a"], {"a"}, k=10) == pytest.approx(1.0)

    def test_empty_gold_returns_zero(self):
        """无答案查询的 Recall 无定义，返回 0.0 而非除零。

        这类查询应当用无答案识别率单独评估，不混进 Recall 平均值。
        """
        assert recall_at_k(["a", "b"], set(), k=5) == 0.0

    def test_empty_retrieved(self):
        assert recall_at_k([], {"a"}, k=5) == 0.0

    def test_duplicate_retrieved_counted_once(self):
        """同一 doc_id 重复出现只算一次命中，否则 Recall 可能超过 1。"""
        assert recall_at_k(["a", "a", "a"], {"a", "b"}, k=3) == pytest.approx(0.5)

    def test_never_exceeds_one(self):
        assert recall_at_k(["a", "b", "c"], {"a"}, k=3) <= 1.0

    def test_invalid_k_raises(self):
        with pytest.raises(ValueError):
            recall_at_k(["a"], {"a"}, k=0)


class TestMRRAtK:
    """MRR = 1 / 第一个命中的排名。只看最靠前的那个 gold。"""

    def test_first_position(self):
        assert mrr_at_k(["a", "x"], {"a"}, k=2) == pytest.approx(1.0)

    def test_second_position(self):
        assert mrr_at_k(["x", "a"], {"a"}, k=2) == pytest.approx(0.5)

    def test_third_position(self):
        assert mrr_at_k(["x", "y", "a"], {"a"}, k=3) == pytest.approx(1 / 3)

    def test_only_first_hit_matters(self):
        """第 1 位命中后，后面还有多少 gold 都不影响 MRR。"""
        assert mrr_at_k(["a", "b", "c"], {"a", "b", "c"}, k=3) == pytest.approx(1.0)

    def test_no_hit_within_k(self):
        assert mrr_at_k(["x", "y", "a"], {"a"}, k=2) == 0.0

    def test_empty_gold(self):
        assert mrr_at_k(["a"], set(), k=5) == 0.0

    def test_empty_retrieved(self):
        assert mrr_at_k([], {"a"}, k=5) == 0.0

    def test_duplicate_before_hit(self):
        """重复项占位会影响排名 —— 这与检索系统的真实行为一致。"""
        assert mrr_at_k(["x", "x", "a"], {"a"}, k=3) == pytest.approx(1 / 3)


class TestNDCGAtK:
    """nDCG@k，二元相关性。DCG = Σ 1/log2(rank+1)。"""

    def test_perfect_ranking(self):
        """所有 gold 排在最前面时 nDCG = 1。"""
        assert ndcg_at_k(["a", "b"], {"a", "b"}, k=2) == pytest.approx(1.0)

    def test_single_gold_first(self):
        assert ndcg_at_k(["a", "x", "y"], {"a"}, k=3) == pytest.approx(1.0)

    def test_single_gold_second_position(self):
        """DCG = 1/log2(3) ≈ 0.6309，IDCG = 1/log2(2) = 1。"""
        import math

        expected = (1 / math.log2(3)) / (1 / math.log2(2))
        assert ndcg_at_k(["x", "a"], {"a"}, k=2) == pytest.approx(expected)

    def test_reversed_is_worse_than_perfect(self):
        good = ndcg_at_k(["a", "b", "x"], {"a", "b"}, k=3)
        bad = ndcg_at_k(["x", "a", "b"], {"a", "b"}, k=3)
        assert good > bad

    def test_no_hit(self):
        assert ndcg_at_k(["x", "y"], {"a"}, k=2) == 0.0

    def test_empty_gold(self):
        assert ndcg_at_k(["a"], set(), k=5) == 0.0

    def test_idcg_capped_by_k(self):
        """gold 有 5 个但 k=2 时，IDCG 只按 2 个理想位置算，
        否则 nDCG 永远无法达到 1，指标失去可比性。"""
        assert ndcg_at_k(["a", "b"], {"a", "b", "c", "d", "e"}, k=2) == pytest.approx(1.0)

    def test_within_unit_interval(self):
        value = ndcg_at_k(["x", "a", "y", "b"], {"a", "b"}, k=4)
        assert 0.0 <= value <= 1.0

    def test_duplicate_counted_once(self):
        """重复的 gold 不应被重复计入 DCG。"""
        assert ndcg_at_k(["a", "a"], {"a"}, k=2) == pytest.approx(1.0)


class TestLatencyPercentiles:
    """延迟分位。报告需要 P50/P95，不能只报平均值 —— 平均值会被长尾掩盖。"""

    def test_basic(self):
        samples = [float(i) for i in range(1, 101)]  # 1..100
        result = latency_percentiles(samples)
        assert result["p50"] == pytest.approx(50.0, abs=1.0)
        assert result["p95"] == pytest.approx(95.0, abs=1.0)

    def test_single_sample(self):
        result = latency_percentiles([42.0])
        assert result["p50"] == pytest.approx(42.0)
        assert result["p95"] == pytest.approx(42.0)
        assert result["p99"] == pytest.approx(42.0)

    def test_empty_returns_zeros(self):
        """空样本不应抛异常 —— 某个 variant 全部失败时仍要能出报告。"""
        result = latency_percentiles([])
        assert result["p50"] == 0.0
        assert result["mean"] == 0.0

    def test_includes_mean_and_max(self):
        result = latency_percentiles([1.0, 2.0, 3.0])
        assert result["mean"] == pytest.approx(2.0)
        assert result["max"] == pytest.approx(3.0)

    def test_unsorted_input(self):
        """输入顺序不应影响结果。"""
        assert latency_percentiles([3.0, 1.0, 2.0]) == latency_percentiles([1.0, 2.0, 3.0])


class TestMetricRelationships:
    """指标间应当成立的关系，防止实现走偏。"""

    def test_recall_monotonic_in_k(self):
        """k 增大时 Recall 不应下降。"""
        retrieved = ["x", "a", "y", "b"]
        gold = {"a", "b"}
        values = [recall_at_k(retrieved, gold, k) for k in (1, 2, 3, 4)]
        assert values == sorted(values)

    def test_mrr_monotonic_in_k(self):
        retrieved = ["x", "y", "a"]
        gold = {"a"}
        values = [mrr_at_k(retrieved, gold, k) for k in (1, 2, 3)]
        assert values == sorted(values)

    def test_mrr_ge_ndcg_when_single_gold_first(self):
        """单个 gold 排第一时，MRR 与 nDCG 都应为 1。"""
        assert mrr_at_k(["a", "x"], {"a"}, k=2) == pytest.approx(1.0)
        assert ndcg_at_k(["a", "x"], {"a"}, k=2) == pytest.approx(1.0)
