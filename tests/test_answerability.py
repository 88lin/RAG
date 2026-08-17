"""可答性判断（`rag.llm.assess_context`）单元测试

这个模块此前**零覆盖**，而 `chat_service` 里还有一份手抄副本 ——
两份实现都没被测过。T0b 删掉副本时补上这份测试。

`TestEquivalenceWithDeletedCopy` 是本文件的重点：它把被删掉的那段副本
原样重建，逐个用例对比两者结论。这是"重构不改变行为"的直接证据 ——
既有 228 条测试全都不覆盖 `chat_service`，全绿并不能说明删对了。

该副本重建函数**不要删**：它是回归基线。日后若要改
`assess_context` 的判定逻辑（M3 会做），这些用例会明确告诉你
哪些输入的结论变了。
"""

from __future__ import annotations

import pytest

from config import ANSWERABLE_MIN_RELEVANCE, RETRIEVAL_MIN_RELEVANCE
from rag.llm import assess_context
from rag.scoring import compute_relevance, has_relevance_signal


def _doc(**fields) -> dict:
    """构造一条检索结果。document 与 metadata 是拼上下文必需的字段。

    默认带 `cosine_distance`：`has_relevance_signal()` 只认
    `rerank_logit` / `cosine_distance` 这两个**原始信号**字段，
    不认已算好的 `relevance`。只给 relevance 会被判成"无相关性信息"
    而走退化分支（信任检索层排序、不做阈值判断），阈值相关的用例
    就全都测不到真正的分支。

    构造纯 BM25 那种无信号的结果时用 `_doc_no_signal()`。
    """
    doc = {"document": "内容", "metadata": {"file": "a.md", "category": "test"}}
    if "relevance" in fields and "cosine_distance" not in fields:
        # relevance = 1 - d/2  =>  d = 2 * (1 - relevance)
        doc["cosine_distance"] = 2.0 * (1.0 - fields["relevance"])
    doc.update(fields)
    return doc


def _doc_no_signal(**fields) -> dict:
    """无相关性信号的结果（纯 BM25 检索的形态）。"""
    return {"document": "内容", "metadata": {"file": "a.md", "category": "test"}, **fields}


# ============================================================
# 基本判定
# ============================================================

class TestAssessContext:
    def test_empty_results_not_answerable(self):
        """无检索结果时不可答，且不返回上下文。"""
        answerable, context, top, used = assess_context([])
        assert answerable is False
        assert context == ""
        assert top is None
        assert used == 0

    def test_high_relevance_is_answerable(self):
        answerable, context, top, used = assess_context(
            [_doc(rerank_logit=5.0)]
        )
        assert answerable is True
        assert top > 0.99
        assert used == 1
        assert "内容" in context

    def test_low_relevance_not_answerable(self):
        """低于阈值时不可答，且**不返回上下文** ——
        证据不足就说不足，不放宽标准凑结果（CLAUDE.md 禁止无声降级）。
        """
        answerable, context, top, used = assess_context(
            [_doc(rerank_logit=-5.0)]
        )
        assert answerable is False
        assert context == ""
        assert top < 0.01
        assert used == 0

    def test_exactly_at_threshold_is_answerable(self):
        """恰好等于阈值算可答（>= 而非 >）。

        边界方向必须固定：改成 > 会让"刚好达标"的查询被拒，
        而阈值本身是校准出来的近似值，这种差异在评测里会体现为
        误拒率的系统性偏移。
        """
        answerable, _, _, _ = assess_context(
            [_doc(relevance=ANSWERABLE_MIN_RELEVANCE)]
        )
        assert answerable is True

    def test_just_below_threshold_not_answerable(self):
        answerable, _, _, _ = assess_context(
            [_doc(relevance=ANSWERABLE_MIN_RELEVANCE - 0.01)]
        )
        assert answerable is False

    def test_top_relevance_is_max_not_first(self):
        """top_relevance 取最大值，不是取第一条。

        检索层通常已排序，但融合与精排之后顺序可能变；
        依赖"第一条最相关"是隐含假设，取 max 才是显式的。
        """
        _, _, top, _ = assess_context([
            _doc(relevance=0.2),
            _doc(relevance=0.9),
        ])
        assert top == pytest.approx(0.9)


class TestContextFiltering:
    def test_low_relevance_chunks_excluded_from_context(self):
        """可答时，低于 RETRIEVAL_MIN_RELEVANCE 的单条仍被剔除。

        两个阈值各管一件事：answerable_min 决定"这次能不能答"，
        retrieval_min 决定"哪些片段值得放进 prompt"。
        """
        _, context, _, used = assess_context([
            _doc(relevance=0.95, document="高相关内容"),
            _doc(relevance=RETRIEVAL_MIN_RELEVANCE - 0.05, document="低相关内容"),
        ])
        assert used == 1
        assert "高相关内容" in context
        assert "低相关内容" not in context

    def test_context_numbering_starts_at_one(self):
        """上下文里的文档编号从 1 开始 —— 它要和答案里的 [doc_N] 角标对上。"""
        _, context, _, _ = assess_context([_doc(relevance=0.95)])
        assert "[文档 1]" in context

    def test_custom_thresholds_respected(self):
        """允许调用方覆盖阈值，用于评测时扫阈值曲线。"""
        results = [_doc(relevance=0.5)]
        assert assess_context(results, answerable_min=0.4)[0] is True
        assert assess_context(results, answerable_min=0.6)[0] is False


class TestNoRelevanceSignal:
    """纯 BM25 检索没有可解释的相关性信息，此时不做阈值判断。

    BM25 分数无界，没有自然的 [0,1] 映射，`compute_relevance` 一律返回
    0.0。照常比阈值会拒绝**所有**查询 —— 而实测纯 bm25 方案
    Recall@5=0.586，排序是对的，不该被拒。
    """

    def test_bm25_only_degrades_to_trusting_ranking(self):
        results = [_doc_no_signal(bm25_score=12.3), _doc_no_signal(bm25_score=8.1)]
        assert not any(has_relevance_signal(r) for r in results), "前提：确实无信号"

        answerable, context, _, used = assess_context(results)
        assert answerable is True
        assert used == 2, "无信号时不按 retrieval_min 过滤，全部进上下文"

    def test_zero_relevance_with_signal_is_still_judged(self):
        """有信号但分数为 0.0 时**要**按阈值判定，不能当成"无信号"。

        这是 `has_relevance_signal` 存在的理由：区分"分数是 0"与
        "没有分数"。用 `if not relevance:` 会把两者混为一谈 ——
        与 CLAUDE.md 禁止用 `or` 链回退数值是同一类问题。
        """
        results = [_doc(relevance=0.0, cosine_distance=2.0)]
        assert has_relevance_signal(results[0]), "前提：cosine_distance 提供了信号"
        answerable, _, _, _ = assess_context(results)
        assert answerable is False


# ============================================================
# 与被删副本的等价性（T0b 的回归基线）
# ============================================================

def _deleted_copy(results: list[dict]) -> bool:
    """`chat_service.py:304-329` 被删掉的那份副本，原样重建。

    保留它是为了回归对比，**不是**给生产代码用。
    结构与原文一一对应：relevance_of 内联、top_relevance 取 max、
    has_signal 短路、三分支判定。
    """
    top_relevance = None
    has_signal = any(has_relevance_signal(r) for r in results) if results else False

    if results:
        top_relevance = max(
            (
                r["relevance"] if isinstance(r.get("relevance"), (int, float))
                else compute_relevance(r)
            )
            for r in results
        )

    if not results:
        return False
    elif not has_signal:
        return True
    return top_relevance >= ANSWERABLE_MIN_RELEVANCE


EQUIVALENCE_CASES = {
    "空结果": [],
    "高相关 rerank": [_doc(rerank_logit=5.0)],
    "低相关 rerank": [_doc(rerank_logit=-5.0)],
    "rerank_logit=0.0": [_doc(rerank_logit=0.0)],
    "恰好卡阈值": [_doc(relevance=ANSWERABLE_MIN_RELEVANCE)],
    "略低于阈值": [_doc(relevance=ANSWERABLE_MIN_RELEVANCE - 0.01)],
    "略高于阈值": [_doc(relevance=ANSWERABLE_MIN_RELEVANCE + 0.01)],
    "纯 BM25 无信号": [_doc_no_signal(bm25_score=12.3)],
    "relevance=0.0 有信号": [_doc(relevance=0.0, cosine_distance=2.0)],
    "余弦距离": [_doc(cosine_distance=0.3)],
    "混合多条取 max": [_doc(relevance=0.2), _doc(relevance=0.95)],
    "全部低相关": [_doc(relevance=0.1), _doc(relevance=0.15)],
    "无任何分数字段": [_doc_no_signal()],
}


class TestEquivalenceWithDeletedCopy:
    """删副本前后，可答性结论必须逐个用例一致。

    既有 228 条测试全都不覆盖 `chat_service`，所以"测试全绿"不能证明
    删对了。这组用例是直接证据。
    """

    @pytest.mark.parametrize(
        "case_name", list(EQUIVALENCE_CASES), ids=list(EQUIVALENCE_CASES)
    )
    def test_same_verdict(self, case_name: str):
        results = EQUIVALENCE_CASES[case_name]
        expected = _deleted_copy(results)
        actual, _, _, _ = assess_context(results)
        assert actual == expected, (
            f"用例「{case_name}」结论不一致："
            f"原副本={expected}，assess_context={actual}"
        )

    def test_top_relevance_also_matches(self):
        """顺带验 top_relevance 的算法一致 —— 它会进 SSE 日志与面板。"""
        for name, results in EQUIVALENCE_CASES.items():
            if not results:
                continue
            expected = max(
                (
                    r["relevance"] if isinstance(r.get("relevance"), (int, float))
                    else compute_relevance(r)
                )
                for r in results
            )
            _, _, actual, _ = assess_context(results)
            assert actual == pytest.approx(expected), f"用例「{name}」top 不一致"
