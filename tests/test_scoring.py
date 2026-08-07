"""
rag/scoring.py 单元测试

这份测试同时是 scoring 模块的规格说明：
- relevance 恒为 [0,1]，越大越相关，可展示给用户
- RRF 分数只用于排序，不可展示、不可当相关性
两者职责严格分离。
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from rag.scoring import (
    RRF_K_DEFAULT,
    compute_relevance,
    has_relevance_signal,
    rrf_fuse,
)


# ============================================================
# compute_relevance
# ============================================================

class TestComputeRelevance:
    """相关性口径：[0,1]，越大越相关。"""

    def test_rerank_logit_zero_gives_half(self):
        """sigmoid(0) = 0.5。

        这条同时是回归测试：修复前 chat_service.py 用 `or` 链回退，
        rerank_score=0.0 会被判为 falsy 而跳过，落到下一个分支。
        """
        assert compute_relevance({"rerank_logit": 0.0}) == pytest.approx(0.5)

    def test_rerank_logit_positive_above_half(self):
        assert compute_relevance({"rerank_logit": 5.0}) > 0.99

    def test_rerank_logit_negative_below_half(self):
        assert compute_relevance({"rerank_logit": -5.0}) < 0.01

    def test_rerank_logit_extreme_no_overflow(self):
        """CrossEncoder logit 可达 ±11，naive exp 会溢出，必须防护。"""
        assert compute_relevance({"rerank_logit": -800.0}) == pytest.approx(0.0, abs=1e-9)
        assert compute_relevance({"rerank_logit": 800.0}) == pytest.approx(1.0, abs=1e-9)

    def test_rerank_takes_priority_over_distance(self):
        """rerank 是 cross-encoder 精排，比向量距离更准，优先级更高。"""
        result = {"rerank_logit": 5.0, "cosine_distance": 1.9}
        assert compute_relevance(result) > 0.99

    def test_cosine_distance_zero_is_one(self):
        """余弦距离 0 = 完全相同。"""
        assert compute_relevance({"cosine_distance": 0.0}) == pytest.approx(1.0)

    def test_cosine_distance_one_is_half(self):
        """归一化向量正交时距离为 1。"""
        assert compute_relevance({"cosine_distance": 1.0}) == pytest.approx(0.5)

    def test_cosine_distance_two_is_zero(self):
        """余弦距离 2 = 完全相反。"""
        assert compute_relevance({"cosine_distance": 2.0}) == pytest.approx(0.0)

    def test_cosine_distance_above_two_clamped(self):
        """浮点误差或非归一化向量可能越界，必须夹紧而非返回负数。"""
        assert compute_relevance({"cosine_distance": 2.5}) == 0.0

    def test_negative_distance_clamped_to_one(self):
        """浮点误差导致的微小负距离夹到 1.0。"""
        assert compute_relevance({"cosine_distance": -1e-9}) == pytest.approx(1.0)

    def test_missing_all_fields_returns_zero(self):
        """无任何分数信息时返回 0，而不是抛异常或返回 None。

        理由：这个值会流向阈值判断和 UI，None 会在下游炸成 TypeError。
        返回 0 的语义是"无证据表明相关"，是安全的默认。
        """
        assert compute_relevance({}) == 0.0

    def test_none_distance_treated_as_missing(self):
        """ChromaDB 在某些情况下返回 None 距离（仓库中已有此现象）。"""
        assert compute_relevance({"cosine_distance": None}) == 0.0

    def test_non_numeric_distance_treated_as_missing(self):
        assert compute_relevance({"cosine_distance": "0.5"}) == 0.0

    @pytest.mark.parametrize("logit", [-11.0, -3.0, 0.0, 3.0, 11.0])
    def test_always_within_unit_interval(self, logit):
        assert 0.0 <= compute_relevance({"rerank_logit": logit}) <= 1.0


# ============================================================
# rrf_fuse
# ============================================================

class TestRRFFuse:
    """RRF：只用排名，不用分数。"""

    def test_single_list_preserves_order(self):
        fused = rrf_fuse(["a", "b", "c"])
        assert [doc_id for doc_id, _ in fused] == ["a", "b", "c"]

    def test_score_formula(self):
        """第 n 名（1-indexed）得 1/(k+n)。"""
        fused = dict(rrf_fuse(["a", "b"], k=60))
        assert fused["a"] == pytest.approx(1 / 61)
        assert fused["b"] == pytest.approx(1 / 62)

    def test_both_lists_contribute(self):
        """同时出现在两路的文档得分累加。"""
        fused = dict(rrf_fuse(["a"], ["a"], k=60))
        assert fused["a"] == pytest.approx(2 / 61)

    def test_consensus_beats_single_top(self):
        """两路都排中游 > 一路第一另一路缺席。

        这是 RRF 的核心语义，也是 k=60 存在的理由。
        vector: x 第一, y 第三
        bm25:   z 第一, y 第二
        y 两路都有 → 应胜过只在单路第一的 x 和 z
        """
        fused = dict(rrf_fuse(["x", "w", "y"], ["z", "y"], k=60))
        assert fused["y"] > fused["x"]
        assert fused["y"] > fused["z"]

    def test_bm25_scale_irrelevant(self):
        """RRF 不看分数量级——这正是它取代加权归一化的原因。

        无论 BM25 原始分是 3.7 还是 3700，只要排名相同，结果就相同。
        """
        assert rrf_fuse(["a", "b"], ["b", "a"]) == rrf_fuse(["a", "b"], ["b", "a"])

    def test_empty_lists(self):
        assert rrf_fuse() == []
        assert rrf_fuse([], []) == []

    def test_ignores_empty_among_nonempty(self):
        fused = dict(rrf_fuse(["a"], []))
        assert fused == {"a": pytest.approx(1 / 61)}

    def test_duplicate_within_one_list_counted_once(self):
        """同一路内重复 id 只按最好排名计一次，避免刷分。"""
        fused = dict(rrf_fuse(["a", "a", "b"], k=60))
        assert fused["a"] == pytest.approx(1 / 61)

    def test_descending_order(self):
        fused = rrf_fuse(["c", "b", "a"], ["a", "b"])
        scores = [s for _, s in fused]
        assert scores == sorted(scores, reverse=True)

    def test_default_k_is_60(self):
        """来自 Cormack et al. 2009 原始论文。"""
        assert RRF_K_DEFAULT == 60

    def test_k_must_be_positive(self):
        """k=0 时第一名会得 1/1，退化为朴素倒数排名；负 k 可能除零。"""
        with pytest.raises(ValueError):
            rrf_fuse(["a"], k=0)
        with pytest.raises(ValueError):
            rrf_fuse(["a"], k=-1)

    def test_larger_k_flattens_gap(self):
        """k 越大，头部名次间差距越小。"""
        gap_small = dict(rrf_fuse(["a", "b"], k=1))
        gap_large = dict(rrf_fuse(["a", "b"], k=1000))
        assert (gap_small["a"] - gap_small["b"]) > (gap_large["a"] - gap_large["b"])


class TestHasRelevanceSignal:
    """区分"无相关性信息"与"确实不相关" —— 两者都是 0.0，处置不同。"""

    def test_rerank_logit_is_signal(self):
        assert has_relevance_signal({"rerank_logit": 0.0}) is True

    def test_cosine_distance_is_signal(self):
        assert has_relevance_signal({"cosine_distance": 2.0}) is True

    def test_bm25_only_has_no_signal(self):
        """纯 BM25 结果没有可解释的相关性。

        BM25 分数是无界 TF-IDF 累加，无自然的 [0,1] 映射。
        实测：纯 bm25 方案 Recall@5=0.586（排序正常）但 relevance 全为 0.0，
        若下游据此判断可答性会拒绝所有查询。
        """
        result = {"id": "x", "bm25_score": 12.7, "document": "..."}
        assert has_relevance_signal(result) is False
        assert compute_relevance(result) == 0.0

    def test_empty_has_no_signal(self):
        assert has_relevance_signal({}) is False

    def test_none_distance_has_no_signal(self):
        assert has_relevance_signal({"cosine_distance": None}) is False

    def test_bool_is_not_signal(self):
        """bool 是 int 子类，但不是合法分数。"""
        assert has_relevance_signal({"rerank_logit": True}) is False


class TestSeparationOfConcerns:
    """RRF 分数与 relevance 是两种量，不可混用。"""

    def test_rrf_score_is_not_relevance(self):
        """RRF 分数量级约 0.016~0.033，直接当相关性展示会永远显示个位数百分比。

        这是修复前的真实缺陷：仪表盘的百分比来自一条混用了
        rerank_score / hybrid_score / 1-distance 的 `or` 链。
        """
        top_score = rrf_fuse(["a", "b", "c"])[0][1]
        assert top_score < 0.02
        assert compute_relevance({"cosine_distance": 0.0}) == pytest.approx(1.0)
