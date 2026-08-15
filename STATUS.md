# STATUS

> 导航文件。与代码冲突时以代码和 `git log` 为准。

## 当前位置

**阶段**：M2 基础设施 —— **进行中**（T1、T2 完成）
**下一步**：T3 Repository 层
**计划书**：`docs/plans/M2-infrastructure.md`

## M2 任务状态

| 任务 | 状态 | 实际产出 |
|---|---|---|
| T1 依赖与容器 | **完成** | compose 加 PG16 + Redis7 含健康检查；requirements 补 6 个依赖；`.env.example` 同步 |
| T2 ORM 与迁移 | **完成** | 11 张表 + Alembic 初始化 + 首个迁移；修掉 SQLite 主键与时区两处缺陷 |
| T3 Repository 层 | 未开始 | —— |
| T4 Redis + 限流迁移 | 一半 | `redis_client.py` 已有并有测试；`rate_limit.py` 仍是 `defaultdict(deque)` 内存态 |
| T5 会话落库 | 未开始 | —— |
| T6 异步摄入 | 未开始 | —— |

## 验收标准进度

| # | 标准 | 状态 |
|---|---|---|
| C1 | `docker compose up` 起 4 服务且健康检查通过 | **部分** —— `docker compose config` 通过，4 服务解析正确、`depends_on` 全为 `service_healthy`；**实际起容器未验证** |
| C2 | `alembic upgrade head` 建出全部表 | **通过** —— 11 表 + `alembic_version`，`alembic check` 无漂移，downgrade 也验过 |
| C3-C5 | runs/evidence 落库、重启后数据仍在 | 未开始（依赖 T3） |
| C6-C7 | 限流存 Redis、Redis 停掉仍放行 | 未开始（T4 后半） |
| C8-C9 | 异步上传、`kb_version` 自增 | 未开始（T6） |
| C10 | 测试通过 + 前端 `vue-tsc` 通过 | **通过** —— 170 passed；`vue-tsc --noEmit` exit 0 |

## 测试覆盖

```
tests/test_scoring.py         分数口径
tests/test_metrics.py         检索指标
tests/test_threshold.py       阈值校准
tests/test_db.py         32 条  ORM/约束/级联/事务边界/并发
tests/test_redis_client.py 41 条  降级路径（三种不可用形态）
tests/test_migrations.py   8 条  迁移与模型不漂移、升降级往返
                          ---
                          170 passed
```

DB 层与 Redis 客户端此前是零覆盖，b67a4c2 写的代码从未被执行过。
补测试当场查出两个使 DB 层在默认配置下完全不可用的缺陷（见下）。

## M2 已修掉的缺陷

两处都只在 SQLite 上出现，而 SQLite 是默认 `DATABASE_URL` 与计划书写明的
失败回退路径 —— 也就是说不是边缘情况：

1. **`BigInteger` 主键不自增**。SQLite 的隐式自增要求列类型名恰好是
   `"INTEGER"`；`BIGINT` 有整数亲和性但不是 rowid 别名，插入报
   `NOT NULL constraint failed: messages.id`。9 张表全中。
   改用 `BigInteger().with_variant(Integer, "sqlite")`，PG 上仍是 BIGINT。
2. **`DateTime(timezone=True)` 不保留时区**。SQLite 无原生时间类型，
   读回来一律 naive，`created_at > utcnow()` 在 PG 上正常、在 SQLite 上抛
   `can't compare offset-naive and offset-aware`。加 `UtcDateTime`
   TypeDecorator 在类型层抹平。

## 环境坑（本轮新踩）

- **`alembic.ini` 必须纯 ASCII**。configparser 以 `encoding="locale"` 读它，
  Windows 上即 GBK，文件里有中文在 alembic 启动前就抛 `UnicodeDecodeError`，
  报错栈指向 configparser 内部，看不出是编码问题。说明写进 `alembic/README.md`。
- **`alembic.ini` 里不能有 `timezone =` 空值**。空字符串被当作已配置，
  喂给 `ZoneInfo("")` 抛 ValueError。要留空就整行删掉。
- **`env.py` 不能用 `TextIOWrapper(sys.stdout.buffer)` 切 UTF-8**。
  它替换 `sys.stdout` 对象本身，在测试里以 API 方式调 alembic 会把 pytest
  的捕获流顶掉，第二次调用报 "I/O operation on closed file"。
  用 `stream.reconfigure(encoding="utf-8")` 就地改。
- **autogenerate 不会为自定义类型加 import**。它把类型渲染成
  `backend.db.models.JSONField()` 却不加 import，直接跑 NameError。
  这是"autogenerate 产物必须人工审阅"的典型例子。
- **对着已有表的库跑 autogenerate 会生成空迁移**。首次生成必须指向空库
  （`DATABASE_URL=... alembic revision --autogenerate`），
  否则 diff 为空而看不出问题。

## 未解决问题（移交 M3）

- **工具调用幂等尚未实现**。`ToolCall.idempotency_key` 字段存在但无任何写入方，
  且只有普通索引不是唯一约束 —— 靠它做"先查再写"判重在并发下两个请求会同时
  查空同时执行。键的组成已定，见
  [ADR-003](docs/decisions/ADR-003-tool-call-idempotency-key.md)：拆成 run 内
  步骤键（带 UNIQUE，先插占位行再执行）与跨 run 结果缓存键（Redis + TTL，
  只对声明只读的工具启用）。原 ROADMAP 那个全局键公式已废弃。
  注意本项目四个工具全部只读无外部副作用，**不要把它讲成解决了重复扣费**。
- **独立的 answerability 判断**：单一阈值已证明不可行。候选方向 ——
  LLM 生成前先判断"给定这些证据能否回答"、用 top1 与 top2 的分数差
  而非绝对值、引用校验前置。
- **rerank 可用性改造**：质量增益真实但 CPU P50 9.8 秒。
  待验证 GPU / 候选数削到 top-5 / 按路由选择性启用。
- **引用准确率与覆盖率**：依赖 M3 的 citation_verify。
- **BM25 分层验证**：T2Ranking 无 query 类型标注，
  无法验证"BM25 在关键词类查询上更强"。

## M1 结论（不要重复实验）

检索层（300 条 T2Ranking）：

| variant | R@5 | MRR@10 | nDCG@10 | P50 ms |
|---|---|---|---|---|
| vector_minilm | 0.023 | 0.058 | 0.029 | 16.5 |
| vector_bge | **0.707** | 0.907 | 0.854 | 15.1 |
| bm25 | 0.586 | 0.841 | 0.721 | 42.5 |
| rrf | 0.708 | 0.902 | 0.837 | 52.1 |
| rrf_rerank | **0.747** | **0.947** | **0.900** | 9841.7 |

1. **换中文 embedding 使 Recall@5 提升约 30 倍**（0.023 → 0.707）。
   MiniLM 的 MRR@10 仅 0.058，等同随机排序。
2. **RRF 在本数据集上无增益**（0.708 vs 0.707），延迟涨 3.5 倍。
   该数据集语义匹配为主，BM25 单独 0.586 无互补空间。
   不是实现错误，**不要试图"修好"它**。
3. **单一 relevance 阈值无法判定可答性**。有答案与无答案的 top1 分布
   严重重叠（无答案最小值 0.714 > 有答案最小值 0.697）。
   误拒率 ≤10% 时最佳阈值只能识别 19.1% 的无答案查询。

生成层（50 条）：忠实度均值 **0.916**，中位数 1.000，44 条有效 / 6 条判 None。
人工核验 20 条一致率 **85%**，不一致的 3 条全部偏紧、无偏松失效。
判分器自实现（不用 ragas，原因见报告第 7 节）。

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
- 不做分布式锁（M2 实现过一版后移除）。摄入本身幂等，
  锁是性能优化而非正确性需求；真需要唯一性时用数据库唯一约束
- 检索指标自研；生成层判分也自研（ragas 在 GLM 上跑不通）
- 项目定位为通用可追溯 RAG 研究平台，`data/documents/` 仅为 demo

## 待办观察（边界外）

- **`.env` 缺 `POSTGRES_PASSWORD`**，`docker compose up` 会直接报错退出。
  这是故意的（没有默认弱密码），但动手起容器前要先补这一行。
- `create_all` 与 Alembic 并存的漂移风险。已在 `alembic/README.md` 写明：
  `init_models()` 只用于测试，生产路径只有 `alembic upgrade head`。
  lifespan 里**没有**调 `create_all`。
- `rate_limit.py` 仍是内存态（T4 后半）。重启后限流归零，多副本配额翻倍。
- `Index("ix_runs_created_desc", "created_at")` 实际是升序索引，名字里的
  desc 名不副实。单列索引可反向扫描，功能上没问题，仅命名误导。
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
