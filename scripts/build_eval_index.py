"""
把评测语料灌进各 variant 对应的 collection

评测语料与生产知识库物理隔离（collection 名前缀 eval_）：
混在一起会让生产检索命中 T2Ranking 的段落。

每个 embedding 模型一个 collection（维度不同不能混）。
本脚本按 variant 用到的模型去重后建库，因此 5 个 variant
只需建 2 个 collection（MiniLM 与 bge）。

用法：
  python scripts/build_eval_index.py                        # 建全部
  python scripts/build_eval_index.py --model BAAI/bge-small-zh-v1.5
  python scripts/build_eval_index.py --reset                # 先清空
  python scripts/build_eval_index.py --list
"""

import argparse
import io
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from rag.embedder import Embedder
from rag.eval.schema import read_docs
from rag.eval.variants import DEFAULT_ORDER, VARIANTS, eval_collection_name
from rag.vectordb import VectorDB

CORPUS_PATH = Path(__file__).parent.parent / "data" / "eval" / "t2ranking_corpus.jsonl"

# 每批灌库的段落数。太大占内存，太小则 embedding 无法充分批处理。
BATCH_SIZE = 256


def list_collections() -> None:
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(
        path=config.CHROMA_DB_PATH,
        settings=Settings(anonymized_telemetry=False),
    )
    print(f"{'COLLECTION':<52} {'条目数':>8}")
    print("-" * 62)
    for collection in client.list_collections():
        marker = "  <-- 评测" if collection.name.startswith("eval_") else ""
        print(f"{collection.name:<52} {collection.count():>8}{marker}")


def build(model_name: str, reset: bool) -> int:
    """为一个 embedding 模型建评测索引，返回写入条数。"""
    collection_name = eval_collection_name(model_name)

    print("=" * 70)
    print(f"模型:       {model_name}")
    print(f"Collection: {collection_name}")
    print("=" * 70)

    if not CORPUS_PATH.exists():
        print(f"[!!] 语料不存在: {CORPUS_PATH}")
        print("     请先运行: python scripts/fetch_eval_data.py")
        return 0

    started = time.perf_counter()
    embedder = Embedder(model_name=model_name)
    vectordb = VectorDB(collection_name=collection_name)

    if reset:
        vectordb.create_collection(reset=True)
    else:
        vectordb.get_collection()

    before = vectordb.count()
    print(f"  向量维度:   {embedder.get_embedding_dim()}")
    print(f"  建库前条数: {before:,}")

    total = 0
    batch_ids, batch_texts, batch_metas = [], [], []

    def flush() -> None:
        nonlocal total
        if not batch_ids:
            return
        # 文档侧编码：不加查询前缀（bge 的前缀只用于查询侧）
        embeddings = embedder.encode_documents(batch_texts, to_list=True)
        vectordb.add(
            ids=batch_ids,
            embeddings=embeddings,
            documents=batch_texts,
            metadatas=batch_metas,
        )
        total += len(batch_ids)
        print(f"  已灌入 {total:,} 条", flush=True)
        batch_ids.clear()
        batch_texts.clear()
        batch_metas.clear()

    for doc in read_docs(CORPUS_PATH):
        batch_ids.append(doc.doc_id)
        batch_texts.append(doc.text)
        # role 标记 gold/distractor，便于事后分析检索命中的是哪类
        batch_metas.append({"role": doc.metadata.get("role", "unknown")})
        if len(batch_ids) >= BATCH_SIZE:
            flush()
    flush()

    elapsed = time.perf_counter() - started
    print(f"\n完成：{before:,} -> {vectordb.count():,} 条，耗时 {elapsed:.1f}s")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="构建评测语料索引")
    parser.add_argument("--model", help="只建指定模型（默认建全部 variant 用到的）")
    parser.add_argument("--reset", action="store_true", help="建库前清空")
    parser.add_argument("--list", action="store_true", help="只列出 collection")
    args = parser.parse_args()

    if args.list:
        list_collections()
        return 0

    if args.model:
        models = [args.model]
    else:
        # 去重：5 个 variant 只用到 2 个模型
        models = list(dict.fromkeys(
            VARIANTS[vid].embedding_model for vid in DEFAULT_ORDER
        ))

    print(f"待建索引的模型: {models}\n")
    written = 0
    for model in models:
        written += build(model, args.reset)
        print()

    return 0 if written > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
