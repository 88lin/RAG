"""
无答案阈值校准

要解决的问题（M0 遗留）：`ANSWERABLE_MIN_RELEVANCE` 的初值 0.50 是拍定的。
M0 在 59 chunk 小语料上已证明它偏低 —— 知识库确实没有答案的查询
（"公司年会在哪个城市举办"）得到 relevance 0.692，被判定为可答，
系统会自信地拿无关文档编答案。

## 无答案子集怎么构造

有两种做法，选择哪一种决定结论是否有意义：

  (a) 手写一批"知识库肯定没有"的问题
      —— 写的时候就知道答案不在库里，会不自觉写得过于离题，
         识别率虚高，线上不成立。

  (b) 取真实 query，把它的全部 gold 段落从语料中移除
      —— query 是真实的、领域相关的、措辞自然的，只是证据被拿走。
         这才是线上的困难情形：用户问了个合理问题，
         但知识库恰好没覆盖这个知识点。

本模块用 (b)。

## 两条曲线的取舍

  无答案识别率 = 正确判定为不可答的比例（阈值越高越好）
  误拒率      = 有答案却被判不可答的比例（阈值越高越差）

两者天然对立，没有"最优阈值"，只有权衡点。对知识库问答场景，
误拒的代价（用户明明能查到却被告知没有）通常高于误答的代价，
因此倾向于选误拒率抬升前的最后一个点，而非识别率最高点。
选点理由必须写进报告 —— 这是"阈值是量出来的"与"阈值是拍的"的区别。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple


@dataclass
class ThresholdPoint:
    """单个阈值下的表现。"""

    threshold: float
    # 无答案样本中被正确判为不可答的比例
    unanswerable_recall: float
    # 有答案样本中被错误判为不可答的比例
    false_reject_rate: float
    # 两者的调和均值变体，用于快速定位候选区间（不作为唯一依据）
    balanced_score: float
    n_answerable: int
    n_unanswerable: int

    def as_row(self) -> str:
        return (
            f"| {self.threshold:.2f} "
            f"| {self.unanswerable_recall:>8.3f} "
            f"| {self.false_reject_rate:>8.3f} "
            f"| {self.balanced_score:>8.3f} |"
        )


def sweep(
    answerable_top_relevance: Sequence[float],
    unanswerable_top_relevance: Sequence[float],
    thresholds: Sequence[float],
) -> List[ThresholdPoint]:
    """扫描阈值，返回每个阈值下的两条曲线取值。

    参数:
        answerable_top_relevance: 有答案查询的 top1 relevance 列表
        unanswerable_top_relevance: 无答案查询的 top1 relevance 列表
        thresholds: 待扫描的阈值

    返回:
        按阈值升序的 ThresholdPoint 列表。

    判定规则与生产一致：top1 relevance >= 阈值 则判为可答。
    """
    n_ans = len(answerable_top_relevance)
    n_unans = len(unanswerable_top_relevance)

    points: List[ThresholdPoint] = []
    for threshold in sorted(thresholds):
        # 无答案样本：top1 低于阈值 = 正确识别
        correct_rejects = sum(
            1 for value in unanswerable_top_relevance if value < threshold
        )
        unanswerable_recall = correct_rejects / n_unans if n_unans else 0.0

        # 有答案样本：top1 低于阈值 = 误拒
        false_rejects = sum(
            1 for value in answerable_top_relevance if value < threshold
        )
        false_reject_rate = false_rejects / n_ans if n_ans else 0.0

        # 平衡分：识别率与"不误拒"的调和均值。
        # 只用于缩小候选区间，最终选点仍要看两条曲线的形状 ——
        # 单一标量会掩盖"误拒率刚开始陡升"这类关键信息。
        keep_rate = 1.0 - false_reject_rate
        if unanswerable_recall + keep_rate > 0:
            balanced = (
                2 * unanswerable_recall * keep_rate
                / (unanswerable_recall + keep_rate)
            )
        else:
            balanced = 0.0

        points.append(
            ThresholdPoint(
                threshold=threshold,
                unanswerable_recall=unanswerable_recall,
                false_reject_rate=false_reject_rate,
                balanced_score=balanced,
                n_answerable=n_ans,
                n_unanswerable=n_unans,
            )
        )

    return points


def find_knee(points: Sequence[ThresholdPoint], max_false_reject: float = 0.10) -> ThresholdPoint:
    """在误拒率约束下选取识别率最高的阈值。

    为什么用约束而非最大化平衡分：知识库问答场景中，
    误拒（用户明明能查到却被告知没有）的体验代价高于误答。
    因此先设误拒率上限，再在满足约束的点中取识别率最高者。

    参数:
        points: sweep() 的输出
        max_false_reject: 可接受的误拒率上限

    返回:
        选中的 ThresholdPoint。若无点满足约束，返回误拒率最低者。
    """
    if not points:
        raise ValueError("points 不能为空")

    feasible = [p for p in points if p.false_reject_rate <= max_false_reject]
    if not feasible:
        # 所有阈值都超出误拒约束时，退回最保守的那个并让调用方看到
        return min(points, key=lambda p: p.false_reject_rate)

    return max(feasible, key=lambda p: p.unanswerable_recall)


def format_table(points: Sequence[ThresholdPoint]) -> str:
    """生成 Markdown 表格。"""
    lines = [
        "| 阈值 | 无答案识别率 | 误拒率 | 平衡分 |",
        "|------|----------|--------|--------|",
    ]
    lines.extend(point.as_row() for point in points)
    return "\n".join(lines)


def format_ascii_curves(points: Sequence[ThresholdPoint], width: int = 40) -> str:
    """用 ASCII 画两条曲线。

    不引入 matplotlib：报告要能在终端和 GitHub 上直接读，
    且这一步的目的是看形状（拐点在哪），不需要精细图形。
    """
    lines = [
        f"{'阈值':<6} {'识别率':<{width + 8}} {'误拒率'}",
    ]
    for point in points:
        bar_recall = "#" * round(point.unanswerable_recall * width)
        bar_reject = "#" * round(point.false_reject_rate * width)
        lines.append(
            f"{point.threshold:<6.2f} "
            f"{bar_recall:<{width}} {point.unanswerable_recall:>5.2f}  "
            f"{bar_reject:<{width}} {point.false_reject_rate:>5.2f}"
        )
    return "\n".join(lines)
