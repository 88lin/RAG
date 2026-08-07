# 交接 · 2026-08-07（M0 完成）

> 本文件每次会话覆写。历史归档为 `docs/handoffs/M{n}-YYYY-MM-DD.md`。

## M0 已完成

A1-A9 全部通过。八个提交：

```
c963a67  docs: 建立 Agent 工作契约与文档骨架
78867fc  test: 添加 scoring 模块规格测试与接口定义
7c2f051  feat: 换用中文 embedding 模型并隔离向量集合
101c9a7  feat: chunk 元数据补定位字段，支持取相邻切片
9034917  refactor: 用 RRF 替换加权融合，统一相关性口径
dc92b31  fix: 修正相关性展示口径，仪表盘数值可解释
f1672a4  feat: 添加索引重建与模型对比脚本
035f87d  docs: 记录 M0 进度、交接说明与 RRF 选型决策
c3f8ecc  feat: 实现相关性计算与 RRF 融合
```

`rag/scoring.py` 由用户实现，审查时修掉一处会影响正确性的 bug：
夹紧的对象搞错了 —— 原实现把**距离**夹到 `[0,1]`，但余弦距离值域是 `[0,2]`，
导致 `[1,2]` 区间全部塌陷到 relevance=0.5，恰好卡在
`ANSWERABLE_MIN_RELEVANCE` 门槛上。改为夹紧**输出**。
另外把 `if/elif` 改成逐级回退，并显式排除 `bool`。

## 下一步：M1 评测集与检索实验

计划书还没写，第一件事是写 `docs/plans/M1-evaluation.md`。

关键设计（在 M0 讨论中已确定，不要重新发明）：

- **标注到「文档 + 文本锚点」，不要标到 chunk_id。** 分块策略一改，
  chunk_id 标注全废。命中判定 = `chunk.metadata.file ∈ gold_docs`
  且 chunk 文本含任一 anchor。这样换分块、换 embedding、换 top_k 都能复用。
- **80-100 条，7 类**：fact ~25 / keyword ~15 / paraphrase ~15 /
  multi_hop ~12 / calculation ~8 / unanswerable ~12 / prompt_injection ~8
- **先标 30 条跑通管线**，M3 之后再补到 100 条重跑。早暴露 metrics 实现的 bug。
- **5 个 variant**：vector+MiniLM（baseline）/ vector+bge / bm25 /
  rrf / rrf+rerank
- LLM-as-judge 打分，但必须人工抽检 20-30 条并在报告里写明一致率。

## M1 必须正面解决的问题

**无答案识别在当前语料规模上不工作。** "公司年会在哪个城市举办"
得到 relevance 0.692，高于阈值 0.50。根因是语料只有 59 chunk：
BM25 对"公司"必然命中，向量侧总能找到最近邻。调阈值治不了，
需要阈值校准曲线，可能还要单独的 answerability 判断。
评测集里那 12 条 unanswerable 就是为这个准备的。

## 关键事实（避免重新发现）

- **换模型的收益是实测过的**：10 条探针里 MiniLM 有 5 条 top1 错误，
  且错误全部指向 `health_insurance.md` —— 英文模型在中文语料上退化成
  "总返回同一文档"。bge 全部命中正确。详见 STATUS.md。
- **relevance 绝对值在不同模型间不可比。** bge 分数普遍比 MiniLM 略低但
  命中率显著更高。不要用"谁分高"判断模型好坏。
- **前缀生效的验证方法**：`python -m rag.embedder` 输出查询/文档编码
  同一句话的余弦相似度，当前 0.887。若接近 1.0 说明前缀失效。
- **排序与展示已分离且可验证**：实测某查询 relevance 不单调
  （0.838/0.810/0.822）而 rrf_score 严格递减（0.03252/0.03178/0.03175）。
- **仪表盘 46% 问题的根因**：`or` 链混用三种量纲 + 前端分段拉伸。
  两处都已删除，现由后端下发 `relevance` + `relevance_basis`。
- **`techcorp_docs`（21 条）是换指纹前的旧库**，留着不影响逻辑，可随时删。

## 环境提醒

- 国内直连 HuggingFace 不稳定。`bge-small-zh-v1.5` 与 MiniLM 均已缓存。
  hf-mirror.com 首页 200 但模型 API 路径不通；ModelScope 可达但未接入。
- 所有 `scripts/` 入口必须把 stdout 切 UTF-8，否则 Windows GBK 控制台
  遇中文即 UnicodeEncodeError。本次已因此中断过两次重建。
- 跑 pytest 前确认 `venv/Scripts/python.exe -m pytest`，
  `.gitignore` 曾把 `test_*.py` 全局忽略（已收窄为 `/test_*.py`）。
