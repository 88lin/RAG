"""
跑检索实验并输出对比报告

分两步是故意的：
  run   —— 跑检索，逐条落 JSONL（分钟级）
  score —— 从 JSONL 重算指标（秒级）
这样调整指标定义或增加 k 值时不必重跑检索。

用法：
  python scripts/run_eval.py run                      # 跑全部 variant
  python scripts/run_eval.py run --variant rrf        # 只跑一个
  python scripts/run_eval.py run --limit 50           # 快速验证管线
  python scripts/run_eval.py score                    # 从已有 JSONL 出报告
  python scripts/run_eval.py score --out docs/eval/report.md
"""

import argparse
import io
import sys
from pathlib import Path
from typing import Dict, List

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

from rag.eval import variants as variants_mod
from rag.eval.metrics import (
    aggregate,
    latency_percentiles,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
)
from rag.eval.runner import read_results, run_variant
from rag.eval.schema import read_queries

ROOT = Path(__file__).parent.parent
QUERIES_PATH = ROOT / "data" / "eval" / "t2ranking_queries.jsonl"
RUNS_DIR = ROOT / "docs" / "eval" / "runs"
REPORT_PATH = ROOT / "docs" / "eval" / "report.md"

# 报告中呈现的指标。Recall@1 反映"第一条就对"的比例，
# Recall@5 是主指标，Recall@10 用于观察召回天花板。
METRIC_KEYS = [
    "recall@1", "recall@5", "recall@10",
    "mrr@10", "ndcg@10",
]


def score_one(result) -> Dict[str, float]:
    """算单条 query 的全部指标。"""
    gold = set(result.gold_doc_ids)
    retrieved = result.retrieved_ids
    return {
        "recall@1": recall_at_k(retrieved, gold, 1),
        "recall@5": recall_at_k(retrieved, gold, 5),
        "recall@10": recall_at_k(retrieved, gold, 10),
        "mrr@10": mrr_at_k(retrieved, gold, 10),
        "ndcg@10": ndcg_at_k(retrieved, gold, 10),
    }


def score_variant(path: Path) -> Dict:
    """从 JSONL 重算一个 variant 的汇总指标。"""
    results = list(read_results(path))
    if not results:
        return {}

    # 失败的 query 不计入指标均值，但要报出条数 ——
    # 静默跳过会让"跑挂一半"看起来和"效果好"一样。
    ok = [item for item in results if not item.error]
    failed = len(results) - len(ok)

    per_query = [score_one(item) for item in ok]
    summary = aggregate(per_query, METRIC_KEYS)
    summary["latency"] = latency_percentiles([item.latency_ms for item in ok])
    summary["count"] = len(ok)
    summary["failed"] = failed

    # 分阶段耗时均值，用于解释延迟差异来自哪一步
    stages: Dict[str, List[float]] = {}
    for item in ok:
        for stage, value in item.stage_ms.items():
            stages.setdefault(stage, []).append(value)
    summary["stage_mean_ms"] = {
        stage: sum(values) / len(values) for stage, values in stages.items()
    }
    return summary


def cmd_run(args) -> int:
    if not QUERIES_PATH.exists():
        print(f"[!!] 评测 query 不存在: {QUERIES_PATH}")
        print("     请先运行: python scripts/fetch_eval_data.py")
        return 1

    queries = read_queries(QUERIES_PATH)
    if args.limit:
        queries = queries[: args.limit]

    ids = [args.variant] if args.variant else variants_mod.DEFAULT_ORDER
    print(f"评测 query: {len(queries)} 条\nvariant: {ids}\n")

    for variant_id in ids:
        variant = variants_mod.get(variant_id)
        out_path = RUNS_DIR / f"{variant_id}.jsonl"
        try:
            run_variant(variant, queries, out_path, top_k=args.top_k)
            print(f"  -> {out_path.relative_to(ROOT)}\n")
        except Exception as exc:  # noqa: BLE001
            # 某个 variant 整体失败（例如 collection 未建）不应中断其余 variant
            print(f"  [!!] {variant_id} 失败: {type(exc).__name__}: {exc}\n")

    return 0


def cmd_score(args) -> int:
    rows: Dict[str, Dict] = {}
    for variant_id in variants_mod.DEFAULT_ORDER:
        path = RUNS_DIR / f"{variant_id}.jsonl"
        if not path.exists():
            continue
        summary = score_variant(path)
        if summary:
            rows[variant_id] = summary

    if not rows:
        print(f"[!!] {RUNS_DIR} 下没有可用的运行记录")
        print("     请先运行: python scripts/run_eval.py run")
        return 1

    header = (
        f"| {'variant':<14} | {'R@1':>6} | {'R@5':>6} | {'R@10':>6} "
        f"| {'MRR@10':>7} | {'nDCG@10':>7} | {'P50 ms':>8} | {'P95 ms':>8} | {'n':>4} |"
    )
    separator = "|" + "|".join(["-" * len(part) for part in header.split("|")[1:-1]]) + "|"

    print("\n" + "=" * len(header))
    print("检索方案对比")
    print("=" * len(header))
    print(header)
    print(separator)

    lines = [header, separator]
    for variant_id, summary in rows.items():
        row = (
            f"| {variant_id:<14} "
            f"| {summary['recall@1']:>6.3f} "
            f"| {summary['recall@5']:>6.3f} "
            f"| {summary['recall@10']:>6.3f} "
            f"| {summary['mrr@10']:>7.3f} "
            f"| {summary['ndcg@10']:>7.3f} "
            f"| {summary['latency']['p50']:>8.1f} "
            f"| {summary['latency']['p95']:>8.1f} "
            f"| {summary['count']:>4} |"
        )
        print(row)
        lines.append(row)

    print("\n分阶段平均耗时 (ms):")
    for variant_id, summary in rows.items():
        stages = summary.get("stage_mean_ms", {})
        detail = "  ".join(f"{k}={v:.1f}" for k, v in sorted(stages.items()))
        print(f"  {variant_id:<14} {detail}")

    failures = {k: v["failed"] for k, v in rows.items() if v.get("failed")}
    if failures:
        print(f"\n[!!] 存在失败的 query: {failures}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n[写入] {out_path}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="检索实验")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="跑检索并落 JSONL")
    run_parser.add_argument("--variant", choices=sorted(variants_mod.VARIANTS))
    run_parser.add_argument("--limit", type=int, help="只跑前 N 条 query")
    run_parser.add_argument("--top-k", type=int, default=20, help="每条召回多少候选")
    run_parser.set_defaults(func=cmd_run)

    score_parser = sub.add_parser("score", help="从 JSONL 重算指标")
    score_parser.add_argument("--out", help="把对比表写入文件")
    score_parser.set_defaults(func=cmd_score)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
