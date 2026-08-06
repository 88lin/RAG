# M0 · 地基修正

**目标**：让检索质量与相关性口径可信,为 M1 评测集提供可比较的基线。
**预算**：6-8 小时
**前置**：无
**阻塞**：M1 全部内容

## 为什么先做这个

当前仓库存在三个使指标失效的问题,不修则 M1 产出的任何数字都无法解释:

1. **Embedding 模型与语料语言不匹配** — `all-MiniLM-L6-v2` 是英文模型,知识库与查询均为中文。
   连带后果:`ENABLE_MULTI_QUERY` 被实测判定为"导致检索退化"、Query Rewrite 被整段删除、
   `chat_service.py` 中出现"查询 ≤20 字则注入 jieba 实体"的硬编码补救。
2. **`SIMILARITY_THRESHOLD` 一个数字承担两种物理量** —
   `chat_service.py:297` 当作 `1-阈值 = hybrid_score 下限`(越大越好),
   `chat_service.py:305` 当作余弦距离上限(越小越好)。
3. **Hybrid 两路分数尺度不可比** — 向量侧绝对距离转换 `1-d/2`,
   BM25 侧相对归一化 `score/max`。后者使"本批最好的"恒为 1.0,即使完全不相关。
   故 `VECTOR_WEIGHT=0.7 / BM25_WEIGHT=0.3` 无物理意义。

## 任务清单

### T1 · 统一相关性口径

新建 `rag/scoring.py`,纯函数,无 IO:

- `compute_relevance(result) -> float` — 返回 `[0,1]`,越大越相关
  - 有 rerank logit → `sigmoid(logit)`(CrossEncoder 以 BCE 训练,sigmoid 后即相关概率)
  - 否则 → `max(0, 1 - cosine_distance/2)`
- `rrf_fuse(*ranked_lists, k=60) -> list[(doc_id, score)]`

`config.py` 变更:
- 删 `SIMILARITY_THRESHOLD`、`BM25_WEIGHT`、`VECTOR_WEIGHT`、`MIN_RELEVANCE_SCORE`
- 加 `RETRIEVAL_MIN_RELEVANCE=0.35`(进上下文门槛)
- 加 `ANSWERABLE_MIN_RELEVANCE=0.50`(判定无答案门槛)
- 加 `RRF_K=60`

同时修掉:
- `chat_service.py:253` 的 `or` 链 falsy bug(`rerank_score=0.0` 被跳过)
- `retriever.py:870-884` 的阈值放宽兜底(破坏无答案识别,M1 有 12 条该类问题)
- `retriever.py:481` `invalidate_bm25_cache()` 的 `AttributeError`
  (索引构建失败时 `_bm25_docs` 未赋值却被无条件 `del`)
- `useChat.ts:130-141` 前端分段拉伸(0.7 以上 ×1.1、0.4 以下 ×0.6)

### T2 · Embedder 双侧编码 + 换 bge-small-zh-v1.5

bge 系列查询侧需指令前缀,文档侧不需要,该非对称性必须复现:

```python
BGE_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
encode_query(text)      # 加前缀
encode_documents(texts) # 不加前缀
```

两者均 `normalize_embeddings=True`,使余弦距离落在可预期区间。
前缀是否启用由模型名判断(`bge` 且 `zh`),换回 MiniLM 时自动不加。

### T3 · Collection 名带模型指纹

`{COLLECTION_NAME}__{model_slug}`,例:
- `techcorp_docs__all_minilm_l6_v2`
- `techcorp_docs__baai_bge_small_zh_v1_5`

理由:维度不同(384 vs 512)会直接报错;维度相同则静默返回垃圾结果且难以排查。
物理隔离是 A/B 对比与回滚的前提。

### T4 · RRF 替换加权融合

只用排名,不用分数。归一化、权重调参、尺度不可比三个问题一并消除。
排序用 RRF 分数,展示与阈值判断用 `compute_relevance()`,两者分离。

### T5 · chunk metadata 补 doc_key / seq / total_chunks

当前序号只存在于 id 字符串(`{doc_id}_chunk_{i}`),metadata 中没有。
Chroma `where` 无法对 id 做前缀或范围查询,故 M3 的 `get_document_context`
(取相邻 chunk 以跨越分块边界)无法实现。

放在 M0 的原因:本阶段因换模型必须重灌数据,两件事合并一次完成。

### T6 · scripts/reindex.py

参数化 `--model` / `--collection` / `--reset` 的可重复重建脚本。
兼作"数据可重建"保证。

### T7 · scripts/compare_embeddings.py

同一批查询在两个 collection 下并排打印 top5 与 relevance。

## 验收标准

| # | 标准 | 验证方式 |
|---|---|---|
| A1 | `grep -rn "SIMILARITY_THRESHOLD" --include=*.py` 无业务代码命中 | 命令输出为空 |
| A2 | `rag/scoring.py` 单测通过,含 RRF 与 sigmoid 边界 | `pytest tests/test_scoring.py` |
| A3 | `rerank_score=0.0` 不再被 `or` 链跳过 | 单测断言 |
| A4 | 两个 collection 并存,维度分别为 384 / 512 | `compare_embeddings.py` 输出 |
| A5 | 任一 chunk 的 metadata 含 `doc_key`/`seq`/`total_chunks` | 脚本打印样例 |
| A6 | `where={"doc_key":X,"seq":{"$in":[..]}}` 能取到相邻 chunk | 脚本实测 |
| A7 | 10 条查询在 MiniLM 与 bge 下 top5 并排输出 | 终端截图 |
| A8 | 前端 `vue-tsc --noEmit` 通过 | exit 0 |
| A9 | 后端可正常导入并启动 | 导入检查 + `/health` |

## 明确不做

- 不引入 PostgreSQL / Redis(M2)
- 不做 Agent 编排(M3)
- 不做前端视觉美化(M5 之后)
- 不建评测集(M1)
- 不调阈值初值 — 0.35/0.50 为拍定值,M1 用数据校准

## 失败条件

- 若 bge 在 A7 对比中未显著优于 MiniLM,先排查前缀与 normalize 是否生效,
  不得直接接受结果继续推进。
- 若重灌数据后文档数与 chunk 数异常,回滚到 MiniLM collection(仍在库中)。
