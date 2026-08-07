"""
rag/eval/threshold.py 单元测试

阈值校准的规格说明。核心不变量：
  阈值升高 -> 无答案识别率单调不降、误拒率单调不降
两条曲线天然对立，选点是权衡而非最优化。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from rag.eval.threshold import find_knee, format_table, sweep


class TestSweep:
    def test_threshold_below_all_accepts_everything(self):
        """阈值低于所有样本时：无答案全部漏判，有答案全部保留。"""
        points = sweep([0.8, 0.9], [0.6, 0.7], [0.5])
        point = points[0]
        assert point.unanswerable_recall == 0.0
        assert point.false_reject_rate == 0.0

    def test_threshold_above_all_rejects_everything(self):
        """阈值高于所有样本时：无答案全部识别，有答案全部误拒。"""
        points = sweep([0.8, 0.9], [0.6, 0.7], [0.95])
        point = points[0]
        assert point.unanswerable_recall == 1.0
        assert point.false_reject_rate == 1.0

    def test_perfect_separation(self):
        """两类样本完全可分时，存在识别率 1.0 且误拒率 0.0 的阈值。"""
        points = sweep([0.8, 0.9], [0.2, 0.3], [0.5])
        point = points[0]
        assert point.unanswerable_recall == 1.0
        assert point.false_reject_rate == 0.0
        assert point.balanced_score == pytest.approx(1.0)

    def test_partial_separation(self):
        answerable = [0.4, 0.6, 0.8, 0.9]
        unanswerable = [0.3, 0.5, 0.7]
        points = sweep(answerable, unanswerable, [0.55])
        point = points[0]
        # 无答案中 0.3 与 0.5 低于 0.55 -> 2/3
        assert point.unanswerable_recall == pytest.approx(2 / 3)
        # 有答案中只有 0.4 低于 0.55 -> 1/4
        assert point.false_reject_rate == pytest.approx(0.25)

    def test_boundary_is_inclusive_for_answerable(self):
        """判定规则是 relevance >= 阈值 则可答，等于阈值不算误拒。"""
        points = sweep([0.5], [], [0.5])
        assert points[0].false_reject_rate == 0.0

    def test_boundary_unanswerable_not_recalled(self):
        """无答案样本恰好等于阈值时，按可答处理，即未被识别。"""
        points = sweep([], [0.5], [0.5])
        assert points[0].unanswerable_recall == 0.0

    def test_monotonic_in_threshold(self):
        """阈值升高时两条曲线都不应下降 —— 这是判定规则的直接推论，
        若不成立说明比较方向写反了。"""
        answerable = [0.3, 0.5, 0.7, 0.9]
        unanswerable = [0.2, 0.4, 0.6, 0.8]
        thresholds = [0.1, 0.3, 0.5, 0.7, 0.9]
        points = sweep(answerable, unanswerable, thresholds)

        recalls = [p.unanswerable_recall for p in points]
        rejects = [p.false_reject_rate for p in points]
        assert recalls == sorted(recalls)
        assert rejects == sorted(rejects)

    def test_output_sorted_by_threshold(self):
        points = sweep([0.5], [0.5], [0.9, 0.1, 0.5])
        assert [p.threshold for p in points] == [0.1, 0.5, 0.9]

    def test_empty_answerable(self):
        """某一类为空不应抛异常 —— 数据不足时仍要能出图。"""
        points = sweep([], [0.3, 0.7], [0.5])
        assert points[0].false_reject_rate == 0.0
        assert points[0].unanswerable_recall == pytest.approx(0.5)

    def test_empty_unanswerable(self):
        points = sweep([0.3, 0.7], [], [0.5])
        assert points[0].unanswerable_recall == 0.0

    def test_both_empty(self):
        points = sweep([], [], [0.5])
        assert points[0].balanced_score == 0.0

    def test_counts_recorded(self):
        points = sweep([0.5, 0.6], [0.3], [0.4])
        assert points[0].n_answerable == 2
        assert points[0].n_unanswerable == 1


class TestFindKnee:
    def test_picks_highest_recall_within_constraint(self):
        """在误拒率约束内选识别率最高的点。"""
        answerable = [0.6, 0.7, 0.8, 0.9, 1.0]      # 5 条
        unanswerable = [0.1, 0.2, 0.3, 0.4, 0.5]    # 5 条
        thresholds = [0.2, 0.4, 0.55, 0.65, 0.75]
        points = sweep(answerable, unanswerable, thresholds)

        chosen = find_knee(points, max_false_reject=0.10)
        # 0.55 时无答案全部识别(1.0)，有答案无误拒(0.0) -> 应选它或更高
        assert chosen.false_reject_rate <= 0.10
        assert chosen.unanswerable_recall == pytest.approx(1.0)

    def test_respects_false_reject_ceiling(self):
        """不能为了识别率牺牲误拒率超过上限。"""
        answerable = [0.5, 0.6, 0.7]
        unanswerable = [0.55, 0.65, 0.75]
        points = sweep(answerable, unanswerable, [0.5, 0.6, 0.7, 0.8])

        chosen = find_knee(points, max_false_reject=0.34)
        assert chosen.false_reject_rate <= 0.34

    def test_falls_back_when_no_point_feasible(self):
        """所有阈值都超出误拒约束时，返回误拒率最低者而非报错。

        这种情况意味着两类样本高度重叠，阈值法本身不适用 ——
        调用方应当看到这个信号，而不是拿到一个异常。
        """
        answerable = [0.1, 0.2]
        unanswerable = [0.8, 0.9]
        points = sweep(answerable, unanswerable, [0.5, 0.6])

        chosen = find_knee(points, max_false_reject=0.0)
        assert chosen.false_reject_rate == min(p.false_reject_rate for p in points)

    def test_empty_points_raises(self):
        with pytest.raises(ValueError):
            find_knee([])


class TestFormatTable:
    def test_produces_markdown(self):
        points = sweep([0.8], [0.3], [0.5])
        table = format_table(points)
        assert table.startswith("| 阈值")
        assert "0.50" in table
        # 表头 + 分隔行 + 1 条数据
        assert len(table.splitlines()) == 3
