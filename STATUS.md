# STATUS

> 导航文件。与代码冲突时以代码和 `git log` 为准。

## 当前位置

**阶段**：M0 地基修正（`docs/plans/M0-foundation.md`）
**进度**：7 项任务代码完成，验收卡在 A2/A3/A7 —— 阻塞于 `rag/scoring.py` 两个函数待实现
**HEAD**：`f1672a4`

## 阻塞项

`rag/scoring.py` 的 `compute_relevance()` 与 `rrf_fuse()` 仍是 `NotImplementedError`（由用户实现，非 AI 代写）。

影响面：整条检索链路在运行时会抛错。`tests/test_scoring.py` 31 条测试是其规格说明，实现完成后应全绿。

## M0 任务状态

| 任务 | 状态 | 说明 |
|---|---|---|
| T1 统一相关性口径 | 部分 | 调用方全部改完；`scoring.py` 两个函数待实现 |
| T2 Embedder 双侧编码 | 完成 | bge-small-zh-v1.5，前缀实测生效（查询/文档编码余弦 0.887） |
| T3 collection 模型指纹 | 完成 | 三个 collection 并存，旧数据未销毁 |
| T4 RRF 替换加权融合 | 部分 | retriever 改造完成，依赖 `rrf_fuse` |
| T5 chunk 定位字段 | 完成 | 两条摄入路径统一写入 doc_key/seq/total_chunks |
| T6 reindex.py | 完成 | 两个模型各重建成功，均 59 chunk |
| T7 compare_embeddings.py | 完成 | 脚本就绪，运行阻塞于 T1 |

## 验收状态（A1-A9）

| # | 标准 | 结果 |
|---|---|---|
| A1 | 业务代码无 `SIMILARITY_THRESHOLD` | 通过 |
| A2 | `scoring.py` 单测通过 | **阻塞** — 30 failed / 1 passed |
| A3 | `rerank_logit=0.0` 不被 `or` 链跳过 | **阻塞** — 同 A2 |
| A4 | 两 collection 并存，384 / 512 维 | 通过 |
| A5 | metadata 含 doc_key/seq/total_chunks | 通过 |
| A6 | `get_neighbors` 可取相邻块 | 通过 — seq=0→[1]，seq=2→[1,3]，seq=4→[3] |
| A7 | 10 条查询双模型并排输出 | **阻塞** — 同 A2 |
| A8 | `vue-tsc --noEmit` | 通过 — exit 0 |
| A9 | 后端可导入 | 通过 |

## 向量库现状

```
techcorp_docs__baai_bge_small_zh_v1_5    59   <- 当前默认（512 维）
techcorp_docs__all_minilm_l6_v2          59   <- 对比基线（384 维）
techcorp_docs                            21   <- 换指纹前的旧库，可安全删除
```

## 活跃决策

- 编排层手写状态机，不引入 LangGraph（节点签名对齐 StateGraph 形状，M5 可选加 adapter）
- 向量留在 ChromaDB，不引入 pgvector；PG 只存关系数据与轨迹
- 阈值初值 `RETRIEVAL_MIN_RELEVANCE=0.35` / `ANSWERABLE_MIN_RELEVANCE=0.50` 为拍定值，M1 用数据校准

## 待办观察（边界外，不在当前阶段处理）

- `scripts/` 下 5 个历史调试脚本仍引用已删配置（`4_run_rag.py`、`5_test_hybrid_rag.py`、`9_test_hybrid.py`、`7_test_rerank.py`），运行会报错。非产品代码，M1 前清理或归档。
- `ingestion.ingest_directory` 用 `print(..., end="")` 与 UTF-8 wrapper 交互，导致部分文件名不显示。仅影响日志观感。
- 国内直连 HuggingFace 不稳定，MiniLM 重建耗时 10401s 几乎全在网络重试。hf-mirror 首页可达但 API 路径不通。
- `chat_service.py` 那段"查询 ≤20 字则注入 jieba 实体"的补救逻辑，换中文模型后是否还需要，M1 用评测集验证后再决定去留。

## 下一步

1. 用户实现 `rag/scoring.py` → 跑通 A2/A3
2. 运行 `scripts/compare_embeddings.py` → 完成 A7，确认 bge 收益
3. M0 收尾，进入 M1（评测集与检索实验）
