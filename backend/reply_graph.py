"""聊天页智能回复：意图识别 → 多风格回复（LangGraph StateGraph）。

图只有两个节点，但把"看懂对方在干什么"和"写什么"拆开：
    START ─▶ intent_node ─▶ generate_replies_node ─▶ END

为什么先判意图再写：
- HR 说"方便发份简历吗"和"什么时候能到岗"，回复策略完全不同；
- 把意图与策略显式写进 state，第二条提示词只负责表达，生成更稳，也方便前端展示。

成本：固定 2 次模型调用（意图 1 次 + 生成 1 次），生成格式不合规会带着错误原因重试，最多 3 次。
"""

from __future__ import annotations

import time
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError, field_validator

try:  # 作为包导入（uvicorn backend.main:app）
    from .llm_service import (
        ConfigurationError,
        LLMConfig,
        LLMServiceError,
        chat_json,
        load_resume,
    )
    from .schemas import REPLY_STYLES, ReplySuggestion, SuggestReplyResponse
except ImportError:  # 从 backend/ 目录直接启动（uvicorn main:app）
    from llm_service import (  # type: ignore[no-redef]
        ConfigurationError,
        LLMConfig,
        LLMServiceError,
        chat_json,
        load_resume,
    )
    from schemas import (  # type: ignore[no-redef]
        REPLY_STYLES,
        ReplySuggestion,
        SuggestReplyResponse,
    )

MAX_HISTORY_IN_PROMPT = 15
MAX_RESUME_CHARS = 4000

INTENT_LABELS = (
    "索要简历",
    "约面试",
    "询问薪资",
    "询问到岗时间",
    "核实经历",
    "介绍岗位",
    "已查看待评估",
    "婉拒",
    "闲聊",
    "等待回复",
    "其他",
)

ROLE_LABEL = {"hr": "HR", "me": "我", "system": "系统"}

INTENT_SYSTEM_PROMPT = """你是 Boss 直聘聊天记录的分析师。任务：看懂 HR 最后一条消息的真实意图，并给候选人定一条应对策略。

只输出一个 JSON 对象，不要 Markdown，不要解释文字：
{"intent": "意图标签", "strategy": "一句话策略", "confidence": 0 到 1 的小数}

intent 只能取以下标签之一，原样输出：
索要简历 / 约面试 / 询问薪资 / 询问到岗时间 / 核实经历 / 介绍岗位 / 已查看待评估 / 婉拒 / 闲聊 / 等待回复 / 其他

判断规则：
1. 以 HR 最后一条真实发言为准。role=system 的是平台提示（如"对方已查看简历""已发送简历""对方已转为电话沟通"），只能辅助判断，不能当成 HR 说的话。
2. 如果最后一条是我发的、HR 还没回，intent 判"等待回复"。
3. 常见映射：
   - 要简历/问有没有附件简历 → 索要简历
   - 提面试、时间、地点、电话聊、加微信 → 约面试
   - 问期望薪资、目前薪资、最低接受多少 → 询问薪资
   - 问多久到岗、离职状态、能否尽快 → 询问到岗时间
   - 追问某个项目、技术栈、某段经历细节 → 核实经历
   - 只是介绍公司在招什么、团队情况 → 介绍岗位
   - 系统提示已查看/已转交用人部门，HR 没新话 → 已查看待评估
   - 明确说不合适、岗位关闭、已招满 → 婉拒
   - 只剩寒暄、表情、天气 → 闲聊
4. strategy 用一句话说清"这条回复要达成什么"，20-40 字，必须可执行。例如：
   "先确认面试时间，给两个可选时段，不主动提薪资"
   "用项目数字回应匹配度，顺手问下一步流程"
5. 不要输出分析过程，不要复述聊天记录。"""

INTENT_USER_TEMPLATE = """【岗位】
{job_line}

【简历摘要】
{resume}

【聊天记录（从旧到新，最多 15 条）】
{transcript}

【HR 最后一句】
{last_hr}

请按系统要求输出 JSON。"""

GENERATE_SYSTEM_PROMPT = """你是候选人的聊天代笔，在 Boss 直聘上回复 HR。要像本人用手机打字：短、具体、有分寸。

只输出一个 JSON 对象，不要 Markdown，不要解释文字：
{"replies": [
  {"style": "稳重礼貌", "content": "..."},
  {"style": "强化匹配", "content": "..."},
  {"style": "高情商反问", "content": "..."}
]}

三条风格固定，顺序不能变：
1. 稳重礼貌：先回应对方关注的点，再补一条与岗位相关的事实，不卑不亢。
2. 强化匹配：直接拿简历里的项目、技术栈或数字对上岗位要求，把话题推进到下一步。
3. 高情商反问：用一个具体问题把球打回去，给对方继续聊的理由。

硬性要求：
1. 每条 1-2 句话，不超过 60 个汉字（含标点），口语化，像微信聊天。超了就删字，不要用分号把三件事串成一句。
2. 严禁"贵司""深耕""期待与您共创辉煌""您好""非常荣幸""很高兴认识您""期待您的回复""希望能有机会""祝您"等套话，不要 emoji，不要营销腔。
3. 只能使用简历里真实存在的经历、技术栈和数字，禁止编造公司、项目、指标。
4. 不替候选人承诺薪资、到岗时间、入职意向；被问到就用反问或"面谈时细聊"接住。
5. 三条之间不能同义重复，切入角度要明显不同；style 必须原样写成"稳重礼貌""强化匹配""高情商反问"，不能改写或重复。
6. 不要复述整段简历，不要写"以上"这类总结句。"""

GENERATE_USER_TEMPLATE = """【岗位】
{job_line}

【HR 意图】
{intent}

【应对策略】
{strategy}

【聊天记录（从旧到新）】
{transcript}

【简历摘要】
{resume}

请按系统要求输出 3 条回复的 JSON。"""


class ReplyState(TypedDict, total=False):
    """节点之间传递的工单。"""

    job_info: dict[str, Any]
    history: list[dict[str, str]]
    resume: str
    intent: str
    strategy: str
    replies: list[dict[str, str]]


class IntentResult(BaseModel):
    """意图节点的输出。字段给默认值，模型漏字段时不至于整条链路崩掉。"""

    intent: str = "其他"
    strategy: str = ""
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class ReplyBatch(BaseModel):
    """生成节点的输出约束：恰好 3 条、风格不能重复。"""

    replies: list[ReplySuggestion] = Field(..., min_length=3, max_length=3)

    @field_validator("replies")
    @classmethod
    def _distinct(cls, items: list[ReplySuggestion]) -> list[ReplySuggestion]:
        styles = [item.style for item in items]
        if len(set(styles)) != len(styles):
            raise ValueError("3 条回复的风格不能重复")
        return items


def _log(node: str, message: str, mark: str = "·") -> None:
    """终端流转日志，与 agent_graph 同样的格式，方便一起看。"""
    print(f"[reply-graph {time.strftime('%H:%M:%S')}] {mark} {node:<21} {message}", flush=True)


def _llm_config(config: RunnableConfig) -> LLMConfig | None:
    return (config.get("configurable") or {}).get("llm_config")


def _job_line(job_info: dict[str, Any]) -> str:
    parts = [
        str(job_info.get("title") or "").strip(),
        str(job_info.get("company") or "").strip(),
        str(job_info.get("salary") or "").strip(),
    ]
    return "｜".join(part for part in parts if part) or "（未提供）"


def _transcript(history: list[dict[str, str]]) -> str:
    lines = []
    for message in history[-MAX_HISTORY_IN_PROMPT:]:
        role = ROLE_LABEL.get(message.get("role", ""), "未知")
        lines.append(f"[{role}] {message.get('content', '').strip()}")
    return "\n".join(lines) or "（无）"


def _last_hr_message(history: list[dict[str, str]]) -> str:
    for message in reversed(history):
        if message.get("role") == "hr" and message.get("content", "").strip():
            return message["content"].strip()
    return "（HR 还没有发过消息）"


def _resolve_resume(explicit: str | None) -> str:
    """请求里带了就用请求的；没带就读 resume.md。读不到不算错误，按无简历生成。"""
    text = (explicit or "").strip()
    if text:
        return text[:MAX_RESUME_CHARS]
    try:
        return load_resume()[:MAX_RESUME_CHARS]
    except ConfigurationError as exc:
        _log("graph", f"没有可用简历，按无简历生成：{exc}", "!")
        return ""


def _normalize_replies(payload: dict[str, Any]) -> list[dict[str, str]]:
    """兼容模型把 replies 写成字符串数组、或字段名写成中文的情况。"""
    raw = payload.get("replies") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []

    normalized: list[dict[str, str]] = []
    for index, item in enumerate(raw[:3]):
        default_style = REPLY_STYLES[index]
        if isinstance(item, str):
            normalized.append({"style": default_style, "content": item.strip()})
        elif isinstance(item, dict):
            style = item.get("style") or item.get("风格") or default_style
            content = item.get("content") or item.get("text") or item.get("回复") or ""
            normalized.append({"style": str(style).strip(), "content": str(content).strip()})
    return normalized


def intent_node(state: ReplyState, config: RunnableConfig) -> dict[str, Any]:
    """意图节点：判断 HR 想干什么，并给出一条应对策略。"""
    history = state.get("history") or []
    _log("intent_node", f"分析 {len(history)} 条记录，定位 HR 意图", "▶")
    started = time.perf_counter()

    payload = chat_json(
        INTENT_SYSTEM_PROMPT,
        INTENT_USER_TEMPLATE.format(
            job_line=_job_line(state.get("job_info") or {}),
            resume=state.get("resume") or "（未提供）",
            transcript=_transcript(history),
            last_hr=_last_hr_message(history),
        ),
        max_tokens=400,
        temperature=0.2,
        config=_llm_config(config),
    )

    try:
        parsed = IntentResult.model_validate(payload)
    except (ValidationError, ValueError):
        parsed = IntentResult()

    intent = parsed.intent.strip() or "其他"
    if intent not in INTENT_LABELS and not (2 <= len(intent) <= 12):
        intent = "其他"
    strategy = parsed.strategy.strip()[:120]

    _log(
        "intent_node",
        f"{intent}｜{strategy or '（无策略）'}｜{time.perf_counter() - started:.1f}s",
        "✔",
    )
    return {"intent": intent, "strategy": strategy}


def _brief_error(exc: Exception) -> str:
    """把 pydantic 的多层报错压成一行人话，方便在终端定位是哪条规则没过。"""
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors()[:3]:
            loc = ".".join(str(item) for item in err.get("loc", ()))
            parts.append(f"{loc} → {err.get('msg', '')}")
        return "；".join(parts) or str(exc)[:160]
    return " ".join(str(exc).split())[:160]


def generate_replies_node(state: ReplyState, config: RunnableConfig) -> dict[str, Any]:
    """生成节点：在意图和策略的约束下写 3 条不同风格的短回复。"""
    intent = state.get("intent") or "其他"
    _log("generate_replies_node", f"按「{intent}」写 3 条回复", "▶")
    started = time.perf_counter()

    user_prompt = GENERATE_USER_TEMPLATE.format(
        job_line=_job_line(state.get("job_info") or {}),
        intent=intent,
        strategy=state.get("strategy") or "（无，按常识应对）",
        transcript=_transcript(state.get("history") or []),
        resume=state.get("resume") or "（未提供）",
    )

    last_error: Exception | None = None
    for attempt in range(1, 4):
        prompt = user_prompt
        if last_error is not None:
            prompt += (
                f"\n\n上一次输出不合格（{last_error}）。"
                "请重新输出 3 条，每条 1-2 句、≤60 字，不要任何套话，只返回 JSON。"
            )
        payload = chat_json(
            GENERATE_SYSTEM_PROMPT,
            prompt,
            max_tokens=800,
            temperature=0.85,
            config=_llm_config(config),
        )
        try:
            batch = ReplyBatch.model_validate({"replies": _normalize_replies(payload)})
            replies = [item.model_dump() for item in batch.replies]
            _log(
                "generate_replies_node",
                f"3 条完成（第 {attempt} 次尝试）｜{time.perf_counter() - started:.1f}s",
                "✔",
            )
            return {"replies": replies}
        except (ValidationError, ValueError) as exc:
            last_error = exc
            _log("generate_replies_node", f"第 {attempt} 次不合格：{_brief_error(exc)}", "!")

    raise LLMServiceError(f"回复未通过校验（已尝试 3 次）：{last_error}")


def build_graph():
    graph = StateGraph(ReplyState)
    graph.add_node("intent_node", intent_node)
    graph.add_node("generate_replies_node", generate_replies_node)
    graph.add_edge(START, "intent_node")
    graph.add_edge("intent_node", "generate_replies_node")
    graph.add_edge("generate_replies_node", END)
    return graph.compile()


GRAPH = build_graph()


def suggest_replies(
    *,
    job_info: dict[str, Any],
    history: list[dict[str, str]],
    resume: str | None = None,
    config: LLMConfig | None = None,
) -> SuggestReplyResponse:
    """对外唯一入口：跑完两个节点，返回意图、策略与 3 条回复。"""
    resume_text = _resolve_resume(resume)
    started = time.perf_counter()
    _log(
        "graph",
        f"启动｜{job_info.get('title') or '未知岗位'}｜{len(history)} 条消息",
        "◆",
    )

    final_state = GRAPH.invoke(
        {"job_info": job_info, "history": history, "resume": resume_text},
        config={"configurable": {"llm_config": config}},
    )

    try:
        result = SuggestReplyResponse(
            intent=final_state["intent"],
            strategy=final_state.get("strategy", ""),
            replies=final_state["replies"],
        )
    except (KeyError, ValidationError, ValueError) as exc:
        raise LLMServiceError(f"回复结果结构异常：{exc}") from exc

    _log(
        "graph",
        f"结束｜意图 {result.intent}｜{len(result.replies)} 条回复"
        f"｜总耗时 {time.perf_counter() - started:.1f}s",
        "◆",
    )
    return result
