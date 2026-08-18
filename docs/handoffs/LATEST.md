# 交接文档 — 2026-08-18

> 覆写此文件时用 `docs/handoffs/LATEST.md`，旧内容不保留。
> 事实来源：代码与 `git log`；本文件仅导航。

## 本次会话完成的工作

### T5 会话落库 + run 轨迹落库（commit `786dec3`）

**验收标准 C3/C4/C5 全部满足：**

- C3：一次问答结束后 `runs` 表有一行 status=ok，含 route/total_ms/first_token_ms
- C4：`evidence` 表记录全部检索结果，答案实际引用的 chunk 标记 used_in_answer=True
- C5：`messages` 表有 user/assistant 两条记录，新事务仍可读（模拟重启）

**核心改动只有两处：**

1. [backend/services/chat_service.py](../../backend/services/chat_service.py) — 新增模块级函数 `_persist_run_async`（第 26-113 行），以及在 `answer_stream` 里添加计时变量和落库调用（第 223-507 行附近）。
2. [tests/test_chat_persistence.py](../../tests/test_chat_persistence.py) — 6 条新测试。

**290 passed，`vue-tsc` exit 0。**

## 当前状态

**已完成**：T1、T2、T3、T0（五处规则漂移）、T5  
**待做（按顺序）**：T4 限流迁移 + 缓存防御 → T6 异步摄入 → M2 完成  

详见 [STATUS.md](../../STATUS.md)。

## 下一格：T4 限流迁移 + 缓存防御

涉及三个已知问题（在 STATUS.md 待办观察里有详细描述）：

1. `rate_limit.py` 内存态 → Redis（重启归零、多副本翻倍）
2. `request_history` 无界增长（每个 IP 建一个 deque 且永不清理）
3. `x-forwarded-for` 无条件信任（可被伪造绕过限流）

`backend/cache/redis_client.py` 已有，有 41 条测试；`rate_limit.py` 还是内存态。

## 关键架构决策（本次新增）

`_persist_run_async` 选择在 `done` 事件 yield **前**落库，而非 yield 后：

- yield 后落库：用户端 SSE 连接已关，任何异常都只能打日志，但落库操作的时序不可预测（`asyncio.to_thread` 的调度）
- yield 前落库：保证"用户看到 done → 数据已在库里"，且异常仍只打日志不影响 SSE

落库在一个 `session_scope()` 事务里：session → run → evidence → mark_used → messages × 2 → finish run。任何步骤失败整体回滚，不存在"run 行存在但 evidence 缺失"的半成功状态。

## 注意事项

- `_persist_run_async` 用延迟导入避免循环依赖（`session_scope` 的导入链在测试里会比 `chat_service` 先初始化）
- `ChatService` 仍保留内存中的 `ConversationManager`（用于多轮对话上下文拼装），T5 不替换它；内存态与 DB 态并存，DB 是持久化副本
- `first_token_ms=None` 存 NULL 而非 0：两者语义不同（未记录 vs 极快缓存命中）
