# M2 · 基础设施：PostgreSQL + Redis + 异步摄入

**目标**：让"可追溯"落到磁盘上 —— 执行轨迹、工具调用、证据、引用
全部可按 `trace_id` 查询与聚合。
**预算**：8-10 小时
**前置**：M1 完成（提供延迟与召回基线）
**阻塞**：M3（Agent 轨迹必须能落库，内存态轨迹在面试时站不住）

## 为什么现在做

当前有两处内存态，进程重启即丢、多副本各算一套：

| 位置 | 现状 | 问题 |
|---|---|---|
| `backend/rate_limit.py` | `defaultdict(deque)` 存 IP 时间戳 | 重启后限流归零；多副本时实际配额翻倍 |
| `chat_service.sessions` | `Dict[str, ConversationManager]` | 重启丢会话；`cleanup_old_sessions` 需手动调用 |

M3 会引入 Agent 的多步执行，每步的耗时、工具入参、证据来源都要能事后
查询。这些数据放内存等于没有 —— "可追溯"这个卖点必须有持久化支撑。

## 数据流与调用链

```
                      ┌──────────────── 写路径 ────────────────┐
HTTP POST /chat/stream
   │
   ├─ RateLimitMiddleware ──► Redis: INCR rl:{ip}:{window}  (原子，带 TTL)
   │                            └─ 超限 → 429，不进业务
   │
   ├─ TraceContext 生成 trace_id (uuid4)
   │
   ├─ RunRepository.create() ──► PG: INSERT runs (trace_id, query, status='running')
   │                                   ↓ 返回 run_id
   ├─ 检索
   │   ├─ Redis GET ret:{variant}:{sha1(query)}:{kb_version}   命中则跳过检索
   │   ├─ ChromaDB 向量检索 + BM25
   │   └─ EvidenceRepository.bulk_insert() ──► PG: INSERT evidence[]
   │
   ├─ 生成（SSE 逐 token 下发）
   │
   └─ RunRepository.finish() ──► PG: UPDATE runs SET status='ok', total_ms, tokens
                                     INSERT run_steps[]  (每节点一行)

                      ┌──────────────── 读路径 ────────────────┐
GET /api/v1/runs/{trace_id}
   └─ PG: SELECT runs JOIN run_steps JOIN tool_calls JOIN evidence
          → 面板回放的数据源（M4）
```

**上传的异步链路**：

```
POST /documents/upload
   ├─ 校验类型与大小 → 写临时文件
   ├─ IngestTaskRepository.create() ──► PG: INSERT ingest_tasks (status='pending')
   ├─ asyncio.Queue.put(task_id)      ← 立即返回，不阻塞 HTTP
   └─ 200 {task_id}

后台常驻 worker（lifespan 启动）
   ├─ queue.get() → 解析 → 分块 → embedding → 入库
   ├─ 每阶段 Redis SETEX ingest:{task_id} {progress}   (前端轮询/SSE 读这里)
   ├─ Redis INCR kb_version                            (检索缓存自然失效)
   └─ PG: UPDATE ingest_tasks SET status='done', chunk_count

GET /documents/tasks/{task_id}/events  → SSE，从 Redis 读进度
```

## 表结构

```sql
-- 会话与消息（替换 chat_service 的内存字典）
sessions(id uuid pk, created_at, last_active_at, meta jsonb)
messages(id bigserial pk, session_id fk, role, content, created_at,
         run_id fk null)                      -- 关联到产生该回答的 run

-- 可追溯的核心
runs(id bigserial pk, trace_id uuid unique, session_id fk null,
     route text, query text, status text,      -- running/ok/error/cancelled
     total_ms int, first_token_ms int,
     prompt_tokens int, completion_tokens int,
     error text null, created_at, finished_at null)

run_steps(id bigserial pk, run_id fk, seq int, node text,
          ms int, state_snapshot jsonb,        -- M4 回放的数据源
          unique(run_id, seq))

tool_calls(id bigserial pk, run_id fk, seq int, tool text,
           args jsonb, result_summary text, ms int, ok bool,
           idempotency_key text,               -- sha1(tool+args+kb_version)
           unique(run_id, seq))

evidence(id bigserial pk, run_id fk, chunk_id text, file text,
         relevance real, rank int, retrieved_by text[],
         used_in_answer bool default false)

citations(id bigserial pk, message_id fk, sentence_idx int,
          chunk_id text, verified bool null, verify_score real null)

-- 反馈与评测
feedback(id bigserial pk, message_id fk, rating smallint, comment text, created_at)
eval_runs(id bigserial pk, suite text, variant text, started_at, finished_at, meta jsonb)
eval_results(id bigserial pk, eval_run_id fk, qid text, metrics jsonb)

-- 异步摄入
ingest_tasks(id uuid pk, filename text, size_bytes int, category text,
             status text, chunk_count int null, error text null,
             created_at, finished_at null)
```

索引要点：
- `runs(trace_id)` unique —— 按 trace_id 查询是主路径
- `runs(created_at desc)` —— 面板列出最近的 run
- `evidence(run_id)`、`run_steps(run_id, seq)`、`tool_calls(run_id, seq)`
- `messages(session_id, created_at)`

`state_snapshot` 与 `args` 用 `jsonb` 而非 `text`：需要按字段查询
（如"找出所有调用了 calculate 且失败的 run"）。

## Redis key 设计

| key | 类型 | TTL | 用途 |
|---|---|---|---|
| `rl:{ip}:{window}` | string(int) | 窗口长度 | 限流计数，`INCR` + `EXPIRE` |
| `kb_version` | string(int) | 无 | 知识库版本，文档变更时 `INCR` |
| `ret:{variant}:{sha1(query)}:{kb_ver}` | string(json) | 1h | 检索结果缓存 |
| `emb:{model}:{sha1(text)}` | string(json) | 24h | 查询向量缓存 |
| `ingest:{task_id}` | hash | 1h | 摄入进度 |
| `cancel:{trace_id}` | string | 10min | 取消信号，节点边界检查 |
| `lock:ingest:{filename}` | string | 30s | 同名文件并发摄入互斥 |

**`kb_version` 嵌进 key 而非用它做失效判断**：文档一变就 `INCR`，
旧 key 因前缀不匹配自然失效，不必遍历删除（`KEYS` 在生产是禁用操作，
`SCAN` 也要 O(n)）。这替换掉现在 `retriever.invalidate_bm25_cache()`
那套手工失效逻辑。

**Redis 不作为事实来源**：全部内容可丢弃后重建。
限流计数丢了最多放过一个窗口的请求；缓存丢了只是变慢。

## 分布式锁的实现与边界

同名文件并发上传会导致两个 worker 同时写同一批 chunk。用 Redis 锁互斥：

```python
# 获取：SET key token NX PX 30000
#   NX  —— 只在不存在时设置，这是互斥的来源
#   PX  —— 带过期，持有者崩溃后锁能自动释放（否则死锁）
#   token = uuid4，用于释放时校验归属

# 释放：Lua 脚本保证「比对 token」与「删除」的原子性
#   if redis.call("get", KEYS[1]) == ARGV[1] then
#       return redis.call("del", KEYS[1])
#   end
#   直接 DEL 是错的：若本进程的锁已过期并被他人获取，
#   DEL 会删掉别人的锁。
```

**必须写进文档的局限**：这是单实例 Redis 锁，主从切换时可能出现
两个持有者（主节点确认了 SET 但未同步到从节点就宕机）。
Redlock 需要多个独立 Redis 实例，本项目单实例部署，不引入。
因此该锁只用于**降低重复劳动**，不用于保证唯一性 —— 真正的唯一性
由 `ingest_tasks` 表的唯一约束和摄入逻辑的幂等（按文件名先删后插）保证。

这个区分很重要：把锁当唯一性保证是分布式系统里最常见的错误之一。

## 任务清单

### T1 · 依赖与容器（1h）

- `docker-compose.yml` 加 `postgres:16-alpine` + `redis:7-alpine`
- 依赖：`sqlalchemy[asyncio]`、`asyncpg`、`alembic`、`redis`
- `config.py` 加 `DATABASE_URL`、`REDIS_URL`，`.env.example` 同步
- 健康检查：PG 用 `pg_isready`，Redis 用 `redis-cli ping`；
  backend 的 `depends_on` 改为 `condition: service_healthy`

### T2 · ORM 模型与迁移（2h）

- `backend/db/models.py` —— 上述全部表
- `backend/db/session.py` —— async engine + sessionmaker，
  FastAPI 依赖注入提供 session
- Alembic 初始化 + 首个迁移
- **不引入 pgvector**（ROADMAP 已定）

### T3 · Repository 层（2h）

```
backend/repositories/
  runs.py       create / finish / add_step / get_by_trace
  evidence.py   bulk_insert / mark_used
  sessions.py   get_or_create / append_message / history
  ingest.py     create / update_status / get
```

Repository 而非在 service 里直接写 SQL：M4 的面板要复用同一套查询，
而 service 层已经很长（`chat_service.py` 500+ 行）。

### T4 · Redis 客户端与限流迁移（2h）

- `backend/cache/redis_client.py` —— 连接池 + 健康检查
- `backend/cache/lock.py` —— `SET NX PX` + Lua 释放
- `rate_limit.py` 改用 Redis `INCR`，**Redis 不可用时降级为放行**
  （限流是保护措施，不该因它挂掉而拒绝全部流量）
- 检索缓存接入 `kb_version`

### T5 · 会话落库（1h）

`chat_service.sessions` 改为读写 PG。历史记录从 `messages` 表加载，
内存只留当前请求的上下文。

### T6 · 异步摄入（2h）

- `POST /documents/upload` 立即返回 `task_id`
- `asyncio.Queue` + lifespan 启动的常驻 worker（不引入 Celery ——
  单机部署，Celery 要多一个 broker 和一套部署复杂度）
- 进度写 Redis，`GET /documents/tasks/{id}/events` 读
- 完成后 `INCR kb_version`

## 验收标准

| # | 标准 | 验证方式 |
|---|---|---|
| C1 | `docker compose up` 起 4 个服务且健康检查通过 | `docker compose ps` |
| C2 | `alembic upgrade head` 建出全部表 | `\dt` 输出 |
| C3 | 一次问答在 `runs` 表留下一行，含 trace_id 与耗时 | SQL 查询 |
| C4 | `evidence` 表记录本次用到的 chunk 与 relevance | SQL 查询 |
| C5 | 重启 backend 后 `runs` 数据仍在、会话历史仍可读 | 重启实测 |
| C6 | 限流计数存于 Redis，重启不归零 | `redis-cli GET rl:*` |
| C7 | Redis 停掉后请求仍能成功（降级放行） | 停容器实测 |
| C8 | 上传 5MB PDF 立即返回 task_id，不阻塞 | 计时实测 |
| C9 | 文档变更后 `kb_version` 自增，旧检索缓存不再命中 | `redis-cli GET kb_version` |
| C10 | 分布式锁单测：并发获取只有一个成功；过期后可再获取；释放时不误删他人锁 | pytest |
| C11 | 全部测试通过，前端 `vue-tsc` 通过 | 命令输出 |

## 明确不做

- 不引入 pgvector（向量留在 ChromaDB）
- 不引入 Celery（`asyncio.Queue` 够用）
- 不做 Redlock（单实例部署，且锁不承担唯一性保证）
- 不做用户系统与鉴权（M5 视情况）
- 不做 Agent 编排（M3）
- 不改前端（M4 统一改）

## 失败条件与回退

- **Docker 起不来 PG** → 退回 SQLite（`aiosqlite`），
  同一套 SQLAlchemy 模型与 Alembic 迁移，只改 `DATABASE_URL`。
  在 README 注明"当前用 SQLite，多副本需换 PG"。
- **Redis 不可用** → 限流降级放行、缓存直接穿透、锁退化为无锁
  （摄入的幂等由"按文件名先删后插"保证）。这条是设计要求，不是回退。
- **异步摄入导致进度事件丢失** → 保留现有同步 SSE 上传端点作为兜底，
  两个端点并存一个版本周期。
