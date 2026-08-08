# STATUS

> 导航文件。与代码冲突时以代码和 `git log` 为准。

## 当前位置

**阶段**：M1 评测集与检索实验 —— **已完成**
**下一步**：M2 基础设施（PG + Redis + 异步摄入）
**HEAD**：`3a57f7c`

## M1 任务状态

| 任务 | 状态 | 产出 |
|---|---|---|
| T1 数据集接入 | 完成 | 300 query / 13,536 段语料 |
| T2 指标实现 | 完成 | 35 条单测 |
| T3 实验 runner | 完成 | 5 variant × 300 条 |
| T4 生成层评测 | 完成 | 忠实度 0.916，核验一致率 85% |
| T5 阈值校准 | 完成 | 证明单一阈值不可行 |
| T6 报告 | 完成 | `docs/eval/report.md` 九节 |

## 检索层结论（300 条 T2Ranking）

| variant | R@5 | MRR@10 | nDCG@10 | P50 ms |
|---|---|---|---|---|
| vector_minilm | 0.023 | 0.058 | 0.029 | 16.5 |
| vector_bge | **0.707** | 0.907 | 0.854 | 15.1 |
| bm25 | 0.586 | 0.841 | 0.721 | 42.5 |
| rrf | 0.708 | 0.902 | 0.837 | 52.1 |
| rrf_rerank | **0.747** | **0.947** | **0.900** | 9841.7 |

三条结论，两条是负面结果：

1. **换中文 embedding 使 Recall@5 提升约 30 倍**（0.023 → 0.707）。
   MiniLM 的 MRR@10 仅 0.058，等同随机排序。
2. **RRF 在本数据集上无增益**（0.708 vs 0.707），延迟涨 3.5 倍。
   原因是该数据集语义匹配为主，BM25 单独 0.586 无互补空间。
   不是实现错误，**不要试图"修好"它**。
3. **单一 relevance 阈值无法判定可答性**。有答案与无答案的 top1 分布
   严重重叠（无答案最小值 0.714 > 有答案最小值 0.697）。
   误拒率 ≤10% 时最佳阈值只能识别 19.1% 的无答案查询。

## 生成层结论（50 条）

忠实度均值 **0.916**，中位数 1.000，44 条有效 / 6 条判 None。
人工核验 20 条，一致率 **85%**，不一致的 3 条全部偏紧、无偏松失效。

判分器自实现（不用 ragas，原因见报告第 7 节）。已知缺陷：
裁定器不检查断言间一致性，会低估部分条目。

## 未解决问题（移交 M3）

- **独立的 answerability 判断**：单一阈值已证明不可行。候选方向 ——
  LLM 生成前先判断"给定这些证据能否回答"、用 top1 与 top2 的分数差
  而非绝对值、引用校验前置。
- **rerank 可用性改造**：质量增益真实但 CPU P50 9.8 秒。
  待验证 GPU / 候选数削到 top-5 / 按路由选择性启用。
- **引用准确率与覆盖率**：依赖 M3 的 citation_verify。
- **BM25 分层验证**：T2Ranking 无 query 类型标注，
  无法验证"BM25 在关键词类查询上更强"。

## 向量库现状

```
eval_t2ranking__baai_bge_small_zh_v1_5   13536   评测（bge）
eval_t2ranking__all_minilm_l6_v2         13536   评测（MiniLM 基线）
techcorp_docs__baai_bge_small_zh_v1_5       59   生产默认
techcorp_docs__all_minilm_l6_v2             59   生产对比基线
techcorp_docs                               21   换指纹前旧库，可删
```

评测与生产物理隔离（前缀 `eval_`）。

## 活跃决策

- 编排层手写状态机，不引入 LangGraph（节点签名对齐 StateGraph 形状）
- 向量留在 ChromaDB，不引入 pgvector；PG 只存关系数据与轨迹
- 检索指标自研；生成层判分也自研（ragas 在 GLM 上跑不通）
- 项目定位为通用可追溯 RAG 研究平台，`data/documents/` 仅为 demo

## 待办观察（边界外）

- `scripts/` 下 4 个历史调试脚本仍引用已删配置
  （`4_run_rag.py`、`5_test_hybrid_rag.py`、`9_test_hybrid.py`、`7_test_rerank.py`），
  运行会 ImportError。非产品代码，清理或归档。
- `chat_service.py` 的"查询 ≤20 字则注入 jieba 实体"补救逻辑，
  以及 Multi-Query 扩展，当初都是为弥补 MiniLM 失效而加的。
  换中文模型后必要性需重新评估。
- **requests 在 hf-mirror 上约 12 KB/s，curl 能跑 9 MB/s**（差 700 倍）。
  大文件必须用 curl，已在 `t2ranking.py` 实现并记入 ADR-002。
- 所有 `scripts/` 入口必须把 stdout 切 UTF-8，否则 Windows GBK 控制台
  遇中文即 UnicodeEncodeError。
- 智谱拒绝 `temperature=1e-8`（要求两位小数），但接受 `0.0` 与 `0.01`。
