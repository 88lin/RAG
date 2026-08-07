"""
评测数据的统一中间格式

存在的理由：把「数据集长什么样」与「runner 怎么用」解耦。
换数据集（T2Ranking -> DuReader-retrieval）只需新写一个 loader，
runner、metrics、报告生成全部不动。

两类评测集提供的信息不同，本 schema 用可选字段兼容两者：

    检索评测集（T2Ranking）  -> gold_doc_ids 有值，gold_answer 为 None
    端到端评测集（CRUD-RAG） -> gold_answer 有值，gold_doc_ids 可能为空

混淆这两类会让报告失效：端到端集不标注"哪个 chunk 是正确证据"
（chunk 边界取决于使用方的分块策略），因此算不出 Recall@k。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set

# 查询类型。检索评测集通常不带类型标注，统一落到 "unknown"；
# 无答案子集由 T5 的阈值校准脚本构造，标为 "unanswerable"。
QUERY_TYPES = (
    "fact",
    "keyword",
    "paraphrase",
    "multi_hop",
    "calculation",
    "unanswerable",
    "prompt_injection",
    "unknown",
)


@dataclass
class EvalDoc:
    """语料库中的一个段落。

    doc_id 必须与 qrels 中的 pid 一致 —— 命中判定靠它，写错会让
    Recall 恒为 0 且不报错。
    """

    doc_id: str
    text: str
    metadata: Dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class EvalQuery:
    """一条评测查询。

    字段:
        qid: 查询标识，需在数据集内唯一
        query: 查询文本
        gold_doc_ids: 人工标注的相关段落 id 集合。检索层指标的依据。
                      空集合意味着"该查询无正确答案"，
                      用于无答案识别评测 —— 不是"标注缺失"。
        gold_answer: 标准答案文本，仅端到端评测集提供
        query_type: 见 QUERY_TYPES
        source: 数据集名，报告里要按来源分组
    """

    qid: str
    query: str
    gold_doc_ids: Set[str] = field(default_factory=set)
    gold_answer: Optional[str] = None
    query_type: str = "unknown"
    source: str = "unknown"

    def __post_init__(self) -> None:
        if not self.qid:
            raise ValueError("qid 不能为空")
        if not self.query or not self.query.strip():
            raise ValueError(f"query 不能为空 (qid={self.qid})")
        if self.query_type not in QUERY_TYPES:
            raise ValueError(
                f"未知 query_type={self.query_type!r} (qid={self.qid})，"
                f"合法值: {QUERY_TYPES}"
            )
        # set 在 JSON 中不可序列化，统一在读写边界转换
        if not isinstance(self.gold_doc_ids, set):
            self.gold_doc_ids = set(self.gold_doc_ids)

    @property
    def is_answerable(self) -> bool:
        """是否存在正确答案。无答案识别评测的真值。"""
        return bool(self.gold_doc_ids) or bool(self.gold_answer)

    def to_json(self) -> str:
        payload = asdict(self)
        # 排序保证同一份数据每次序列化结果一致，便于 diff 与缓存校验
        payload["gold_doc_ids"] = sorted(self.gold_doc_ids)
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "EvalQuery":
        payload = json.loads(line)
        payload["gold_doc_ids"] = set(payload.get("gold_doc_ids") or [])
        return cls(**payload)


def write_queries(path: Path, queries: List[EvalQuery]) -> int:
    """写 JSONL，返回写入条数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for query in queries:
            fh.write(query.to_json() + "\n")
    return len(queries)


def read_queries(path: Path) -> List[EvalQuery]:
    """读 JSONL。"""
    with path.open(encoding="utf-8") as fh:
        return [EvalQuery.from_json(line) for line in fh if line.strip()]


def write_docs(path: Path, docs: Iterator[EvalDoc]) -> int:
    """流式写语料 JSONL，返回写入条数。

    接受迭代器而非列表：语料可能上万条，不必全部驻留内存。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for doc in docs:
            fh.write(doc.to_json() + "\n")
            count += 1
    return count


def read_docs(path: Path) -> Iterator[EvalDoc]:
    """流式读语料 JSONL。"""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            payload = json.loads(line)
            yield EvalDoc(**payload)
