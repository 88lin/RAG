"""
生成层评测 —— 忠实度（faithfulness）

只评 faithfulness，不评 answer_correctness，原因：
  T2Ranking 是检索评测集，不提供标准答案。answer_correctness 需要
  gold answer 才能算，硬凑一个"参考答案"会让指标失去意义。
  而 faithfulness 只需 (question, answer, contexts)，恰好是本项目
  「可追溯」卖点最直接的量化 —— 答案里的每个断言是否被检索到的证据支持。

判分模型用项目当前配置的 LLM（GLM-4-Flash），走智谱的 OpenAI 兼容端点。
不用 GPT-4 判分：成本高，且本阶段目的是建立可复现的流程与基线，
不是追求判分精度的极限。

**LLM-as-judge 的结果必须人工抽检。** 本模块产出逐条明细以支持抽检，
一致率写入 docs/eval/human_check.md。不做这一步的忠实度数字不可信。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional

PROGRESS_EVERY = 8  # 每评几条打印一次进度

# 判分温度。智谱不接受 1e-8 这类科学计数法极小值
# （报 "temperature参数非法：限制小数点[2]位"），取两位小数的最小值。
JUDGE_TEMPERATURE = 0.01


def _strip_code_fence(text: str) -> str:
    """剥掉 markdown 代码块围栏，保留内部内容。

    处理 ```json\n[...]\n``` 与 ```[...]``` 两种形式。
    非代码块内容原样返回。
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text

    # 去掉起始围栏（可能带语言标注）
    body = stripped[3:]
    newline = body.find("\n")
    first_line = body[:newline] if newline != -1 else body
    # 起始行只有语言标注时整行丢弃；否则内容与围栏同行（```[...] 形式）
    if newline != -1 and first_line.strip().isalpha():
        body = body[newline + 1 :]

    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]

    return body.strip() or text


@dataclass
class GenerationSample:
    """一条生成层评测样本。

    contexts 是实际进入 prompt 的证据文本 —— 必须是真实用过的那些，
    不能用全部召回结果。否则忠实度会被稀释：答案没引用的证据
    也被算作"可支持的来源"，分数虚高。
    """

    qid: str
    question: str
    answer: str
    contexts: List[str]
    cited_files: List[str] = field(default_factory=list)
    latency_ms: float = 0.0
    error: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "GenerationSample":
        return cls(**json.loads(line))


@dataclass
class FaithfulnessResult:
    """单条的忠实度评分。"""

    qid: str
    question: str
    answer: str
    faithfulness: Optional[float]
    context_count: int
    # 落盘断言与逐条裁定，人工核验时可直接看模型怎么判的，
    # 而不是只有一个黑盒分数
    claims: List[str] = field(default_factory=list)
    verdicts: List[int] = field(default_factory=list)
    error: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "FaithfulnessResult":
        return cls(**json.loads(line))


CLAIM_PROMPT = """把下面的答案拆成若干可核查的事实断言。

什么算一条断言：
- 一个完整的、可独立判断真假的陈述句
- 条件句要合并成完整命题，例如
  「如果没有办卡但收到欠款短信，建议致电 95588 核实」算一条，
  不要拆成「如果没有办卡」和「建议致电 95588」两条

不要输出这些：
- 半句话、条件从句片段、并列成分（如「填写有误等情况导致的」）
- 关于文档本身的元陈述（如「根据文档 2 的内容」「文档中未提及」）
- 建议、免责声明、礼貌用语（如「建议咨询相关部门」）
- 对问题的复述

其他要求：
1. 保留原文的数字、名称、条件，不要概括或改写
2. 若答案里没有可核查的事实断言，输出空数组 []
3. 只输出 JSON 数组，不要代码块围栏，不要解释

问题：{question}

答案：{answer}

输出格式：["断言1", "断言2"]"""


VERDICT_PROMPT = """判断每个断言能否由给定资料推出。

资料：
{context}

断言：
{claims}

判断规则：
1. 只看资料，不用你的背景知识
2. 资料明确支持 → 1
3. 资料未提及、或与资料矛盾 → 0
4. 数字、条件、范围必须与资料一致，否则判 0
5. 只输出 JSON 数组，长度与断言数一致，不要代码块围栏

输出格式：[1, 0, 1]"""


def _parse_json_array(text: str) -> Optional[list]:
    """从 LLM 输出中解析 JSON 数组，容忍代码块围栏与前后说明文字。"""
    cleaned = _strip_code_fence(text).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        pass

    # 回退：截取第一个 [ 到最后一个 ] —— 模型常在前后加说明
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start : end + 1])
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        return None


def score_faithfulness(
    samples: List[GenerationSample],
    progress_every: int = PROGRESS_EVERY,
) -> List[FaithfulnessResult]:
    """计算忠实度：答案中的断言有多少比例能被证据支持。

    算法与 ragas 的 faithfulness 一致（抽断言 → 逐个裁定 → 取比例），
    但自己实现而不用 ragas，原因：

    1. ragas 0.1.x 的内部 prompt 是英文且要求严格 JSON，
       GLM 会把结果包在 markdown 代码块里，导致断言抽取失败
       （报 "No statements were generated"）并给 0 分。
       在 ragas 的多条调用路径上打补丁需要依赖其实现细节，脆弱且易漏。
    2. 判分逻辑透明可审：每条的断言与裁定结果都落盘，
       人工核验时能直接看到模型是怎么判的，而不是一个黑盒分数。
    3. prompt 可针对中文优化，并明确"只看资料、不用背景知识"——
       这正是忠实度要测的东西。

    衡量的是"有没有编造"，不是"答案对不对"。
    答案完全正确但超出证据范围，忠实度也应该低。

    参数:
        samples: 待评样本；error 非空或答案/证据为空的会被跳过
        progress_every: 每评几条打印一次进度

    返回:
        与输入等长的结果列表。判分失败时 faithfulness 为 None，
        不置 0 —— 那会把"判不了"和"完全不忠实"混为一谈。
    """
    from rag.llm import LLMClient

    llm = LLMClient()
    results: List[FaithfulnessResult] = []

    valid_ids = {
        s.qid for s in samples if not s.error and s.answer.strip() and s.contexts
    }

    for index, sample in enumerate(samples, start=1):
        if sample.qid not in valid_ids:
            results.append(
                FaithfulnessResult(
                    qid=sample.qid,
                    question=sample.question,
                    answer=sample.answer,
                    faithfulness=None,
                    context_count=len(sample.contexts),
                    error=sample.error or "答案或证据为空",
                )
            )
            continue

        try:
            claim_reply = llm.generate(
                CLAIM_PROMPT.format(question=sample.question, answer=sample.answer),
                system_prompt="你是严谨的信息抽取器，只输出 JSON。",
                temperature=JUDGE_TEMPERATURE,
                max_tokens=800,
            )
            claims = [
                str(c).strip()
                for c in (_parse_json_array(claim_reply) or [])
                if str(c).strip()
            ]

            if not claims:
                results.append(
                    FaithfulnessResult(
                        qid=sample.qid,
                        question=sample.question,
                        answer=sample.answer,
                        faithfulness=None,
                        context_count=len(sample.contexts),
                        error="未能从答案中抽取出断言",
                    )
                )
                continue

            numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(claims, start=1))
            context_text = "\n\n".join(
                f"[资料{i}] {c}" for i, c in enumerate(sample.contexts, start=1)
            )

            verdict_reply = llm.generate(
                VERDICT_PROMPT.format(context=context_text, claims=numbered),
                system_prompt="你是严格的事实核查器，只输出 JSON。",
                temperature=JUDGE_TEMPERATURE,
                max_tokens=400,
            )
            raw_verdicts = _parse_json_array(verdict_reply) or []

            verdicts = []
            for value in raw_verdicts[: len(claims)]:
                if isinstance(value, bool):
                    verdicts.append(1 if value else 0)
                elif isinstance(value, (int, float)):
                    verdicts.append(1 if value >= 0.5 else 0)

            if not verdicts:
                results.append(
                    FaithfulnessResult(
                        qid=sample.qid,
                        question=sample.question,
                        answer=sample.answer,
                        faithfulness=None,
                        context_count=len(sample.contexts),
                        claims=claims,
                        error="裁定结果无法解析",
                    )
                )
                continue

            results.append(
                FaithfulnessResult(
                    qid=sample.qid,
                    question=sample.question,
                    answer=sample.answer,
                    faithfulness=sum(verdicts) / len(verdicts),
                    context_count=len(sample.contexts),
                    claims=claims,
                    verdicts=verdicts,
                )
            )

        except Exception as exc:  # noqa: BLE001 - 单条失败不中断整轮
            results.append(
                FaithfulnessResult(
                    qid=sample.qid,
                    question=sample.question,
                    answer=sample.answer,
                    faithfulness=None,
                    context_count=len(sample.contexts),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

        if index % progress_every == 0 or index == len(samples):
            done = sum(1 for r in results if r.faithfulness is not None)
            print(f"  忠实度评分 {index}/{len(samples)}（成功 {done}）", flush=True)

    return results


def summarize(results: List[FaithfulnessResult]) -> Dict[str, float]:
    """汇总忠实度。

    判分失败的条目不计入均值，但要报出条数 ——
    静默丢弃会让"判不了一半"看起来和"分数很高"一样。
    """
    scored = [r.faithfulness for r in results if r.faithfulness is not None]
    if not scored:
        return {"mean": 0.0, "scored": 0, "failed": len(results)}

    ordered = sorted(scored)
    return {
        "mean": sum(scored) / len(scored),
        "median": ordered[len(ordered) // 2],
        "min": ordered[0],
        "max": ordered[-1],
        # 低于 0.8 的视为存在未被证据支持的断言，值得人工看
        "below_0.8": sum(1 for v in scored if v < 0.8) / len(scored),
        "scored": len(scored),
        "failed": len(results) - len(scored),
    }


def write_samples(path: Path, samples: List[GenerationSample]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for sample in samples:
            fh.write(sample.to_json() + "\n")
    return len(samples)


def read_samples(path: Path) -> List[GenerationSample]:
    with path.open(encoding="utf-8") as fh:
        return [GenerationSample.from_json(line) for line in fh if line.strip()]


def write_results(path: Path, results: List[FaithfulnessResult]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for item in results:
            fh.write(item.to_json() + "\n")
    return len(results)


def read_results(path: Path) -> Iterator[FaithfulnessResult]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield FaithfulnessResult.from_json(line)
