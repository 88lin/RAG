"""
重建向量索引

用途：
  1. 换 embedding 模型后重灌数据（不同模型写入各自的 collection，互不影响）
  2. 修改分块策略或 metadata 结构后重建
  3. chroma_db 损坏后从 data/documents/ 恢复

用法：
  python scripts/reindex.py                                  # 用 config 中的默认模型
  python scripts/reindex.py --model all-MiniLM-L6-v2         # 指定模型（建到独立 collection）
  python scripts/reindex.py --reset                          # 先清空目标 collection
  python scripts/reindex.py --list                           # 只列出已有 collection

collection 名由模型名自动派生，不需要手动指定，避免不同模型的向量混入同一集合。
"""

import argparse
import io
import sys
import time
from pathlib import Path

# Windows 控制台默认 GBK，输出中文或符号会抛 UnicodeEncodeError。
# 与 backend/main.py 一致，在入口处统一切到 UTF-8。
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from rag.embedder import Embedder
from rag.ingestion import DocumentIngestion
from rag.vectordb import VectorDB


def list_collections() -> None:
    """列出库中所有 collection 及其条目数。"""
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(
        path=config.CHROMA_DB_PATH,
        settings=Settings(anonymized_telemetry=False),
    )
    collections = client.list_collections()

    if not collections:
        print("（无 collection）")
        return

    print(f"{'COLLECTION':<48} {'条目数':>8}")
    print("-" * 58)
    for col in collections:
        marker = "  <-- 当前默认" if col.name == config.COLLECTION_NAME else ""
        print(f"{col.name:<48} {col.count():>8}{marker}")


def reindex(model_name: str, reset: bool) -> int:
    """重建指定模型的索引，返回写入的 chunk 数。"""
    collection_name = config.collection_name_for(model_name)

    print("=" * 70)
    print("重建向量索引")
    print("=" * 70)
    print(f"  模型:       {model_name}")
    print(f"  Collection: {collection_name}")
    print(f"  文档目录:   {config.DATA_DIR}")
    print(f"  清空重建:   {'是' if reset else '否（增量覆盖同名文件）'}")
    print()

    if not config.DATA_DIR.exists():
        print(f"[!!] 文档目录不存在: {config.DATA_DIR}")
        return 0

    started = time.perf_counter()

    embedder = Embedder(model_name=model_name)
    vectordb = VectorDB(collection_name=collection_name)

    if reset:
        vectordb.create_collection(reset=True)
    else:
        vectordb.get_collection()

    before = vectordb.count()
    print(f"  重建前条目数: {before}")
    print(f"  向量维度:     {embedder.get_embedding_dim()}")
    print()

    ingestion = DocumentIngestion(vectordb, embedder)
    stats = ingestion.ingest_directory(config.DATA_DIR, recursive=True)

    after = vectordb.count()
    elapsed = time.perf_counter() - started

    print()
    print("=" * 70)
    print(f"完成：{before} → {after} 条，耗时 {elapsed:.1f}s")
    print("=" * 70)

    # 抽样打印一条，确认 metadata 结构（doc_key / seq / total_chunks 必须存在，
    # M3 的相邻块查询依赖它们）
    sample = vectordb.get(limit=1)
    metadatas = sample.get("metadatas") or []
    if metadatas:
        meta = metadatas[0]
        print("\nmetadata 样例:")
        for key in ("file", "category", "doc_key", "seq", "total_chunks", "header"):
            flag = "" if key in meta else "  [!!] 缺失"
            print(f"  {key:<14} = {meta.get(key)!r}{flag}")

        missing = [k for k in ("doc_key", "seq", "total_chunks") if k not in meta]
        if missing:
            print(f"\n[!!] 定位字段缺失: {missing}")
            print("  这会导致 get_neighbors() 无法工作，请检查 ingestion 写入逻辑。")
        else:
            print("\n[OK] 定位字段完整")

    return stats.get("total_chunks", 0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="重建向量索引",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--model",
        default=config.EMBEDDING_MODEL_NAME,
        help=f"embedding 模型名（默认 {config.EMBEDDING_MODEL_NAME}）",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="重建前清空目标 collection（默认按文件名覆盖，保留其他文件）",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="只列出已有 collection，不做重建",
    )
    args = parser.parse_args()

    if args.list:
        list_collections()
        return 0

    chunks = reindex(args.model, args.reset)
    return 0 if chunks > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
