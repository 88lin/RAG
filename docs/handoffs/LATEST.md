# 交接文档 — 2026-08-19

> 覆写此文件时用 `docs/handoffs/LATEST.md`，旧内容不保留。
> 事实来源：代码与 `git log`；本文件仅导航。

## 本次会话完成的工作

### T5 会话落库 + run 轨迹落库（commit `786dec3`）

验收标准 C3/C4/C5 全部满足：
- [backend/services/chat_service.py](../../backend/services/chat_service.py) 新增 `_persist_run_async`，在 `done` 事件前调用
- 一个事务写完 sessions / runs / evidence / messages 四张表
- 6 条新测试，全部通过

### T4 限流迁移 + 缓存防御（commit `374bb75`）

三个已知问题全部修复：

1. Redis 优先 + 内存降级：[backend/rate_limit.py](../../backend/rate_limit.py) 改用 INCR+EXPIRE 滑动窗口，Redis 不可用时自动切本地内存，不拒绝流量
2. 内存有界：`defaultdict` 改 `OrderedDict`，上限 10000 IP，超出 FIFO 淘汰
3. XFF 校验：只有来自 `TRUSTED_PROXY_IPS` 的 TCP 对端才采信 x-forwarded-for

[config.py](../../config.py) 新增两个配置项：`RATE_LIMIT_WINDOW_SECONDS`（默认 60）、`TRUSTED_PROXY_IPS`（默认空列表）

11 条新测试，**301 passed total**。

## 当前状态

**已完成**：T1、T2、T3、T0（五处规则漂移）、T5、T4

**待做（按顺序）**：T6 异步摄入 → M2 完成

详见 [STATUS.md](../../STATUS.md)。

## 下一格：T6 异步摄入

目标：上传 5MB PDF 立即返回 task_id，不阻塞请求。

基础设施已就绪：
- `IngestTaskRepository` — pending/running/done/error 状态机，CAS 防双认领
- `backend/cache/redis_client.py` — `hset_mapping`/`hgetall` 可用于进度写入
- `upload.py` 两个端点的所有阻塞调用已 offload（T0c）

需要做的：
1. 新建 `/api/v1/documents/upload/async` 端点，接收文件后写 `ingest_tasks` 行、把实际摄入扔进 `asyncio.create_task` / BackgroundTasks，立即返回 task_id
2. 新建 `/api/v1/documents/tasks/{task_id}` 查询端点，从 DB 读状态
3. 进度（embedding_progress 等）写 Redis hash（key = `ingest:{task_id}`，TTL = 1h），前端用 SSE 或轮询读

## T4 关键设计决策

**为什么用固定窗口（INCR+EXPIRE）而非 sorted-set 滑动窗口**：

固定窗口 2 条 Redis 命令；sorted-set 需要 ZADD + ZREMRANGEBYSCORE + ZCARD，写放大更大，TTL 管理更复杂。边界抖动（窗口末尾可能接受 2× 的请求）对这个场景可以接受。

**为什么内存降级用 FIFO 而非 LRU**：

LRU 需要每次命中时更新顺序，与限流写路径耦合。FIFO 淘汰最旧插入的 IP，对扫描器场景足够：扫描器不会持续复用同一 IP，最旧的恰好是最不活跃的。

**XFF 安全默认**：

`TRUSTED_PROXY_IPS` 默认空列表。新部署时不配置代理就不会意外信任伪造的 IP，需要时显式加入。
