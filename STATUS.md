# STATUS

> 导航文件。与代码冲突时以代码和 `git log` 为准。

## 当前位置

**阶段**：M0 地基修正 —— **已完成**（`docs/plans/M0-foundation.md`）
**下一步**：M1 评测集与检索实验
**HEAD**：`c3f8ecc`

## M0 任务状态

| 任务 | 状态 |
|---|---|
| T1 统一相关性口径 | 完成 |
| T2 Embedder 双侧编码 | 完成 |
| T3 collection 模型指纹 | 完成 |
| T4 RRF 替换加权融合 | 完成 |
| T5 chunk 定位字段 | 完成 |
| T6 reindex.py | 完成 |
| T7 compare_embeddings.py | 完成 |

## 验收结果（A1-A9 全部通过）

| # | 标准 | 实测 |
|---|---|---|
| A1 | 业务代码无 `SIMILARITY_THRESHOLD` | 通过 |
| A2 | `scoring.py` 单测通过 | 31 passed |
| A3 | `rerank_logit=0.0` 不被跳过 | 通过（sigmoid(0)=0.5） |
| A4 | 两 collection 并存 384/512 维 | 通过 |
| A5 | metadata 含 doc_key/seq/total_chunks | 通过 |
| A6 | `get_neighbors` 可取相邻块 | seq=0→[1]，seq=2→[1,3]，seq=4→[3] |
| A7 | 10 条查询双模型并排输出 | 通过，见下 |
| A8 | `vue-tsc --noEmit` | exit 0 |
| A9 | 后端可导入 | 通过 |

## 换 embedding 模型的实测收益（A7）

10 条探针查询，**MiniLM 有 5 条 top1 命中错误文档，且错误全部指向
`health_insurance.md`** —— 英文模型在中文语料上退化为"总返回同一文档"，
即随机排序的典型表现。bge-small-zh-v1.5 全部命中正确文档。

| 查询 | MiniLM top1 | bge top1 |
|---|---|---|
| 宠物可以带到公司吗 | remote_work ✗ | pet_policy ✓ |
| 在家上班一周能几天 | health_insurance ✗ | remote_work ✓ |
| 头晕怎么办 | health_insurance ✗ | Daily_Log ✓ |
| 船舶总布置图含哪些舱室 | health_insurance ✗ | General_Arrangement ✓ |
| 船员出差住宿标准 | health_insurance ✗ | Charter_Party_Rider ✓ |

无答案类：MiniLM 给 0.875 / 0.809，bge 降到 0.703 / 0.723。方向正确但
**仍高于 `ANSWERABLE_MIN_RELEVANCE=0.50`** —— 见下方未解决问题。

注：relevance 绝对值在不同模型间不可比（分布不同）。bge 分数普遍略低但
命中率显著更高，不矛盾。定量结论以 M1 评测集为准。

## 端到端验证

`retrieve_advanced` 三条查询实测：
- `rrf_score` 量级 0.031~0.033，符合 `1/(60+1)+1/(60+2)`，两路命中时累加
- `retrieved_by=vector+bm25`，融合生效
- **排序与展示确实分离**：某条查询 relevance 为 0.838/0.810/0.822（不单调），
  rrf_score 为 0.03252/0.03178/0.03175（严格递减）。排序按 RRF，展示用 relevance

## 未解决问题（移交 M1）

**无答案识别在当前语料规模上无法工作。** "公司年会在哪个城市举办"（知识库
确实没有）得到 relevance 0.692，高于阈值 0.50，会被判定为可答。
根因是语料只有 59 chunk：BM25 对"公司"必然命中，向量侧总能找到最近邻。
这不是调阈值能解决的，需要 M1 用评测集的 12 条无答案问题正面处理
（阈值校准曲线 + 可能需要引入 answerability 判断）。

## 向量库现状

```
techcorp_docs__baai_bge_small_zh_v1_5    59   <- 当前默认（512 维）
techcorp_docs__all_minilm_l6_v2          59   <- 对比基线（384 维）
techcorp_docs                            21   <- 换指纹前的旧库，可安全删除
```

## 活跃决策

- 编排层手写状态机，不引入 LangGraph（节点签名对齐 StateGraph 形状，M5 可选加 adapter）
- 向量留在 ChromaDB，不引入 pgvector；PG 只存关系数据与轨迹
- 阈值初值 0.35 / 0.50 为拍定值，M1 用数据校准（已确认 0.50 偏低）

## 待办观察（边界外）

- `scripts/` 下 4 个历史调试脚本仍引用已删配置（`4_run_rag.py`、
  `5_test_hybrid_rag.py`、`9_test_hybrid.py`、`7_test_rerank.py`），
  运行会 ImportError。非产品代码，M1 前清理或归档。
- `ingestion.ingest_directory` 用 `print(..., end="")` 与 UTF-8 wrapper 交互，
  部分文件名不显示。仅影响日志观感。
- 国内直连 HuggingFace 不稳定，MiniLM 重建耗时 10401s 几乎全在网络重试。
  hf-mirror 首页可达但模型 API 路径不通。
- `chat_service.py` 那段"查询 ≤20 字则注入 jieba 实体"的补救逻辑，
  换中文模型后是否还需要，M1 用评测集验证后再决定去留。
