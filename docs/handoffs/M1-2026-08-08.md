# 交接 · 2026-08-08（M1 完成）

> 本文件每次会话覆写。历史归档为 `docs/handoffs/M{n}-YYYY-MM-DD.md`。

## 本次进展

M1 全部完成（T1-T6），检索层与生成层评测都有报告。
同时修了前端三个排版问题。

```
f727472  fix: 修复角标强制换行、卡片高度溢出、chunk 预览错乱
390df83  docs: 重写 README 定位，补差异化与文档格式限制
4f9e755  feat: 引用卡片细化相关性说明，修正角标过密
3a57f7c  feat: 生成层忠实度评测，M1 收尾
```

## 下一步：M2 基础设施

计划书还没写，第一件事是 `docs/plans/M2-infrastructure.md`。

要点（ROADMAP 已定，不要重新发明）：

- docker-compose 加 `postgres:16-alpine` + `redis:7-alpine`
- SQLAlchemy + Alembic 迁移
- 表结构：`runs` / `run_steps` / `tool_calls` / `evidence` / `citations`
  / `feedback` / `eval_runs` / `eval_results` / `ingest_tasks`
  加上 `users` / `documents` / `sessions` / `messages`
- **向量继续留在 ChromaDB，不引入 pgvector**
- Redis 检索缓存的 key 带 `kb_version`，文档变更时 `INCR` 一次，
  旧 key 自然失效，不必遍历删除
- 文档解析异步化：`upload → 建 task → 返回 task_id → 后台 worker →
  进度写 Redis → GET /tasks/{id}/events`。用 `asyncio.Queue` + 常驻 worker，
  不引入 Celery

验收：`docker compose up` 起 4 服务；上传 5MB PDF 立即返回不阻塞；
重启后 runs 表数据仍在。

## M1 的关键结论（不要重复实验）

**检索层**（300 条 T2Ranking，`docs/eval/report.md`）：

| variant | R@5 | MRR@10 | P50 ms |
|---|---|---|---|
| vector_minilm | 0.023 | 0.058 | 16.5 |
| vector_bge | 0.707 | 0.907 | 15.1 |
| bm25 | 0.586 | 0.841 | 42.5 |
| rrf | 0.708 | 0.902 | 52.1 |
| rrf_rerank | 0.747 | 0.947 | 9841.7 |

- 换中文 embedding 是决定性的（30 倍）
- **RRF 无增益是数据集特性，不是 bug。不要试图"修好"它。**
  报告已说明适用条件（编号/型号/专有名词类查询才需要 BM25）
- rerank 质量最好但 CPU P50 9.8 秒，M3 按路由选择性启用

**生成层**（50 条，`docs/eval/human_check.md`）：

忠实度 0.916，人工核验一致率 85%。判分器自实现，**不要改回 ragas** ——
它按 `temperature=1e-8` 调用（智谱拒绝），且英文 prompt 使 GLM 把 JSON
包在 markdown 代码块里导致断言抽取失败。三处拦截点都试过，无法覆盖全部路径。

## 环境坑（已踩过，别再踩）

- **requests 在 hf-mirror 上约 12 KB/s，curl 跑 9 MB/s**。大文件用 curl。
- **断点续传别用 HEAD 的 Content-Length 判完整性** —— 镜像 302 后长度不一致，
  会对已下完的文件发 Range 请求并收 416。用 `.done` 标记文件。
- **所有 `scripts/` 入口要把 stdout 切 UTF-8**，否则 Windows GBK 控制台
  遇中文即 UnicodeEncodeError。已因此中断过两次重建。
- **智谱拒绝 `temperature=1e-8`**（要求两位小数），但接受 `0.0` 和 `0.01`。
- Git Bash 里 curl 是原生 Windows 程序，读不到 `/tmp`；
  传含中文的 JSON body 要写文件再 `--data-binary @file`。

## 前端注意

`marked` 与 `dompurify` 是本次新增依赖，**Vite 需要重启 dev server** 才会
加载，热更新不生效。

引用角标已改为内联在 HTML 中（`<sup class="citation-marker" data-citation="n">`），
点击用事件委托。不要改回"按占位符切分 HTML"的做法 —— 角标在 `<p>`/`<li>`
内部，切分会产出未闭合标签，浏览器自动闭合后角标被挤到下一行。

## 数据现状

```
data/eval/raw/collection.tsv          3.5 GB   （gitignore，可重下）
data/eval/t2ranking_queries.jsonl     300 条
data/eval/t2ranking_corpus.jsonl      13,536 条
docs/eval/runs/*.jsonl                5 variant × 300 条 + 忠实度 50 条
```

重建：`python scripts/fetch_eval_data.py` →
`python scripts/build_eval_index.py --reset`
