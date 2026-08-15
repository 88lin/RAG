# ADR-003 · 工具调用幂等键的键组成

**日期**：2026-08-14
**状态**：已采纳（实现推迟到 M3，本文只定形状）
**相关**：`backend/db/models.py::ToolCall`、`docs/ROADMAP.md` M3 节、
`tests/test_db.py::TestConstraints::test_idempotency_key_is_not_unique_yet`

## 背景

Tool Call 比普通 LLM 调用危险，因为它可能产生外部副作用（发邮件、创建订单、
扣费、写数据库）。常见做法是为每次调用生成幂等键，执行前检查是否已成功执行过，
已成功则返回上次结果而不重复产生副作用。

`ToolCall` 表已有 `idempotency_key` 字段，但**没有任何代码计算或写入它** ——
M2 只建了表，工具与 dispatcher 属于 M3。本 ADR 在写实现之前先把键的组成定死，
因为选错了之后改要动数据。

ROADMAP 原先写的公式是：

```
idempotency_key = sha1(tool + canonical_json(args) + kb_version)
```

## 问题

上面这个公式**不含 `run_id` 与步骤序号**，于是键在全局共享。这带来一个
语义混淆：两个不同目的被塞进了同一个字段。

| | 含 `run_id + seq` | 不含（全局共享） |
|---|---|---|
| 作用域 | 单个 run 内的某一步 | 跨所有 run |
| 防的是 | 同一步骤被**重放** | Agent **死循环**刷同一工具 |
| 副作用工具 | 正确 | **错误** |
| 只读工具 | 无法跨 run 复用结果 | 可当缓存 |

具体地说：用全局键时，用户先后两次要求"给张三发同一封邮件"会算出同一个键，
第二次被判为重复而不执行 —— 但这是两次独立的、都应当生效的请求。
反过来，用 run 内键时，Agent 在同一个 run 里对同一工具刷 50 次不会被拦住。

第二个问题是**并发下的判重根本不成立**。当前 `idempotency_key` 上只有普通索引：

```python
Index("ix_tool_calls_key", "idempotency_key")     # 非唯一
```

"查一下有没有 → 没有就执行"这个模式在两个请求同时到达时，两者都查到空、
都执行。这与 M2 移除分布式锁时的结论是同一条：**真需要唯一性时，
正确做法是数据库唯一约束，不是应用层检查。**

## 决策

**拆成两个概念，不共用一个字段。**

1. **`idempotency_key`（run 内步骤键）** —— 只用于防重放，作用域是一步：

   ```
   idempotency_key = sha1(run_id + ":" + seq + ":" + tool + ":" + canonical_json(args))
   ```

   加 `UNIQUE` 约束。执行模式改为**先插占位行再执行**：

   ```
   INSERT tool_calls(..., ok=NULL) → 冲突则说明已有人在做/做过 → 读回其结果
                                   → 成功插入则自己执行，完成后 UPDATE
   ```

   唯一约束由数据库保证，并发下只有一个插入成功，另一个拿到 IntegrityError。
   这不需要锁。

2. **`result_cache_key`（跨 run 结果缓存键）** —— 只用于复用只读工具的结果，
   放 Redis 而非 PG，带 TTL：

   ```
   ret:{tool}:{sha1(canonical_json(args))}:{kb_version}
   ```

   **只对声明为只读的工具启用**。工具 Protocol 上加 `side_effects: bool`，
   默认 `True`（保守），只读工具显式声明 `False` 才走缓存。默认值取
   保守的那一侧：漏标一个只读工具只是少一层缓存，漏标一个副作用工具
   会重复扣费。

3. **死循环兜底不靠幂等键做**，而是每个 run 的工具调用次数上限 + 同一
   `(tool, args)` 在本 run 内重复次数上限。这是编排层的控制流问题，
   与幂等是两件事，混在一起会让两边都不好推理。

`canonical_json` 指键排序、无多余空格、UTF-8 不转义的确定性序列化。
不用 Python 的 `json.dumps` 默认参数：`dict` 顺序变化会算出不同的 hash。

## 现状与迁移

本项目 M3 规划的四个工具（`search_knowledge_base`、`get_document_context`、
`calculate`、`web_search`）**全部只读、无外部副作用**。因此上文第 1 条在
M3 落地时实际防的是"崩溃恢复后重跑同一步"而非真金白银的重复扣费，
第 2 条才是当下有收益的部分。

这一点要在 README 里说清楚，不要把"我们做了 Tool Call 幂等"讲成
解决了扣费重复问题 —— 本项目没有那样的工具。**声明能力的边界本身是专业信号。**

改 `idempotency_key` 为唯一约束需要一次迁移。当前该列全表为 NULL
（没有写入方），因此加约束无需清理数据；`UNIQUE` 允许多个 NULL，
历史行不受影响。

## 被否决的方案

**在应用层加锁再判重**。已在 M2 实现过一版 Redis 分布式锁后移除
（见 `docs/plans/M2-infrastructure.md` 明确不做一节）。理由同前：
锁是性能优化，唯一性该由数据库表达。为判重引入需要理解
`SET NX PX` + Lua 释放 + 主从切换边界的机制不划算。

**只用全局键，靠 TTL 控制重复窗口**。TTL 选多长都是错的：
选短了防不住慢重试，选长了拦掉合法的重复请求。问题在于键的语义，
不在于过期时间。
