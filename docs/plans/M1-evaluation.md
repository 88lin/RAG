# M1 · 评测集与检索实验

**目标**：用公开人工精标数据集，量化 5 个检索方案的差异，产出可写进简历的对比报告。
**预算**：14-18 小时
**前置**：M0 完成（相关性口径统一、RRF、collection 指纹）
**阻塞**：M3 的路由与工具设计需要本阶段的延迟与召回基线

## 为什么不自己标注

项目定位是**通用可追溯 RAG 知识库研究 Agent**，`data/documents/` 下的海事文档
只是 demo。主评测应当用公开数据集，理由有三：

1. **规模**。59 个 chunk 的探针实验（M0 的 A7）只能定性，任何 Recall 数字都无统计意义。
2. **可信度**。自己标注 + 自己评测 = 循环论证，面试一问就穿。公开集是第三方人工精标。
3. **可比性**。T2Ranking 有公开 baseline，"我的 RRF 方案 Recall@5 达到 X"这句话
   才有参照系。

垂直海事领域没有完整的「知识库 + 问答对 + 溯源 chunk」RAG 闭环评测集
（MaritimeBench 是航运选择题，不是检索评测），所以海事文档只做定性验收。

## 数据集选型与实测可达性

**一个必须先讲清的区分**：RAG 评测集分两类，混淆会让整份报告失效。

| 类型 | 提供什么 | 能算什么 | 代表 |
|---|---|---|---|
| 检索评测集 | query + 段落库 + **qrels 相关性标注** | Recall@k、MRR、nDCG | T2Ranking、DuReader-retrieval、BEIR |
| 端到端评测集 | query + 知识库 + **标准答案** | 答案正确性、忠实度 | CRUD-RAG、Ragas 默认场景 |

端到端集**不标注"哪个 chunk 是正确证据"** —— 因为 chunk 边界取决于使用方的
分块策略，数据集作者无法预先标注。所以 CRUD-RAG 算不出 Recall@5。

### 已实测的可达性（2026-08-07）

```
hf-mirror.com  datasets API                      200   可用
hf-mirror.com  模型 API                          不通  （M0 已知）
huggingface.co 直连                              000   不通
modelscope.cn  api/v1/datasets                   500
```

**T2Ranking**（检索层，THUIR，中文真实搜索日志 + 人工分级标注）

```
https://hf-mirror.com/datasets/THUIR/T2Ranking/resolve/main/data/
  queries.dev.tsv            0.9 MB    qid → 查询文本
  qrels.retrieval.dev.tsv    1.4 MB    qid → pid（人工标注的相关段落）
  collection.tsv           3489.7 MB   pid → 段落文本
```

纯 TSV，不需要 `datasets` 库。注意 hf-mirror 会 302，curl 必须带 `-L`。

**CRUD-RAG**（生成层，IAAR-Shanghai，中文人工精标）
仅在 GitHub，不在 HF：

```
https://raw.githubusercontent.com/IAAR-Shanghai/CRUD_RAG/main/
  data/crud_split/split_merged.json    26.9 MB   单跳/多跳问答 + 标准答案
  data/crud/merged.zip                           知识库原文
```

### collection.tsv 3.5GB 的处理策略

不下载全量。做法：

1. 先下 queries + qrels（合计 2.3 MB）
2. 抽 N 条 query（默认 300），收集其全部 gold pid
3. **流式扫描** collection.tsv（`stream=True` 逐行读，不落盘全量），
   只保留 gold pid + 一批随机干扰段落
4. 干扰段落数按 `gold 数 × 8` 取，使语料规模约 1-2 万段

这样最终语料几千到上万条，ChromaDB 吃得下，且 Recall/MRR 依然有效
（gold 全在库内，干扰段落提供检索难度）。抽样比例与干扰倍数写进报告。

## 任务清单

### T1 · 数据集接入（3-4h）

```
rag/eval/
  __init__.py
  datasets/
    t2ranking.py     # 下载 + 流式抽样 + 转统一格式
    crud_rag.py      # 下载 + 解析
  schema.py          # EvalQuery / EvalCorpus 数据类
scripts/
  fetch_eval_data.py # 一次性下载，支持 --limit / --resume
```

统一中间格式（与具体数据集解耦，后续换数据集不动 runner）：

```python
@dataclass
class EvalQuery:
    qid: str
    query: str
    gold_doc_ids: set[str]        # 检索层：qrels 的 pid
    gold_answer: str | None        # 生成层：标准答案
    query_type: str                # fact / multi_hop / unanswerable ...
```

断点续传是硬要求 —— 3.5GB 流式扫描中断后不能从头再来。

### T2 · 指标实现（2-3h）

```
rag/eval/metrics.py     # 纯函数，无 IO
tests/test_metrics.py
```

检索层**自己实现**，不用框架：Recall@k / MRR@k / nDCG@k 都是确定性集合运算，
可单测、可复现，不该让 LLM 判。

```python
def recall_at_k(retrieved: list[str], gold: set[str], k: int) -> float
def mrr_at_k(retrieved: list[str], gold: set[str], k: int) -> float
def ndcg_at_k(retrieved: list[str], gold: set[str], k: int) -> float
def latency_percentiles(samples: list[float]) -> dict  # P50/P95/P99
```

边界必须覆盖：gold 为空、retrieved 为空、k 大于结果数、重复 id、
全部命中、全部未命中。nDCG 用二元相关性（qrels 是 0/1），IDCG 按
`min(len(gold), k)` 计算。

### T3 · 实验 runner（3-4h）

```
rag/eval/runner.py
scripts/run_eval.py    # --variant / --limit / --out
```

5 个 variant：

| id | 配置 | 用途 |
|---|---|---|
| `vector_minilm` | 纯向量 + all-MiniLM-L6-v2 | baseline，量化换模型收益 |
| `vector_bge` | 纯向量 + bge-small-zh-v1.5 | 单路上限 |
| `bm25` | 纯 BM25 | 关键词路的独立贡献 |
| `rrf` | RRF(vector_bge + bm25) | M0 的默认配置 |
| `rrf_rerank` | RRF + bge-reranker-base | 精排增益与延迟代价 |

每个 variant 一个独立 collection（M0 的指纹机制已支持）。
逐条记录 `qid / retrieved_ids / relevance / 各阶段耗时`，落 JSONL，
便于事后重算指标而不重跑检索。

### T4 · 生成层评测（3-4h）

接 Ragas，指标：`faithfulness`、`answer_relevancy`、`answer_correctness`。
判分模型用当前配置的 LLM（GLM-4-Flash）。

**必须人工抽检 20-30 条并在报告中写明一致率。** LLM-as-judge 不可全信，
这条本身就是专业信号。抽检记录落 `docs/eval/human_check.md`。

引用层指标由本系统自产（M3 的 citation_verify 之后才能完整算）：
- 引用准确率 = verified 引用数 / 有引用的句子数
- 引用覆盖率 = 有引用的句子数 / 需要证据的句子数

本阶段先留接口，M3 补齐。

### T5 · 无答案阈值校准（2h）

M0 遗留问题：59 chunk 语料下 `ANSWERABLE_MIN_RELEVANCE=0.50` 拦不住
"公司年会在哪个城市举办"（得 0.692）。

做法：从 T2Ranking 构造无答案子集（取 query 但把其 gold pid 全部
从语料中移除），扫描阈值 0.3~0.9，画两条曲线：

- 无答案识别率（正确判定为不可答的比例）
- 误拒率（有答案却被判不可答的比例）

取拐点作为新默认值，并在报告中给出曲线图与选点理由。
这一节是"阈值不是拍的，是量出来的"的证据。

### T6 · 报告（2h）

```
docs/eval/
  report.md            # 主报告
  human_check.md       # 人工抽检记录与一致率
  runs/*.jsonl         # 原始逐条记录（gitignore 大文件，保留摘要）
```

主报告结构：

1. 实验设置（数据集版本、抽样方式、干扰倍数、硬件、模型版本）
2. 检索层对比表（5 variant × Recall@5/MRR@10/nDCG@10/P50/P95）
3. 换 embedding 模型的收益（vector_minilm vs vector_bge）
4. RRF 相比单路的增益（rrf vs vector_bge / bm25）
5. Rerank 的增益与延迟代价（rrf_rerank vs rrf）
6. 无答案阈值校准曲线
7. 生成层指标 + 人工抽检一致率
8. 已知局限（抽样规模、单机延迟、judge 模型偏差）

## 验收标准

| # | 标准 | 验证方式 |
|---|---|---|
| B1 | `pytest tests/test_metrics.py` 全绿，含边界用例 | 命令输出 |
| B2 | `fetch_eval_data.py` 可断点续传，中断后重跑不从头开始 | 手动中断实测 |
| B3 | 5 个 variant 各自跑完 ≥300 条 query，产出 JSONL | 文件行数 |
| B4 | 报告含 5×5 对比表，数字可从 JSONL 重算复现 | 重算校验脚本 |
| B5 | `vector_bge` 的 Recall@5 显著高于 `vector_minilm` | 报告表格 |
| B6 | 无答案阈值校准曲线，给出新默认值与选点理由 | 报告章节 |
| B7 | 人工抽检 ≥20 条，报告写明与 LLM judge 的一致率 | human_check.md |
| B8 | 检索延迟 P50/P95 分 variant 记录 | 报告表格 |

## 明确不做

- 不引入 PostgreSQL / Redis（M2）
- 不做 Agent 编排（M3）
- 不追求 SOTA 检索效果，目标是**可解释的对比**而非刷分
- 不用 Evalscope（偏模型打榜，非 pipeline 评测）
- 不在本阶段算引用准确率（依赖 M3 的 citation_verify）

## 失败条件与回退

- **hf-mirror 数据集通道失效** → 改用 DuReader-retrieval（百度，国内可达）
  或 mMARCO 中文子集；统一中间格式已隔离数据源，只需换 loader。
- **CRUD-RAG 26MB 拉不下来** → 生成层评测降级为用 T2Ranking 的 query
  自行生成答案后只算 faithfulness（不需要 gold answer）。
- **`vector_bge` 未显著优于 baseline** → 先查前缀是否生效
  （`python -m rag.embedder`，余弦应 <1.0，当前 0.887），
  再查 collection 是否配错，不得直接接受结果。
