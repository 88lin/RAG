"""
下载并抽样评测数据集

产出统一格式的 JSONL 到 data/eval/：
    {dataset}_queries.jsonl    EvalQuery
    {dataset}_corpus.jsonl     EvalDoc

用法：
  # 只取 query 与标注，验证 loader（2.3 MB，快）
  python scripts/fetch_eval_data.py --dataset t2ranking --limit 300 --skip-collection

  # 完整抽样（需下载 3.5GB collection，支持断点续传）
  python scripts/fetch_eval_data.py --dataset t2ranking --limit 300

  # 查看已有产物
  python scripts/fetch_eval_data.py --status

原始 TSV 缓存在 data/eval/raw/，重复运行不会重新下载已完整的文件。
中断后重跑会从断点续传。
"""

import argparse
import io
import sys
from pathlib import Path

# Windows 控制台默认 GBK，输出中文会抛 UnicodeEncodeError
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

from rag.eval.datasets import t2ranking
from rag.eval.schema import read_queries, write_docs, write_queries

EVAL_DIR = Path(__file__).parent.parent / "data" / "eval"
RAW_DIR = EVAL_DIR / "raw"

DATASETS = {
    "t2ranking": t2ranking,
}


def show_status() -> None:
    """列出已下载的原始文件与已生成的评测数据。"""
    print("=" * 70)
    print("评测数据状态")
    print("=" * 70)

    print(f"\n原始文件缓存 ({RAW_DIR}):")
    if RAW_DIR.exists():
        files = sorted(RAW_DIR.glob("*"))
        if files:
            for path in files:
                print(f"  {path.name:<32} {path.stat().st_size / 1048576:>10.1f} MB")
        else:
            print("  (空)")
    else:
        print("  (目录不存在)")

    print(f"\n已生成的评测数据 ({EVAL_DIR}):")
    found = False
    for path in sorted(EVAL_DIR.glob("*.jsonl")):
        found = True
        lines = sum(1 for _ in path.open(encoding="utf-8"))
        print(f"  {path.name:<32} {lines:>10,} 条")
    if not found:
        print("  (空)")


def summarize(queries_path: Path) -> None:
    """打印抽样结果的统计，用于人工确认数据合理。"""
    queries = read_queries(queries_path)
    if not queries:
        print("  [!!] 没有读到任何 query")
        return

    gold_counts = [len(q.gold_doc_ids) for q in queries]
    answerable = sum(1 for q in queries if q.is_answerable)

    print("\n抽样统计:")
    print(f"  query 数:            {len(queries)}")
    print(f"  可答 query:          {answerable}")
    print(f"  gold 段落数（去重）: {len(set().union(*(q.gold_doc_ids for q in queries)))}")
    print(f"  每 query 平均 gold:  {sum(gold_counts) / len(gold_counts):.1f}")
    print(f"  每 query 最少/最多:  {min(gold_counts)} / {max(gold_counts)}")

    print("\n样例（前 3 条）:")
    for query in queries[:3]:
        print(f"  qid={query.qid}  gold={len(query.gold_doc_ids)}  {query.query[:40]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="下载并抽样评测数据集")
    parser.add_argument("--dataset", default="t2ranking", choices=sorted(DATASETS))
    parser.add_argument("--limit", type=int, default=300, help="抽取的 query 条数")
    parser.add_argument("--seed", type=int, default=42, help="随机种子，控制可复现性")
    parser.add_argument(
        "--skip-collection",
        action="store_true",
        help="跳过 3.5GB 语料下载，只产出 query 与标注",
    )
    parser.add_argument("--status", action="store_true", help="只查看状态，不下载")
    args = parser.parse_args()

    if args.status:
        show_status()
        return 0

    loader = DATASETS[args.dataset]
    queries, docs = loader.fetch(
        raw_dir=RAW_DIR,
        limit=args.limit,
        seed=args.seed,
        skip_collection=args.skip_collection,
    )

    queries_path = EVAL_DIR / f"{args.dataset}_queries.jsonl"
    written = write_queries(queries_path, queries)
    print(f"\n[写入] {queries_path.name}: {written} 条 query")

    if docs is not None:
        corpus_path = EVAL_DIR / f"{args.dataset}_corpus.jsonl"
        doc_count = write_docs(corpus_path, docs)
        print(f"[写入] {corpus_path.name}: {doc_count:,} 条段落")
    else:
        print("[跳过] 语料抽样（--skip-collection）")

    summarize(queries_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
