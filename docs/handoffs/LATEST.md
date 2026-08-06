# 交接 · 2026-08-07

> 本文件每次会话覆写。历史归档为 `docs/handoffs/M{n}-YYYY-MM-DD.md`。

## 本次做了什么

建立仓库记忆骨架，执行 M0 全部七项任务的代码改造。六个提交：

```
c963a67  docs: 建立 Agent 工作契约与文档骨架
78867fc  test: 添加 scoring 模块规格测试与接口定义
7c2f051  feat: 换用中文 embedding 模型并隔离向量集合
101c9a7  feat: chunk 元数据补定位字段，支持取相邻切片
9034917  refactor: 用 RRF 替换加权融合，统一相关性口径
dc92b31  fix: 修正相关性展示口径，仪表盘数值可解释
f1672a4  feat: 添加索引重建与模型对比脚本
```

起步时还清理了两个遗留提交（`3d58037` 部署脚本健康检查路径、`fa26298` 文档切片懒加载），
它们是进入本次会话时工作区里的未提交改动，与 M0 无关，故独立落盘。

## 下一个会话要做的第一件事

**用户手动实现 `rag/scoring.py` 的两个函数**，这是刻意留给用户的部分（核心口径 + 面试必问点），不要代写。

```bash
venv/Scripts/python.exe -m pytest tests/test_scoring.py -q   # 期望 31 passed
venv/Scripts/python.exe scripts/compare_embeddings.py --top-k 3
```

实现要点已写在函数 docstring 里。三个最容易踩的：

1. 判断字段存在性不能用真值测试。`0.0` 是合法 logit（relevance 0.5），
   `if result.get("rerank_logit")` 会把它误判为缺失 —— 这正是本次要修的原始 bug。
   用 `isinstance(x, (int, float))`。
2. sigmoid 必须分支防溢出。`logit=-800` 时 `math.exp(800)` 抛 OverflowError：
   `logit >= 0` 走 `1/(1+exp(-logit))`，否则走 `e/(1+e)` where `e = exp(logit)`。
3. `rrf_fuse` 同一路内重复 id 只按最好排名计一次（每路一个 `seen` 集合），
   跨路重复正常累加。`k <= 0` 校验放在遍历之前。

## 关键事实（避免重新发现）

- **换模型的动机**：知识库与查询全中文，原 `all-MiniLM-L6-v2` 是英文模型。
  仓库里几处补丁（Multi-Query 被判"导致检索退化"、Query Rewrite 被删、
  chat_service 的 jieba 实体注入）都是在给错的 encoder 打补丁。
- **前缀已验证生效**：`python -m rag.embedder` 输出查询/文档编码同一句话的
  余弦相似度 0.887（<1.0 即两侧路径不同）。若将来该值接近 1.0，说明前缀失效。
- **仪表盘 46% 问题的根因**：`chat_service.py` 一条 `or` 链混用
  rerank_score / hybrid_score / 1-distance 三种量纲，前端再叠一层分段拉伸
  （≥0.7 乘 1.1、<0.4 乘 0.6）。两处都已删除，改为后端下发 `relevance` +
  `relevance_basis`，前端只做展示。
- **两个 collection 对比条件公平**：同一份 7 文档、各 59 chunk、同样 metadata 结构。
- **`techcorp_docs`（21 条）是换指纹前的旧库**，留着不影响任何逻辑，可随时删。

## 未完成 / 已知问题

见 `STATUS.md` 的「待办观察」。其中一条需要留意：`scripts/` 下 5 个历史调试脚本
仍引用已删除的配置项（`BM25_WEIGHT` 等），运行会 ImportError。它们不是产品代码，
但下次谁去跑会踩到。

## 环境提醒

- 国内直连 HuggingFace 不稳定。`bge-small-zh-v1.5` 已缓存；MiniLM 重建时
  重新拉 `model.safetensors` 耗了近 3 小时（几乎全在重试）。
  hf-mirror.com 首页返回 200 但模型 API 路径不通，ModelScope 可达但未接入。
- 所有 `scripts/` 入口必须把 stdout 切 UTF-8，否则 Windows GBK 控制台
  遇中文或符号即 UnicodeEncodeError。本次已因此中断过两次重建。
