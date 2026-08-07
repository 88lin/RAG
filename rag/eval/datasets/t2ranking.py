"""
T2Ranking 数据集接入（检索层评测）

来源：THUIR/T2Ranking —— 中文真实搜索日志 + 人工分级相关性标注。
选它的理由：提供 qrels（query 到相关段落的人工标注），
这是算 Recall@k / MRR / nDCG 的前提。端到端评测集（如 CRUD-RAG）
不标注 chunk 级证据，算不出这些指标。

文件结构（TSV，带表头）：
    queries.dev.tsv          0.9 MB    qid \t text
    qrels.retrieval.dev.tsv  1.4 MB    qid \t pid
    collection.tsv        3489.7 MB    pid \t text

collection.tsv 有 3.5GB，不下载全量。策略：
  1. 先取 queries + qrels（合计 2.3 MB）
  2. 抽 N 条 query，收集其全部 gold pid
  3. 流式扫描 collection，只保留 gold pid + 一批随机干扰段落
  4. 干扰段落提供检索难度，使 Recall 数字有意义

网络注意：国内直连 huggingface.co 不通，走 hf-mirror.com。
镜像会 302 到 /api/resolve-cache/...，必须允许重定向。
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

import requests

from ..schema import EvalDoc, EvalQuery

DATASET_NAME = "t2ranking"

# hf-mirror 的 datasets 通道实测可用（模型通道不可用）
BASE_URL = "https://hf-mirror.com/datasets/THUIR/T2Ranking/resolve/main/data"

FILES = {
    "queries": "queries.dev.tsv",
    "qrels": "qrels.retrieval.dev.tsv",
    "collection": "collection.tsv",
}

# 每条 gold 段落配多少条干扰段落。
# 太少则 Recall 虚高（几乎没有竞争者），太多则语料膨胀、embedding 耗时线性增长。
# 8 倍使 300 条 query（约 1500 gold）产出约 1.3 万段语料，单机可承受。
DISTRACTOR_RATIO = 8

# 流式下载分块大小
CHUNK_BYTES = 1 << 20  # 1 MiB


def _download(url: str, dest: Path, timeout: int = 60) -> Path:
    """下载文件，支持断点续传。

    3.5GB 文件中断后从头再来不可接受，因此用 Range 请求续传。
    校验方式是比对 Content-Length：不匹配则重下（不做 checksum，
    数据集本身没提供）。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 先探远端大小
    head = requests.head(url, allow_redirects=True, timeout=timeout)
    head.raise_for_status()
    total = int(head.headers.get("content-length", 0))

    if dest.exists():
        local = dest.stat().st_size
        if total and local == total:
            print(f"  [skip] 已完整: {dest.name} ({local / 1048576:.1f} MB)")
            return dest
        if local > total > 0:
            print(f"  [warn] 本地文件比远端大，重新下载: {dest.name}")
            dest.unlink()
            local = 0
    else:
        local = 0

    headers = {}
    mode = "wb"
    if local > 0:
        # 服务端不支持 Range 时会返回 200 而非 206，此时必须从头写，
        # 否则会把完整内容追加到已有片段后面，产出损坏文件。
        headers["Range"] = f"bytes={local}-"
        mode = "ab"
        print(f"  [resume] 从 {local / 1048576:.1f} MB 续传: {dest.name}")

    response = requests.get(
        url, headers=headers, stream=True, allow_redirects=True, timeout=timeout
    )
    response.raise_for_status()

    if local > 0 and response.status_code == 200:
        print("  [warn] 服务端不支持断点续传，改为完整下载")
        mode = "wb"
        local = 0

    done = local
    # 进度按百分比节流：3.5GB 文件每 MiB 打一行会刷出三千多行，
    # 且 \r 在重定向到文件或管道时不生效。
    next_report = 0.0
    with dest.open(mode) as fh:
        for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
            if not chunk:
                continue
            fh.write(chunk)
            done += len(chunk)
            if total:
                pct = done / total * 100
                if pct >= next_report:
                    print(
                        f"  {dest.name}: {done / 1048576:>8.1f}/"
                        f"{total / 1048576:.1f} MB ({pct:.0f}%)",
                        flush=True,
                    )
                    next_report = pct + 10
    print(f"  {dest.name}: 完成 ({done / 1048576:.1f} MB)")
    return dest


def _read_tsv(path: Path, expect_cols: int = 2) -> Iterator[Tuple[str, ...]]:
    """逐行读 TSV，跳过表头与列数不符的行。

    流式设计：collection.tsv 3.5GB 不能一次读入内存。
    表头判定靠第一列是否为数字 —— 三份文件的第一列都是数值 id
    （qid / pid），表头则是字面量 "qid" / "pid"。
    """
    with path.open(encoding="utf-8", errors="replace") as fh:
        first = fh.readline()
        parts = first.rstrip("\n").split("\t")
        is_header = not parts[0].strip().isdigit()
        if not is_header and len(parts) >= expect_cols:
            yield tuple(parts[:expect_cols])

        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < expect_cols:
                continue
            yield tuple(parts[:expect_cols])


def load_qrels(path: Path) -> Dict[str, Set[str]]:
    """读 qrels，返回 qid -> {pid}。

    该文件只有 qid/pid 两列，无相关性等级，故按二元相关性处理
    （出现即相关）。nDCG 的 IDCG 计算需据此调整。
    """
    qrels: Dict[str, Set[str]] = {}
    for qid, pid in _read_tsv(path, 2):
        qrels.setdefault(qid, set()).add(pid)
    return qrels


def load_queries(path: Path) -> Dict[str, str]:
    """读 queries，返回 qid -> 查询文本。"""
    return {qid: text for qid, text in _read_tsv(path, 2) if text.strip()}


def sample_corpus(
    collection_path: Path,
    gold_pids: Set[str],
    distractor_count: int,
    seed: int = 42,
) -> Iterator[EvalDoc]:
    """流式扫描 collection，产出 gold 段落 + 随机干扰段落。

    只扫一遍 3.5GB，不把全量读进内存。
    干扰段落用 reservoir sampling：无需预知总行数，单遍完成，
    且每条被选中的概率相同。

    参数:
        gold_pids: 必须全部保留的段落 id
        distractor_count: 需要的干扰段落数
        seed: 固定随机种子，保证语料可复现

    产出:
        EvalDoc，gold 段落先出，干扰段落后出
    """
    rng = random.Random(seed)
    reservoir: List[Tuple[str, str]] = []
    seen_distractors = 0
    gold_found: Dict[str, str] = {}

    for idx, (pid, text) in enumerate(_read_tsv(collection_path, 2)):
        if not text.strip():
            continue

        if pid in gold_pids:
            gold_found[pid] = text
            continue

        # reservoir sampling：前 k 条直接放入，之后以 k/n 概率替换
        seen_distractors += 1
        if len(reservoir) < distractor_count:
            reservoir.append((pid, text))
        else:
            j = rng.randrange(seen_distractors)
            if j < distractor_count:
                reservoir[j] = (pid, text)

        if idx % 500_000 == 0 and idx:
            print(
                f"  扫描 collection: {idx:>10,} 行，"
                f"gold 命中 {len(gold_found)}/{len(gold_pids)}",
                flush=True,
            )

    missing = gold_pids - set(gold_found)
    if missing:
        # gold 段落缺失会让对应 query 的 Recall 恒为 0，必须显式告警
        print(f"  [!!] {len(missing)} 个 gold pid 未在 collection 中找到")

    for pid, text in gold_found.items():
        yield EvalDoc(doc_id=pid, text=text, metadata={"role": "gold"})
    for pid, text in reservoir:
        yield EvalDoc(doc_id=pid, text=text, metadata={"role": "distractor"})


def fetch(
    raw_dir: Path,
    limit: int = 300,
    seed: int = 42,
    skip_collection: bool = False,
) -> Tuple[List[EvalQuery], Optional[Iterator[EvalDoc]]]:
    """下载并抽样 T2Ranking。

    参数:
        raw_dir: 原始 TSV 存放目录
        limit: 抽取的 query 条数
        seed: 随机种子，控制 query 抽样与干扰段落抽样
        skip_collection: 跳过 3.5GB 的 collection 下载（只要 query 时用）

    返回:
        (queries, docs_iterator)。skip_collection 时 docs_iterator 为 None。
    """
    print(f"[{DATASET_NAME}] 下载 queries 与 qrels")
    queries_path = _download(f"{BASE_URL}/{FILES['queries']}", raw_dir / FILES["queries"])
    qrels_path = _download(f"{BASE_URL}/{FILES['qrels']}", raw_dir / FILES["qrels"])

    all_queries = load_queries(queries_path)
    qrels = load_qrels(qrels_path)
    print(f"  queries={len(all_queries):,}  带标注的 query={len(qrels):,}")

    # 只保留有标注的 query，否则无法算检索指标
    annotated = [qid for qid in qrels if qid in all_queries]
    rng = random.Random(seed)
    rng.shuffle(annotated)
    picked = annotated[:limit]

    eval_queries = [
        EvalQuery(
            qid=qid,
            query=all_queries[qid],
            gold_doc_ids=qrels[qid],
            query_type="unknown",  # T2Ranking 不提供类型标注
            source=DATASET_NAME,
        )
        for qid in picked
    ]

    gold_pids: Set[str] = set()
    for query in eval_queries:
        gold_pids |= query.gold_doc_ids

    print(f"  抽取 {len(eval_queries)} 条 query，涉及 {len(gold_pids):,} 个 gold 段落")

    if skip_collection:
        return eval_queries, None

    print(f"[{DATASET_NAME}] 下载 collection（3.5GB，支持断点续传）")
    collection_path = _download(
        f"{BASE_URL}/{FILES['collection']}", raw_dir / FILES["collection"]
    )

    distractor_count = len(gold_pids) * DISTRACTOR_RATIO
    print(f"  抽样：{len(gold_pids):,} gold + {distractor_count:,} 干扰段落")
    docs = sample_corpus(collection_path, gold_pids, distractor_count, seed=seed)

    return eval_queries, docs
