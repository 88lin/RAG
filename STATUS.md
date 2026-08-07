# STATUS

> 导航文件。与代码冲突时以代码和 `git log` 为准。

## 当前位置

**阶段**：M1 评测集与检索实验（`docs/plans/M1-evaluation.md`）
**进度**：T1/T2/T3 完成，检索层报告已出。T4/T5 未开始。
**HEAD**：`3eedab4`

**正在后台运行**：`rrf_rerank` 补跑（36/300，约 2 秒/条，预计 100 分钟）。
不阻塞其他工作，跑完后重新 `python scripts/run_eval.py score` 更新报告即可。

## M1 任务状态

| 任务 | 状态 | 说明 |
|---|---|---|
| T1 数据集接入 | 完成 | T2Ranking，300 query / 13,536 段语料 |
| T2 指标实现 | 完成 | 35 条单测，Recall/MRR/nDCG/延迟分位 |
| T3 实验 runner | 完成 | 5 variant，4 个已跑满 300 条 |
| T4 生成层评测 | 未开始 | Ragas + 人工抽检一致率 |
| T5 无答案阈值校准 | 未开始 | 依赖 T1 的数据 |
| T6 报告 | 部分 | 检索层已出，生成层待补 |

## 检索层核心结论（已实测，300 条）

| variant | R@5 | P50 ms |
|---|---|---|
| vector_minilm | 0.023 | 16.5 |
| vector_bge | **0.707** | 15.1 |
| bm25 | 0.586 | 42.5 |
| rrf | 0.708 | 52.1 |
| rrf_rerank | 0.770* | 18974.6 |

\* 24 条样本，仍在补跑

**换 embedding 模型使 Recall@5 提升约 30 倍**（0.023 → 0.707）。
MiniLM 在中文语料上 MRR@10 仅 0.058，等同随机排序。

**RRF 在本数据集上无增益**（0.708 vs 0.707），延迟涨 3.5 倍。
原因是 T2Ranking 以语义匹配为主，BM25 对向量路无有效补充。
这是诚实的负面结果，已写进报告并说明适用条件。

**Rerank 增益真实但 CPU 延迟 18.9 秒**，不可线上使用。
当前 `ENABLE_RERANK=false` 是正确默认。

详见 `docs/eval/report.md`。

## 未解决问题

**无答案识别（M0 遗留，M1 T5 处理）**：`ANSWERABLE_MIN_RELEVANCE=0.50`
在 59 chunk 小语料上拦不住无答案查询（"公司年会在哪个城市举办"得 0.692）。
需要用 T2Ranking 构造无答案子集扫描阈值，画识别率/误拒率曲线取拐点。

**RRF 未按 query 类型分层**：T2Ranking 不提供类型标注，
因此无法验证"BM25 在关键词类查询上更强"这一假设。

## 向量库现状

```
eval_t2ranking__baai_bge_small_zh_v1_5   13536   <- 评测（bge）
eval_t2ranking__all_minilm_l6_v2         13536   <- 评测（MiniLM 基线）
techcorp_docs__baai_bge_small_zh_v1_5       59   <- 生产默认
techcorp_docs__all_minilm_l6_v2             59   <- 生产对比基线
techcorp_docs                               21   <- 换指纹前旧库，可删
```

评测与生产物理隔离（前缀 `eval_`），否则生产检索会命中 T2Ranking 段落。

## 活跃决策

- 编排层手写状态机，不引入 LangGraph（节点签名对齐 StateGraph 形状）
- 向量留在 ChromaDB，不引入 pgvector；PG 只存关系数据与轨迹
- 检索指标自研（确定性运算可单测），生成层用 Ragas 但必须人工抽检报一致率
- 项目定位为通用可追溯 RAG 知识库研究 Agent，`data/documents/` 仅为 demo

## 待办观察（边界外）

- `scripts/` 下 4 个历史调试脚本仍引用已删配置
  （`4_run_rag.py`、`5_test_hybrid_rag.py`、`9_test_hybrid.py`、`7_test_rerank.py`），
  运行会 ImportError。非产品代码，清理或归档。
- `chat_service.py` 的"查询 ≤20 字则注入 jieba 实体"补救逻辑，
  换中文模型后必要性需重新评估 —— 它当初是为了弥补 MiniLM 的失效。
  同理 Multi-Query 扩展与已删的 Query Rewrite。
- **requests 在 hf-mirror 上会退化到约 12 KB/s，curl 能跑 9 MB/s**。
  大文件必须用 curl，已在 `t2ranking.py` 实现并记入 ADR-002。
- 所有 `scripts/` 入口必须把 stdout 切 UTF-8，否则 Windows GBK 控制台
  遇中文即 UnicodeEncodeError。
