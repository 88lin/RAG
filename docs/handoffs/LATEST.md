# 交接 · 2026-08-07（M1 检索层完成）

> 本文件每次会话覆写。历史归档为 `docs/handoffs/M{n}-YYYY-MM-DD.md`。

## 本次进展

M0 全部完成并端到端验证；M1 的 T1/T2/T3 完成，检索层报告已出。

```
c3f8ecc  feat: 实现相关性计算与 RRF 融合
95fbc4d  docs: M0 完成，记录换模型实测收益与遗留问题
5e2c72b  feat: 接入 T2Ranking 评测集与检索指标实现
078e4fe  feat: 检索实验 runner 与五方案定义
3eedab4  docs: 检索方案评测报告（300 条 T2Ranking）
```

## 立即要做的第一件事

检查 `rrf_rerank` 是否补跑完：

```bash
wc -l docs/eval/runs/rrf_rerank.jsonl        # 目标 300
python scripts/run_eval.py score              # 重算并更新报告表格
```

若进程已死（后台任务可能随会话结束终止），重跑：

```bash
python scripts/run_eval.py run --variant rrf_rerank
```

约 2 秒/条，300 条需 100 分钟。跑完后把 `docs/eval/report.md` 第 2 节
表格里 rrf_rerank 的 `n=24*` 更新为 300，并删掉那条脚注。

## 下一步：M1 T5 与 T4

**T5 无答案阈值校准**（2h，建议先做，它是 M0 的遗留问题）：
从 T2Ranking 构造无答案子集 —— 取 query 但把其 gold pid 全部从语料中排除，
扫描阈值 0.3~0.9，画无答案识别率与误拒率两条曲线，取拐点作为
`ANSWERABLE_MIN_RELEVANCE` 的新默认值。M0 已证明 0.50 偏低。

**T4 生成层评测**（3-4h）：接 Ragas 算 faithfulness / answer_relevancy /
answer_correctness，必须人工抽检 20-30 条并把一致率写进
`docs/eval/human_check.md`。

## 关键事实（避免重新发现或重复实验）

- **换 embedding 模型使 Recall@5 从 0.023 到 0.707（约 30 倍）。**
  MiniLM 在 13,536 段中文语料上 MRR@10 仅 0.058，等同随机排序。
  这是整个项目单点收益最高的改动。
- **RRF 在 T2Ranking 上没有增益**（0.708 vs 0.707），延迟涨 3.5 倍。
  不是实现错误，是数据集特性：query 以语义匹配为主，BM25 单独只有 0.586。
  报告已如实记录并说明适用条件。**不要试图"修好"这个结果。**
- **rerank CPU 延迟 P50 18.9 秒**，其中 rerank 阶段占 17.7 秒。
  质量增益真实（R@5 0.708→0.770）但不可线上。
- **requests 在 hf-mirror 上约 12 KB/s，curl 能跑 9 MB/s（差 700 倍）。**
  不是网络问题，是连接复用与缓冲策略。大文件一律用 curl。
- **断点续传不能用 HEAD 的 Content-Length 判完整性** —— hf-mirror 会 302
  到上游，长度不一致会导致对已下完文件发 Range 请求并收 416。
  改用 `.done` 标记文件。
- 评测语料 collection 前缀 `eval_`，与生产隔离。混在一起会让生产检索
  命中 T2Ranking 段落。

## 数据现状

```
data/eval/raw/collection.tsv          3.5 GB   （gitignore，可重下）
data/eval/t2ranking_queries.jsonl     300 条
data/eval/t2ranking_corpus.jsonl      13,536 条（1504 gold + 12032 干扰）
docs/eval/runs/*.jsonl                4 个满 300 条，rrf_rerank 补跑中
```

原始数据与运行记录已加入 `.gitignore`（体积大，可由脚本重建）。
重建命令：`python scripts/fetch_eval_data.py` → `python scripts/build_eval_index.py --reset`

## 尚未推送

九个 M0 提交 + 五个 M1 提交都还在本地 `main`。
用户仓库是 `https://github.com/xiyue188/RAG.git`，尚未确认推送方式。

## 环境提醒

- 所有 `scripts/` 入口必须把 stdout 切 UTF-8（Windows GBK 会抛
  UnicodeEncodeError）。已成为惯例，新脚本照抄。
- Git Bash 里 curl 是原生 Windows 程序，读不到 `/tmp` 这类虚拟路径，
  测 API 时把临时文件放项目内。
- 用 curl 传含中文的 JSON body 要写文件再 `--data-binary @file`，
  单引号内联会被 shell 破坏编码。
