"""
生成层评测：忠实度（faithfulness）

分两步是故意的：
  generate —— 跑完整 RAG 管线产出答案（慢，要调 LLM）
  score    —— 用 ragas 给答案打分（也调 LLM，但可独立重跑）
分开后调整判分逻辑不必重新生成答案。

用法：
  python scripts/run_generation_eval.py generate --limit 50
  python scripts/run_generation_eval.py score
  python scripts/run_generation_eval.py sample --n 20   # 抽人工核验样本

只评 faithfulness：T2Ranking 不提供标准答案，answer_correctness
需要 gold answer，硬凑参考答案会让指标失去意义。
"""

import argparse
import io
import random
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from rag.eval.generation import (
    GenerationSample,
    read_samples,
    read_results,
    score_faithfulness,
    summarize,
    write_results,
    write_samples,
)
from rag.eval.schema import read_queries
from rag.eval.variants import get as get_variant

ROOT = Path(__file__).parent.parent
QUERIES_PATH = ROOT / "data" / "eval" / "t2ranking_queries.jsonl"
SAMPLES_PATH = ROOT / "docs" / "eval" / "runs" / "generation_samples.jsonl"
RESULTS_PATH = ROOT / "docs" / "eval" / "runs" / "faithfulness.jsonl"

# 用哪个检索方案生成答案。取 rrf 而非 rrf_rerank：
# 后者 P50 延迟 9.8 秒，50 条要跑 8 分钟以上，而生成层评的是
# 「答案是否忠于给定证据」，与检索方案的选择关系不大。
GENERATION_VARIANT = "rrf"


def cmd_generate(args) -> int:
    """跑 RAG 管线产出答案，落 JSONL。"""
    from rag.eval.runner import EvalRetriever
    from rag.llm import LLMClient, assess_context

    if not QUERIES_PATH.exists():
        print(f"[!!] 评测 query 不存在: {QUERIES_PATH}")
        return 1

    queries = read_queries(QUERIES_PATH)[: args.limit]
    variant = get_variant(GENERATION_VARIANT)
    engine = EvalRetriever(variant)
    llm = LLMClient()

    print(f"生成答案：{len(queries)} 条，检索方案 {variant.describe()}")
    print(f"判分口径：faithfulness（答案断言是否被证据支持）\n")

    samples = []
    for index, item in enumerate(queries, start=1):
        started = time.perf_counter()
        try:
            ordered, relevances, _ = engine.search(item.query, top_k=config.TOP_K_RESULTS * 2)

            # 取实际进入 prompt 的证据 —— 不是全部召回结果。
            # 用全部会稀释忠实度：答案没引用的证据也被算作可支持来源。
            raw = engine.vectordb.get(ids=ordered[: config.TOP_K_RESULTS])
            documents = raw.get("documents") or []
            metadatas = raw.get("metadatas") or []
            results = [
                {
                    "id": ordered[i],
                    "document": documents[i],
                    "metadata": metadatas[i] if i < len(metadatas) else {},
                    "relevance": relevances[i] if i < len(relevances) else 0.0,
                }
                for i in range(len(documents))
            ]

            is_answerable, context, _, used = assess_context(results)
            if not is_answerable or not context:
                samples.append(
                    GenerationSample(
                        qid=item.qid,
                        question=item.query,
                        answer="",
                        contexts=[],
                        error="判定为知识库无答案，跳过生成",
                    )
                )
                continue

            answer = llm.answer_with_context(item.query, context)
            samples.append(
                GenerationSample(
                    qid=item.qid,
                    question=item.query,
                    answer=answer or "",
                    contexts=[r["document"] for r in results[:used]],
                    cited_files=[
                        r["metadata"].get("file", r["id"]) for r in results[:used]
                    ],
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不中断整轮
            samples.append(
                GenerationSample(
                    qid=item.qid,
                    question=item.query,
                    answer="",
                    contexts=[],
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

        if index % 10 == 0 or index == len(queries):
            failed = sum(1 for s in samples if s.error)
            print(f"  {index}/{len(queries)}" + (f"，跳过/失败 {failed}" if failed else ""),
                  flush=True)

    write_samples(SAMPLES_PATH, samples)
    ok = sum(1 for s in samples if not s.error)
    print(f"\n[写入] {SAMPLES_PATH.relative_to(ROOT)}: {len(samples)} 条（有效 {ok}）")
    return 0


def cmd_score(args) -> int:
    """用 ragas 给已生成的答案打忠实度分。"""
    if not SAMPLES_PATH.exists():
        print(f"[!!] 样本不存在: {SAMPLES_PATH}")
        print("     请先运行: python scripts/run_generation_eval.py generate")
        return 1

    samples = read_samples(SAMPLES_PATH)
    valid = [s for s in samples if not s.error]
    print(f"待评分：{len(valid)} 条（共 {len(samples)} 条，{len(samples)-len(valid)} 条被跳过）")
    print(f"判分模型：{config.ZHIPU_MODEL}（temperature=0）\n")

    results = score_faithfulness(samples)
    write_results(RESULTS_PATH, results)

    stats = summarize(results)
    print(f"\n[写入] {RESULTS_PATH.relative_to(ROOT)}")
    print("\n忠实度汇总:")
    print(f"  均值:            {stats['mean']:.3f}")
    if stats.get("scored"):
        print(f"  中位数:          {stats.get('median', 0):.3f}")
        print(f"  最低/最高:       {stats.get('min', 0):.3f} / {stats.get('max', 0):.3f}")
        print(f"  低于 0.8 的比例: {stats.get('below_0.8', 0):.1%}")
    print(f"  成功评分:        {stats['scored']}")
    print(f"  评分失败:        {stats['failed']}")

    if stats["failed"]:
        print("\n  注意：评分失败的条目未计入均值。原因见 JSONL 的 error 字段。")

    print("\n下一步：抽样人工核验并记录一致率")
    print("  python scripts/run_generation_eval.py sample --n 20")
    return 0


def cmd_sample(args) -> int:
    """抽样输出待人工核验的条目。

    LLM-as-judge 的结果必须人工抽检 —— 不做这一步的忠实度数字不可信。
    优先抽低分条目（那里最可能出现判分错误），并混入随机高分条目作对照。
    """
    if not RESULTS_PATH.exists():
        print(f"[!!] 评分结果不存在: {RESULTS_PATH}")
        return 1

    results = [r for r in read_results(RESULTS_PATH) if r.faithfulness is not None]
    if not results:
        print("[!!] 没有可用的评分结果")
        return 1

    rng = random.Random(args.seed)
    low = sorted(results, key=lambda r: r.faithfulness)[: args.n // 2]
    rest = [r for r in results if r not in low]
    rng.shuffle(rest)
    picked = low + rest[: args.n - len(low)]

    print("=" * 78)
    print(f"人工核验样本（{len(picked)} 条：{len(low)} 条低分 + {len(picked)-len(low)} 条随机）")
    print("=" * 78)
    print("\n核验方法：读答案与证据，判断答案里的断言是否都能由证据推出。")
    print("与 ragas 打分是否一致，记入 docs/eval/human_check.md。\n")

    samples = {s.qid: s for s in read_samples(SAMPLES_PATH)}

    for i, item in enumerate(picked, start=1):
        print("-" * 78)
        print(f"[{i}] qid={item.qid}  ragas 忠实度={item.faithfulness:.3f}")
        print(f"问题: {item.question}")
        print(f"答案: {item.answer[:220]}")
        sample = samples.get(item.qid)
        if sample:
            for j, ctx in enumerate(sample.contexts, start=1):
                print(f"证据{j}: {ctx[:160]}")
        print()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成层评测（忠实度）")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="跑 RAG 管线产出答案")
    gen.add_argument("--limit", type=int, default=50, help="评测条数")
    gen.set_defaults(func=cmd_generate)

    score = sub.add_parser("score", help="用 ragas 打忠实度分")
    score.set_defaults(func=cmd_score)

    samp = sub.add_parser("sample", help="抽样输出待人工核验条目")
    samp.add_argument("--n", type=int, default=20, help="抽样条数")
    samp.add_argument("--seed", type=int, default=42)
    samp.set_defaults(func=cmd_sample)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
