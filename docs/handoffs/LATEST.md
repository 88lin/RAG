# 交接 · 2026-08-14（M2 T1+T2 完成）

> 本文件每次会话覆写。历史归档为 `docs/handoffs/M{n}-YYYY-MM-DD.md`。
> 上一版已归档到 `M1-2026-08-08.md`。

## 上次交接是错的，先说这个

上一版 LATEST.md（8-08）说"M2 计划书还没写"，STATUS.md 说 HEAD 是
`3a57f7c`、阶段 M1 完成。两者都过期：M2 计划书早已存在且完整，
M2 也已有两个提交。本次已按代码实测重写两份文档。

**教训：不要相信文档里的进度声明，先跑 `git log` 与测试。**

## 本次做了什么

M2 的 T1（依赖与容器）与 T2（ORM 与迁移）收尾，并补齐 DB 层与
Redis 客户端的测试 —— 它们此前零覆盖。

- `docker-compose.yml`：加 `postgres:16-alpine` + `redis:7-alpine`，
  健康检查用 `pg_isready` / `redis-cli ping`，backend 的 `depends_on`
  改 `condition: service_healthy`
- `requirements.txt`：补 sqlalchemy[asyncio] / alembic / asyncpg /
  aiosqlite / greenlet / redis 六个。它们此前只装在本地 venv 里没写进文件，
  等于 Docker 构建与新克隆的机器起不来
- `.env.example`：同步 `DATABASE_URL` / `POSTGRES_*` / `REDIS_*` /
  `CACHE_TTL_*` / `RATE_LIMIT_PER_MINUTE`
- Alembic：`alembic.ini` + `alembic/env.py`（异步引擎，URL 从
  `config.DATABASE_URL` 取）+ 首个迁移 `20260814_85b38f6f3e90_init_schema.py`
- `backend/main.py`：lifespan 接入 DB/Redis 探测与关闭；`/health` 报组件状态
- 测试 +81 条，总数 170

## T3 之前必读：本次修掉的两个真 bug

**b67a4c2 写的 DB 层在默认配置下一行都写不进去。** 零测试覆盖时这个状态
可以一直躺到 T3 写 Repository 才炸。两处都只在 SQLite 上出现，
而 SQLite 是默认 `DATABASE_URL`、也是计划书写明的失败回退路径：

1. **`BigInteger` 主键不自增**。SQLite 隐式自增要求列类型名恰好是
   `"INTEGER"`；`BIGINT` 有整数亲和性但不是 rowid 别名，插入报
   `NOT NULL constraint failed`。9 张表全中。已改
   `BigInteger().with_variant(Integer, "sqlite")`，PG 上仍是 BIGINT
   （轨迹表按每次问答若干行增长，INTEGER 的 21 亿上限不是安全余量）。
2. **`DateTime(timezone=True)` 不保留时区**。SQLite 无原生时间类型，
   读回来一律 naive。于是 `run.created_at > utcnow()` 在 PG 上正常、
   在 SQLite 上抛 `can't compare offset-naive and offset-aware` ——
   同一份 Repository 代码在两个库上一个能跑一个崩。已加
   `UtcDateTime(TypeDecorator)` 在类型层抹平，手法同 `JSONField`
   抹平 JSONB/JSON。

**写 T3 时注意**：这两个修复意味着模型层已经保证了「主键能自增」与
「时间戳一定 aware」，Repository 里不要再各自判一次。

## 下一步：T3 Repository 层

```
backend/repositories/
  runs.py       create / finish / add_step / get_by_trace
  evidence.py   bulk_insert / mark_used
  sessions.py   get_or_create / append_message / history
  ingest.py     create / update_status / get
```

前置条件已就绪：迁移能建表（C2 已验），`session_scope()` 的事务边界已有
测试覆盖（异常回滚、批量失败整批回滚、失败后续 scope 不受污染）。

建议顺序：先 `runs.py`，它是 C3/C4 的直接依赖，也是 M4 面板的数据源。

**不要**在 Repository 里写 `create_all` —— 建表只有
`alembic upgrade head` 一条路径。改了 `models.py` 必须生成迁移，
`tests/test_migrations.py::TestNoDrift` 会拦住漏生成的情况。

## 起容器前要做的一件事

`.env` 里**没有 `POSTGRES_PASSWORD`**，compose 会直接报错退出：

```
error while interpolating services.postgres.environment.POSTGRES_PASSWORD:
required variable POSTGRES_PASSWORD is missing a value
```

这是故意的 —— 没给默认弱密码。往 `.env` 加一行即可。
C1（`docker compose up` 起 4 服务）本次**只验到 `docker compose config`
通过**，实际起容器未验证。

## Alembic 用法与本轮踩的坑

```bash
venv/Scripts/python.exe -m alembic upgrade head                    # 建表/升级
venv/Scripts/python.exe -m alembic revision --autogenerate -m "说明"  # 改模型后
venv/Scripts/python.exe -m alembic current                          # 看版本
venv/Scripts/python.exe -m alembic downgrade -1                     # 回退一步
```

- **`alembic.ini` 必须纯 ASCII**。configparser 用 `encoding="locale"` 读，
  Windows 上即 GBK，有中文在 alembic 启动前就 `UnicodeDecodeError`，
  栈指向 configparser 内部看不出是编码问题。注释写在 `alembic/README.md`。
- **不能有 `timezone =` 空值**，空串被当已配置，`ZoneInfo("")` 抛 ValueError。
  要留空就删整行。
- **`env.py` 切 UTF-8 要用 `stream.reconfigure()`**，不能新建
  `TextIOWrapper(sys.stdout.buffer)` —— 后者替换 `sys.stdout` 对象，
  在测试里以 API 方式调 alembic 会顶掉 pytest 的捕获流，
  第二次调用报 "I/O operation on closed file"。
- **autogenerate 不会为自定义类型加 import**。它渲染出
  `backend.db.models.JSONField()` 却不加 import，直接跑 NameError。已手工补。
- **首次生成必须指向空库**：对着已有表的库跑 autogenerate 得到的是空迁移，
  而且不报错。用 `DATABASE_URL="sqlite+aiosqlite:///./tmp/x.db" ... revision --autogenerate`。

## 关于 Tool Call 幂等（本次只定形状，未实现）

`ToolCall.idempotency_key` 字段存在，但**没有任何代码写它**，
且只有普通索引不是唯一约束。M3 落地前先定了键的组成，见
[ADR-003](../decisions/ADR-003-tool-call-idempotency-key.md)。

要点：原 ROADMAP 的全局键 `sha1(tool + args + kb_version)` **已废弃** ——
它不含 `run_id`，会把"两次都该生效的相同请求"判成重复。改为拆两个键：
run 内步骤键（带 UNIQUE，先插占位行再执行，靠约束冲突判重而非"先查再写"）
+ 跨 run 结果缓存键（Redis + TTL，只对声明 `side_effects=False` 的只读工具启用）。

**本项目四个工具全部只读无外部副作用**，README 里不要把这件事讲成
解决了重复扣费/重复发邮件 —— 那样的工具本项目没有。

## 测试怎么跑

```bash
venv/Scripts/python.exe -m pytest tests/ -q          # 170 passed，约 15s
cd frontend-vue && npx vue-tsc --noEmit              # exit 0
```

`tests/conftest.py` 提供三个夹具：`sqlite_url`（临时库路径）、
`db`（建好表的库，退出时 dispose 连接池 —— 不 dispose 的话 Windows 上
临时文件被占用，pytest 清理 tmp_path 会报 PermissionError）、
`redis_disabled`（关掉 Redis 验降级）。

`tests/test_redis_client.py` 不连真实 Redis，用假客户端注入三种不可用形态：
未启用、建客户端失败、**连上过但命令抛 RedisError**。第三种最常见也最容易漏 ——
只测 `get_client() is None` 覆盖不到它。

## M1 结论（不要重复实验）

- 换中文 embedding 是决定性的：R@5 0.023 → 0.707（约 30 倍）
- **RRF 无增益是数据集特性，不是 bug。不要试图"修好"它。**
  报告已说明适用条件（编号/型号/专有名词类查询才需要 BM25）
- rerank 质量最好（MRR@10 0.947）但 CPU P50 9.8 秒，M3 按路由选择性启用
- 生成层忠实度 0.916，人工核验一致率 85%。判分器自实现，
  **不要改回 ragas** —— 它按 `temperature=1e-8` 调用（智谱拒绝），
  且英文 prompt 使 GLM 把 JSON 包在代码块里导致断言抽取失败

## 老环境坑（仍然有效）

- **requests 在 hf-mirror 上约 12 KB/s，curl 跑 9 MB/s**。大文件用 curl。
- **断点续传别用 HEAD 的 Content-Length 判完整性** —— 镜像 302 后长度不一致，
  会对已下完的文件发 Range 请求并收 416。用 `.done` 标记文件。
- **所有 `scripts/` 入口要把 stdout 切 UTF-8**（GBK 控制台遇中文即报错）。
- **智谱拒绝 `temperature=1e-8`**（要求两位小数），但接受 `0.0` 和 `0.01`。
- Git Bash 里 curl 是原生 Windows 程序，读不到 `/tmp`；
  传含中文的 JSON body 要写文件再 `--data-binary @file`。

## 数据现状

```
data/app.db                           本地 SQLite（gitignore），已有 11 张表
data/eval/raw/collection.tsv          3.5 GB（gitignore，可重下）
data/eval/t2ranking_queries.jsonl     300 条
data/eval/t2ranking_corpus.jsonl      13,536 条
docs/eval/runs/*.jsonl                5 variant × 300 条 + 忠实度 50 条
```

`data/app.db` 里的表是早先 `create_all` 建的，**没有 alembic 版本记录**
（`alembic_version` 表是本次探测时建的，值为空）。要让它进入迁移管理，
干净做法是删掉重新 `alembic upgrade head` —— 里面没有业务数据。
