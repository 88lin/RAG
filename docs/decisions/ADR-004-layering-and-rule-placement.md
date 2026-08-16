# ADR-004 · 分层边界与业务规则的归属

**日期**：2026-08-16
**状态**：已采纳
**相关**：`CLAUDE.md` 架构约束、`rag/`、`backend/services/`、`backend/api/`、
`backend/repositories/`、[ADR-003](ADR-003-tool-call-idempotency-key.md)

## 背景

`backend/main.py` 与 `chat_service.py` 的 docstring 写着"六边形架构"，
但代码里没有任何端口（抽象接口）定义：

```python
# chat_service.py:14 —— 直接依赖具体类
from rag import Retriever, LLMClient, VectorDB, Embedder
```

真正落实的只有 CLAUDE.md 那条约束：**`rag/` 不得 import `backend/`，
依赖方向单向**。这条是有真实收益的 —— `rag/eval/` 那套评测能脱离 FastAPI
独立跑（`scripts/run_eval.py` 直接调 `rag/`，不需要起 Web 服务）。

但缺少端口带来了可观测的后果：**规则会漂到最方便写的地方**。

## 问题：两条领域规则漂进了编排层

`chat_service.py:304-329`：

```python
from config import ANSWERABLE_MIN_RELEVANCE
has_signal = any(has_relevance_signal(r) for r in results) if results else False
...
should_use_context = top_relevance >= ANSWERABLE_MIN_RELEVANCE
```

判断"这些证据够不够支撑一个基于文档的回答"需要领域知识：relevance 的口径、
不同检索方案的分数分布、M1 实测校准出的阈值。这跟 HTTP 无关、跟 SSE 无关。

紧邻的那段更明显 —— 注释里全是领域知识（BM25 分数无界、无自然的 [0,1]
映射、实测纯 BM25 方案 Recall@5=0.586），却写在一个负责 SSE 编排的文件里。

后果是 `rag/eval/` 想复用同一个判断时得把 service 层的代码抄一遍。
这不是假想：M3 要把可答性判断纳入评测集。

成因是历史演进：M0 修分数口径时把 `scoring.py` 抽到了 `rag/`，
但可答性判断留在了原地。**没有接口强制，就只靠人自觉，而人会漂。**

## 决策一：用三类规则判定归属

新增代码时按下面的顺序问，第一个"是"决定它放哪：

### ① 领域规则 → `rag/`

**判定**：换掉交互形态（HTTP → CLI → 批处理评测脚本）后，这条规则**一字不变**吗？

领域规则回答"这个领域里什么是对的"。它不知道请求、不知道会话、不知道
有没有人在等结果。

| 在 `rag/` 的例子 | 为什么 |
|---|---|
| relevance 怎么算（`scoring.py`） | 分数口径是领域定义 |
| RRF 融合、`RETRIEVAL_MIN_RELEVANCE` | 什么算召回得动、什么该进上下文 |
| chunk 怎么切（`chunker.py`） | 文档结构的领域知识 |
| 指代消解、实体抽取（`conversation.py`） | 语言层面的处理 |
| **可答性判断（当前错放在 service）** | 需要 relevance 口径与实测阈值 |

### ② 应用编排 → `backend/services/`

**判定**：换个交互形态后，这个**顺序或流程**会变吗？

编排回答"这次请求按什么顺序做什么"，以及失败了怎么办。它调用领域规则，
但自己不含领域知识。

| 属于编排的例子 | 为什么 |
|---|---|
| 先解指代 → 再检索 → 再生成 | 顺序是应用决定的 |
| 生成失败的降级链（重试 → 小模型 → 返回证据） | 可用性策略，非领域 |
| 每步发什么进度事件 | 为了让用户看到过程 |
| 事务边界（哪些写入必须原子） | 只有业务知道 |
| 用不用引用模式 | 功能开关的编排 |

### ③ 协议适配 → `backend/api/`（驱动侧）与 `db/` `cache/` `adapters/`（被驱动侧）

**判定**：这段代码在讲某个**外部技术**的方言吗？

| 属于适配的例子 |
|---|
| SSE 帧格式 `data: {json}\n\n`、HTTP 状态码 |
| Pydantic 出入参 schema |
| SQL、`selectinload`、SAVEPOINT（`repositories/`） |
| Redis 的 key 设计与降级（`cache/`） |
| 同步转异步的线程桥接（`adapters/`、`chat_service` 的 `_sync_generator_to_async`） |

**边界情况的裁决**：一段代码同时含领域知识和协议细节时，**拆开**，
不要按"主要成分"归类。典型是 `chat_service` 那段可答性判断 ——
判断逻辑归 `rag/`，"判断结果决定发哪个 SSE 事件"归 service。

## 决策二：不做完整六边形，只在三个边界引入端口

### 三个方案的权衡

| | A · 完整六边形 | B · 现状（分层 + 依赖方向约束） | **C · 混合（采纳）** |
|---|---|---|---|
| 做法 | 每个外部依赖定义 Protocol + 适配器 | 直接依赖具体类，只约束 import 方向 | 只在确有多实现或确需替身处定义 Protocol |
| 可替换性 | 强 | 无 | 只在需要的地方有 |
| 抽象成本 | 每个端口一个 Protocol + 一层间接 | 零 | 三处 |
| 空抽象风险 | **高** —— 单实现的接口是纯噪音 | 无 | 低 |
| 规则漂移 | 被接口挡住 | **靠自觉，已经漂了** | 关键边界挡住，其余靠约定 |
| 测试替身 | 容易 | 难（要 mock 具体类） | 需要的地方容易 |

**为什么否决 A**：Python 的 `Protocol` 是结构化类型，没有 Java `implements`
那种编译期强制，抽象的约束力本就弱于静态语言。而本项目多数外部依赖**只有
一个实现且已决定不换**（ChromaDB 见 ROADMAP 已定决策）。为它们写 Protocol
得到的是"一个接口 + 一个实现"，读代码时多跳一层却没有换实现的可能 ——
这是抽象成本没有对应收益的典型。

**为什么否决 B**：规则漂移已经发生了，不是假想风险。而且 M5 要做的 CI
集成测试需要 fake LLM provider（不能在 CI 里烧真 token），没有端口就只能
靠 monkeypatch 具体类，脆且难读。

### 值得引入端口的三处

| 边界 | 为什么值得 | 时机 |
|---|---|---|
| **`LLMClient`** | 确有四个实现（openai/anthropic/zhipu/qwen），且 M5 的 CI 集成测试需要 fake provider —— **收益最大的一处** | M5 |
| **M3 的四个工具** | ROADMAP 已定要统一 Protocol + Pydantic 入参校验；dispatcher 要在一处做超时/幂等/落库/取消，没有统一签名做不到 | M3 |
| **可答性判断** | M3 要重做（单一阈值已被 M1 证否）。**它主要需要的是搬家到 `rag/`，未必需要 Protocol** —— 若最终只有一种实现，一个模块函数即可 | M3 |

### 明确不引入端口的四处

- **`VectorDB`（ChromaDB）** —— ROADMAP 已定不迁 pgvector/Milvus，
  抽象是空的
- **`Embedder`** —— 换模型是配置项（`EMBEDDING_MODEL_NAME`），
  不是实现替换
- **Repository** —— 它**本身就是被驱动侧适配器**，已经在正确的位置。
  但不为它定义 Protocol：只有一个实现，而 service 的测试**直接用真
  Repository + 临时 SQLite 更划算** —— 比手写 fake 更真实（能测出约束
  冲突、级联、事务行为），且已有 228 条测试证明这条路跑得通。
  手写 fake Repository 是"为了测试而测试"的常见陷阱：fake 与真实实现
  漂移后，测试全绿但线上炸。
- **`Retriever`** —— 检索方案的切换是参数（`enable_hybrid` /
  `enable_rerank`），不是实现替换

## 迁移方案：分三阶段，不做大爆炸重构

**原则：每一步都随该阶段本来就要做的工作一起做，不单独开重构分支。**
纯搬家的 PR 收益低、冲突多，而且搬完还是那段已知不可行的单阈值逻辑。

### 阶段 1 · 随 M3 的 answerability 重做（M3 内）

新建 `rag/answerability.py`，把 `chat_service.py:304-329` 的判断逻辑搬进去
并按 M1 的结论重做：

```
输入：检索结果列表
输出：(can_answer: bool, reason: str, signals: dict)
```

`reason` 与 `signals` 是给面板展示的 —— 拒答时要说得出为什么，
这正好对齐"禁止无声降级"。

- **验收**：`rag/eval/` 能直接调用它跑可答性评测，不经过 `backend/`
- **验收**：`chat_service` 里不再出现 `ANSWERABLE_MIN_RELEVANCE`
- **成本**：搬家部分约 1h，重做逻辑属 M3 本来的工作量

### 阶段 2 · M3 的工具 Protocol（M3 内）

```python
class Tool(Protocol):
    name: str
    args_model: type[BaseModel]
    side_effects: bool          # 默认 True，见 ADR-003
    async def run(self, args: BaseModel) -> ToolResult: ...
```

dispatcher 在一处做超时、幂等、重试、取消检查、落库、SSE 推送。
这是本项目**第一个真正的端口**。

- **验收**：加一个新工具不需要改 dispatcher
- **验收**：`calculate` 的白名单测试能不起 Web 服务直接跑

### 阶段 3 · LLM Port 与 fake provider（M5）

```python
class LLMProvider(Protocol):
    def stream(self, prompt: str, **kw) -> Iterator[str]: ...
```

`FakeLLMProvider` 返回预置文本，使 CI 能跑完整的 direct_rag / agent 链路
集成测试而不烧 token。

- **验收**：GitHub Actions 上跑通端到端测试，无真实 API 调用
- **成本**：约 2-3h

### 不做的事

- 不为 `VectorDB` / `Embedder` / `Repository` / `Retriever` 定义 Protocol
- 不把 `chat_service.py` 拆成多个文件（561 行是长，但拆分标准应该是
  职责而非行数；T5 落库后会自然减少一批会话管理代码，届时再评估）
- 不改 docstring 里"六边形架构"的说法为别的名词 —— 但要在 README 里
  写清楚实际做到的是什么。**声明能力的边界本身是专业信号**，
  这与 ADR-003 里"不要把只读工具的幂等讲成解决了重复扣费"是同一条原则。

## 后果

**正面**：新代码有明确的归属判据，不再靠"写在哪方便"。三个真正需要
替身或多实现的边界拿到端口，CI 集成测试成为可能。

**负面**：混合方案意味着**边界不一致** —— 有的地方有端口，有的地方直接
依赖。读代码的人需要知道"哪些有、为什么"，所以本 ADR 必须保持更新。
判断"值不值得加端口"仍需人的判断，没有机械规则。

**已接受的风险**：`rag/` 与 `services/` 的边界靠约定而非编译器维持。
缓解手段是本 ADR 的三条判定问题 + code review 时对照检查。
