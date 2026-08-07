"""
并排对比两个 embedding 模型的检索结果

用途：
  换 embedding 模型前后，用同一批查询验证收益是否真实存在。
  这是 M1 正式评测之前的 sanity check —— 如果这里看不出差异，
  多半是查询前缀没生效或 collection 配错了，而不是"模型没用"。

前置：两个模型的 collection 都已建好
  python scripts/reindex.py --model all-MiniLM-L6-v2   --reset
  python scripts/reindex.py --model BAAI/bge-small-zh-v1.5 --reset

用法：
  python scripts/compare_embeddings.py
  python scripts/compare_embeddings.py --top-k 3
  python scripts/compare_embeddings.py --baseline all-MiniLM-L6-v2 --candidate BAAI/bge-small-zh-v1.5
"""

import argparse
import io
import sys
from pathlib import Path

# Windows 控制台默认 GBK，中文输出会抛 UnicodeEncodeError
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from rag.embedder import Embedder
from rag.scoring import compute_relevance
from rag.vectordb import VectorDB

# 覆盖多种查询类型的探针集。不是评测集（那是 M1 的事），
# 只用来肉眼确认换模型是否有效。
PROBE_QUERIES = [
    ("事实", "船员出差住宿标准是多少"),
    ("事实", "宠物可以带到公司吗"),
    ("关键词", "EV Ban 通告编号"),
    ("关键词", "Rider Clauses 附加条款"),
    ("语义改写", "在家上班一周能几天"),
    ("语义改写", "生病看医生能报销吗"),
    ("口语短查询", "头晕怎么办"),
    ("跨文档", "船舶总布置图包含哪些舱室"),
    ("无答案", "公司年会在哪个城市举办"),
    ("无答案", "CEO 的私人电话号码"),
]


def search(model_name: str, queries, top_k: int):
    """用指定模型检索，返回 {query: [(file, relevance), ...]}。"""
    collection_name = config.collection_name_for(model_name)
    db = VectorDB(collection_name=collection_name)
    db.get_collection()

    count = db.count()
    if count == 0:
        print(f"[!!] collection 为空: {collection_name}")
        print(f"     请先运行: python scripts/reindex.py --model {model_name} --reset")
        return None, collection_name, 0, 0

    embedder = Embedder(model_name=model_name)
    dim = embedder.get_embedding_dim()

    out = {}
    for _, query in queries:
        vector = embedder.encode_query(query, to_list=True)
        raw = db.query(query_embeddings=[vector], n_results=top_k)

        hits = []
        ids = raw["ids"][0]
        distances = (raw.get("distances") or [[]])[0]
        metadatas = raw["metadatas"][0]

        for i in range(len(ids)):
            distance = distances[i] if i < len(distances) else None
            relevance = compute_relevance({"cosine_distance": distance})
            hits.append((metadatas[i].get("file", "unknown"), relevance))
        out[query] = hits

    return out, collection_name, count, dim


def main() -> int:
    parser = argparse.ArgumentParser(description="并排对比两个 embedding 模型的检索结果")
    parser.add_argument("--baseline", default="all-MiniLM-L6-v2", help="基线模型")
    parser.add_argument("--candidate", default="BAAI/bge-small-zh-v1.5", help="候选模型")
    parser.add_argument("--top-k", type=int, default=5, help="每个查询展示几条")
    args = parser.parse_args()

    print("=" * 100)
    print("Embedding 模型检索对比")
    print("=" * 100)

    base, base_col, base_n, base_dim = search(args.baseline, PROBE_QUERIES, args.top_k)
    if base is None:
        return 1
    cand, cand_col, cand_n, cand_dim = search(args.candidate, PROBE_QUERIES, args.top_k)
    if cand is None:
        return 1

    print(f"\n基线:   {args.baseline}")
    print(f"        {base_col}  ({base_n} chunks, {base_dim} 维)")
    print(f"候选:   {args.candidate}")
    print(f"        {cand_col}  ({cand_n} chunks, {cand_dim} 维)")

    if base_n != cand_n:
        print(f"\n[warn] 两个 collection 的 chunk 数不同（{base_n} vs {cand_n}），")
        print("       对比可能不公平。建议用同一份文档分别重建。")

    col_width = 46
    top1_same = 0

    for kind, query in PROBE_QUERIES:
        print()
        print("-" * 100)
        print(f"[{kind}] {query}")
        print("-" * 100)
        print(f"  {'基线 ' + args.baseline:<{col_width}} | {'候选 ' + args.candidate}")

        base_hits = base[query]
        cand_hits = cand[query]

        for i in range(max(len(base_hits), len(cand_hits))):
            left = ""
            if i < len(base_hits):
                f, r = base_hits[i]
                left = f"{i + 1}. {f[:30]:<30} {r:.3f}"

            right = ""
            if i < len(cand_hits):
                f, r = cand_hits[i]
                right = f"{i + 1}. {f[:30]:<30} {r:.3f}"

            print(f"  {left:<{col_width}} | {right}")

        # top1 相关性对比
        if base_hits and cand_hits:
            b_rel, c_rel = base_hits[0][1], cand_hits[0][1]
            delta = c_rel - b_rel
            same_file = base_hits[0][0] == cand_hits[0][0]
            if same_file:
                top1_same += 1
            arrow = "上升" if delta > 0.01 else ("下降" if delta < -0.01 else "持平")
            print(
                f"  top1 relevance: {b_rel:.3f} -> {c_rel:.3f} "
                f"（{arrow} {delta:+.3f}）| top1 文件{'相同' if same_file else '不同'}"
            )

    print()
    print("=" * 100)
    print("汇总")
    print("=" * 100)
    print(f"  探针查询数:              {len(PROBE_QUERIES)}")
    print(f"  两者 top1 命中同一文件:  {top1_same}")
    print(f"  top1 文件不同（需人工判断哪个对）: {len(PROBE_QUERIES) - top1_same}")
    print()

    # 无答案类查询的 top1 相关性应当偏低。这一项比命中率更能暴露问题：
    # 分数虚高意味着系统会自信地拿无关文档编答案。
    print("  无答案类查询的 top1 相关性（越低越好）:")
    for kind, query in PROBE_QUERIES:
        if kind != "无答案":
            continue
        b = base[query][0][1] if base[query] else 0.0
        c = cand[query][0][1] if cand[query] else 0.0
        print(f"    {query[:24]:<26} 基线 {b:.3f}  ->  候选 {c:.3f}")
    print()
    print("  判读方法：")
    print("    1. relevance 绝对值在不同模型间不可比（分布不同），不要看谁分高。")
    print("    2. 看 top1 命中的文件对不对 —— 这是唯一可靠的人工信号。")
    print("    3. 看无答案类是否被压低。若仍高于 ANSWERABLE_MIN_RELEVANCE，")
    print("       说明该阈值初值偏高，需用评测集校准。")
    print("    4. 定量结论以 M1 评测集为准（Recall@5 / MRR@10 / nDCG@10）。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
