# ADR-002 · 评测数据集选型：T2Ranking + CRUD-RAG

**日期**：2026-08-07
**状态**：已采纳
**相关**：`rag/eval/datasets/`、`docs/plans/M1-evaluation.md`

## 背景

项目定位是**通用可追溯 RAG 知识库研究 Agent**，`data/documents/` 下的
海事文档只是 demo。需要为检索方案对比选择评测数据。

三个候选路径：自己人工标注、用公开数据集、半自动生成。

## 决策

**主评测用公开数据集，海事文档只做定性验收。**

- 检索层：**T2Ranking**（THUIR，中文真实搜索日志 + 人工分级标注）
- 生成层：**CRUD-RAG**（IAAR-Shanghai，中文人工精标）

不自己标注。不用 LLM 生成评测集作为主评测。

## 理由

1. **规模决定统计意义**。M0 的探针实验只有 59 个 chunk、10 条查询，
   只能定性说"MiniLM 有 5 条 top1 错了"，任何 Recall 数字都无意义。
   T2Ranking 有 22812 条带标注 query，抽 300 条即有统计意义。
2. **避免循环论证**。用同一个 LLM 生成 query、生成标准答案、再判分，
   等于让被测系统给自己出考卷加判卷。这在面试场景一问就穿。
3. **可比性**。T2Ranking 有公开 baseline，"我的 RRF 方案 Recall@5 达到 X"
   才有参照系。自建集没有任何外部锚点。
4. **垂直领域无闭环评测集**。海事领域只有 MaritimeBench（航运选择题），
   没有「知识库 + 问答对 + 溯源 chunk」的完整 RAG 评测集。
   强行自建的成本（4-6 小时纯人工）换不到相应的可信度。

## 一个必须澄清的区分

RAG 评测集分两类，混淆会让整份报告失效：

| 类型 | 提供 | 能算 | 代表 |
|---|---|---|---|
| 检索评测集 | query + 段落库 + **qrels** | Recall@k、MRR、nDCG | T2Ranking、BEIR |
| 端到端评测集 | query + 知识库 + **标准答案** | 答案正确性、忠实度 | CRUD-RAG、Ragas 场景 |

端到端集**不标注"哪个 chunk 是正确证据"** —— chunk 边界取决于使用方的
分块策略，数据集作者无法预先标注。所以 CRUD-RAG 算不出 Recall@5，
只用它会让"检索方案对比"这一节失去硬指标。

这也是为什么两个数据集都要接：各算各的层。

## 指标实现：检索层自研，生成层用 Ragas

Recall@k / MRR / nDCG 是确定性集合运算，自己实现（`rag/eval/metrics.py`，
纯函数，35 条单测）。让 LLM judge 介入只会引入方差，且无法解释。

忠实度、答案正确性确实需要语义判断，交给 Ragas。但**必须人工抽检
20-30 条并在报告中写明一致率** —— LLM-as-judge 不可全信。

不用 Evalscope：它偏模型 benchmark 打榜，不是 RAG pipeline 评测。

## 工程约束与代价

**collection.tsv 有 3.5GB，不下载全量。** 策略：先取 qrels 确定 gold pid，
再流式扫描 collection，只保留 gold + reservoir sampling 抽的干扰段落
（8 倍）。最终语料约 1.3 万段，单机可承受，且 Recall 仍有效
（gold 全在库内，干扰段落提供检索难度）。抽样比例写进报告。

**评测 collection 与生产隔离**（前缀 `eval_`）：否则生产检索会命中
T2Ranking 的段落。

**requests 在 hf-mirror 上会退化到约 12 KB/s**，curl 能跑 9 MB/s，
差三个数量级。原因是连接复用与缓冲策略，不是网络问题。
3.5GB 按 requests 速率需 70 小时以上，故大文件走 curl（`-C -` 原生续传），
requests 仅作回退。这个坑不记下来下次一定会再踩。

**断点续传的完整性判定不能依赖 HEAD 的 Content-Length**：hf-mirror 会
302 到上游，HEAD 拿到的长度与实际响应体不一致，据此判定会对已下完的
文件发 Range 请求并收到 416。改用 `.done` 标记文件，且只在正常读完
响应体后落标记。

## 回退方案

- hf-mirror 数据集通道失效 → DuReader-retrieval（百度，国内可达）
  或 mMARCO 中文子集。统一中间格式（`rag/eval/schema.py`）已隔离数据源，
  只需换 loader。
- CRUD-RAG 拉不下来 → 生成层降级为只算 faithfulness（不需要 gold answer）。
