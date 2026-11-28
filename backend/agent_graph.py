"""打招呼语的「生成 → 评审 → 重写」反思循环（LangGraph StateGraph）。

为什么单独成模块：
- llm_service 只负责"调一次模型"，这里只管"状态怎么流转"，职责不混；
- 对外仍是一个函数 `analyze_with_review()`，接口层不需要知道图的存在。

图结构：
    START ─▶ generate_node ─▶ review_node ─┬─ 通过 / 重写额度用完 ─▶ END
                  ▲                        │
                  └────── 不通过且还有额度 ─┘

调用成本（以 DeepSeek 计）：
- 首轮生成复用 llm_service.analyze_jd：1 次调用拿到匹配度、优势、短板、3 条招呼语；
- 每次评审 1 次调用；每次重写 1 次调用（只写 3 条招呼语，token 更省）。
- 顺利时 2 次调用；最坏（重写 2 次）6 次调用，由 retry_count >= MAX_REWRITES 强制收敛。
"""

from __future__ import annotations

import time
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError, field_validator

try:  # 作为包导入（uvicorn backend.main:app）
    from .llm_service import (
        LLMConfig,
        LLMServiceError,
        analyze_jd,
        chat_json,
        load_resume,
    )
    from .schemas import (
        BANNED_GREETING_PATTERNS,
        MAX_GREETING_LEN,
        AnalyzeResponse,
        clean_greetings,
    )
except ImportError:  # 从 backend/ 目录直接启动（uvicorn main:app）
    from llm_service import (  # type: ignore[no-redef]
        LLMConfig,
        LLMServiceError,
        analyze_jd,
        chat_json,
        load_resume,
    )
    from schemas import (  # type: ignore[no-redef]
        BANNED_GREETING_PATTERNS,
        MAX_GREETING_LEN,
        AnalyzeResponse,
        clean_greetings,
    )

# 最多重写 2 次：首稿 + 2 次重写 = 3 版，再差也得收手，避免无限循环与延迟爆炸。
MAX_REWRITES = 2

# 评审自己打的分低于这条线，即使它写了 passed=true 也按不通过处理。
PASS_SCORE = 80

REVIEW_SYSTEM_PROMPT = """你是 Boss 直聘开场白的审核员，负责给 AI 写的打招呼语打分，并给出能直接照做的修改意见。

只输出一个 JSON 对象，不要 Markdown 代码块，不要解释文字，字段严格如下：

{
  "passed": true 或 false,
  "score": 0-100 的整数,
  "ai_cliche": ["命中的套话原句", ...],
  "covered_keywords": ["文案里已经体现的 JD 硬性要求", ...],
  "missing_keywords": ["JD 要求但文案完全没提的硬性要求", ...],
  "issues": ["具体问题", ...],
  "suggestions": ["可直接执行的改法", ...]
}

判定标准（全部满足才给 passed=true）：
1. 三条都像真人打的字：有具体技术名词、项目细节或数字。出现"您好""贵司""非常感兴趣""期待您的回复""希望能有机会""很高兴认识您"这类寒暄，或通篇形容词没有事实，判不合格。
2. 三条合起来覆盖 JD 至少 2 个硬性要求（技术栈 / 业务场景 / 指标），且每条至少命中 1 个 JD 关键词。
3. 三条风格互不重复，依次是：痛点切入、结果数字、提问共创。
4. 每条不超过 60 个汉字，不堆 emoji，不喊口号。
5. 只能用【简历】里真实存在的经历与数字，编造项目或指标直接判不合格。

打分口径：80 分以上可直接发送；60-79 能用但平庸；60 以下必须重写。宁可严格，不要放水。
score 低于 80 时 passed 必须为 false，两者不能自相矛盾。
issues 与 suggestions 必须一一对应，suggestions 要写成"把 X 改成 Y"这种能直接执行的句子，禁止"建议优化表达"这类空话。"""

REVIEW_USER_TEMPLATE = """【岗位】
{job_title}

【JD 原文】
{jd_text}

【简历（用于核对文案里的事实是否真实）】
{resume}

【待评审的 3 条打招呼语】
{draft}

请按系统要求输出 JSON。"""

REWRITE_SYSTEM_PROMPT = """你是 Boss 直聘开场白写手。请根据评审意见，把打招呼语重写到能直接发送的水平。

只输出 JSON：{"greetings": ["...", "...", "..."]}

硬性要求：
1. 恰好 3 条，顺序固定为：痛点切入、结果数字、提问共创。
2. 每条不超过 60 个汉字；第一句就进入岗位或经历，禁止"您好""贵司""非常感兴趣""期待您的回复""希望能有机会""很高兴认识您"等任何寒暄。
3. 逐条解决【评审意见】里的问题，并覆盖【必须命中】里的关键词。
4. 只能用【简历】里真实存在的经历和数字；没有数字就写技术细节，禁止编造公司、项目、指标。
5. 简体中文，不要 emoji，不要形容词堆砌，不要营销腔。"""

REWRITE_USER_TEMPLATE = """【岗位】
{job_title}

【JD 原文】
{jd_text}

【简历】
{resume}

【上一版（不能照抄，要针对问题改）】
{draft}

【上一轮评审意见】
{issues}

【必须命中】
{keywords}

请重写 3 条并输出 JSON。"""


class GreetingState(TypedDict, total=False):
    """在节点之间流转的状态。每个节点只返回自己改动的字段。"""

    job_title: str
    jd_text: str
    resume: str
    analysis: dict[str, Any]  # 首轮完整分析结果（match_score / pros / gaps）
    draft: list[str]  # 当前 3 条招呼语
    review: dict[str, Any]  # 最近一次评审结论
    retry_count: int  # 已经重写过几次
    rewrite_error: str  # 重写失败的原因（有值表示降级保留上一版）


class GreetingDraft(BaseModel):
    """重写节点的输出约束：必须恰好 3 条，且过与接口相同的清洗规则。"""

    greetings: list[str] = Field(..., min_length=3, max_length=3)

    @field_validator("greetings")
    @classmethod
    def _check(cls, items: list[str]) -> list[str]:
        return clean_greetings(items)


class ReviewResult(BaseModel):
    """评审节点的输出。字段给默认值，模型漏字段时不至于整条链路崩掉。"""

    passed: bool = False
    score: int = Field(0, ge=0, le=100)
    ai_cliche: list[str] = Field(default_factory=list)
    covered_keywords: list[str] = Field(default_factory=list)
    missing_keywords: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


def _log(node: str, message: str, mark: str = "·") -> None:
    """终端流转日志。用 print 而非 logging，避免被 uvicorn 的日志配置吞掉。"""
    print(f"[agent-graph {time.strftime('%H:%M:%S')}] {mark} {node:<13} {message}", flush=True)


def _llm_config(config: RunnableConfig) -> LLMConfig | None:
    return (config.get("configurable") or {}).get("llm_config")


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {text}" for i, text in enumerate(items, start=1)) or "（无）"


def _bullets(items: list[str] | None) -> str:
    lines = [f"- {item}" for item in (items or []) if str(item).strip()]
    return "\n".join(lines) or "（评审未给出）"


def _hard_check(draft: list[str]) -> tuple[list[str], list[str], list[str]]:
    """确定性检查：条数、字数、套话、重复。这些不必花一次模型调用。

    返回 (issues, suggestions, cliche_hits)。
    """
    issues: list[str] = []
    suggestions: list[str] = []
    cliche: list[str] = []

    if len(draft) != 3:
        issues.append(f"条数为 {len(draft)}，必须恰好 3 条")
        suggestions.append("补齐或删减到 3 条，依次为痛点切入、结果数字、提问共创")

    seen: set[str] = set()
    for index, raw in enumerate(draft, start=1):
        text = (raw or "").strip()
        if not text:
            issues.append(f"第 {index} 条为空")
            suggestions.append(f"重写第 {index} 条，写一句具体的项目经历")
            continue
        if len(text) > MAX_GREETING_LEN:
            issues.append(f"第 {index} 条 {len(text)} 字，超过 {MAX_GREETING_LEN} 字上限")
            suggestions.append(f"第 {index} 条砍掉修饰词，压到 {MAX_GREETING_LEN} 字以内")
        for pattern in BANNED_GREETING_PATTERNS:
            if pattern in text:
                issues.append(f"第 {index} 条出现寒暄套话「{pattern}」")
                suggestions.append(f"第 {index} 条删掉「{pattern}」，第一句直接说岗位或经历")
                if pattern not in cliche:
                    cliche.append(pattern)
        if text in seen:
            issues.append(f"第 {index} 条与前面的内容重复")
            suggestions.append(f"第 {index} 条换一个切入角度，别和上一版同义")
        seen.add(text)

    return issues, suggestions, cliche


def _rewrite_greetings(
    *,
    job_title: str,
    jd_text: str,
    resume: str,
    previous: list[str],
    review: dict[str, Any],
    config: LLMConfig | None,
) -> list[str]:
    """按评审意见重写 3 条招呼语。格式不合规时带错误原因再要一次。"""
    user_prompt = REWRITE_USER_TEMPLATE.format(
        job_title=job_title,
        jd_text=jd_text,
        resume=resume,
        draft=_numbered(previous),
        issues=_bullets(review.get("issues")) + "\n" + _bullets(review.get("suggestions")),
        keywords="、".join(review.get("missing_keywords") or []) or "（评审未指定，按 JD 自选 2 个硬性要求）",
    )

    last_error: Exception | None = None
    for _ in range(2):
        prompt = user_prompt
        if last_error is not None:
            prompt += f"\n\n上一次输出不合格（{last_error}）。请严格输出 3 条、每条 ≤60 字，只返回 JSON。"
        payload = chat_json(
            REWRITE_SYSTEM_PROMPT,
            prompt,
            max_tokens=700,
            temperature=0.85,
            config=config,
        )
        try:
            return GreetingDraft.model_validate(payload).greetings
        except (ValidationError, ValueError) as exc:
            last_error = exc

    raise LLMServiceError(f"重写结果未通过校验：{last_error}")


def generate_node(state: GreetingState, config: RunnableConfig) -> dict[str, Any]:
    """生成节点：首轮出完整分析，后续轮只按评审意见重写招呼语。"""
    llm_config = _llm_config(config)
    draft = state.get("draft") or []

    if not draft:
        _log("generate_node", "第 1 轮｜完整分析（匹配度 / 优势 / 短板 / 3 条招呼语，1 次调用）", "▶")
        started = time.perf_counter()
        result = analyze_jd(
            state["job_title"],
            state["jd_text"],
            config=llm_config,
            resume=state.get("resume"),
        )
        payload = result.model_dump()
        greetings = payload.pop("greetings")
        _log(
            "generate_node",
            f"首稿完成：{len(greetings)} 条｜{time.perf_counter() - started:.1f}s",
            "✔",
        )
        return {"analysis": payload, "draft": greetings, "retry_count": 0}

    retry = state.get("retry_count", 0) + 1
    review = state.get("review") or {}
    _log(
        "generate_node",
        f"第 {retry + 1} 轮｜按 {len(review.get('suggestions') or [])} 条意见重写（只写招呼语）",
        "▶",
    )
    started = time.perf_counter()
    try:
        new_draft = _rewrite_greetings(
            job_title=state["job_title"],
            jd_text=state["jd_text"],
            resume=state.get("resume", ""),
            previous=draft,
            review=review,
            config=llm_config,
        )
    except LLMServiceError as exc:
        # 已经有能用的上一版，不能因为"打磨失败"让用户整单丢掉分析结果
        _log("generate_node", f"重写失败，保留上一版：{exc}", "✖")
        return {"retry_count": retry, "rewrite_error": str(exc)}

    _log(
        "generate_node",
        f"第 {retry + 1} 版完成｜{time.perf_counter() - started:.1f}s",
        "✔",
    )
    return {"draft": new_draft, "retry_count": retry}


def review_node(state: GreetingState, config: RunnableConfig) -> dict[str, Any]:
    """评审节点：先跑确定性规则，再让模型判套话与关键词覆盖。"""
    if state.get("rewrite_error"):
        # 重写已经失败，不再浪费一次评审调用，直接沿用上一轮结论收尾
        return {"review": state.get("review") or {"passed": False}}

    draft = state.get("draft") or []
    _log("review_node", "开始评审（套话 / JD 关键词 / 风格 / 字数）", "▶")

    hard_issues, hard_suggestions, cliche = _hard_check(draft)
    if hard_issues:
        _log("review_node", f"硬性规则未过：{hard_issues[0]}（跳过模型评审）", "✖")
        return {
            "review": {
                "passed": False,
                "score": 0,
                "ai_cliche": cliche,
                "covered_keywords": [],
                "missing_keywords": [],
                "issues": hard_issues,
                "suggestions": hard_suggestions,
            }
        }

    user_prompt = REVIEW_USER_TEMPLATE.format(
        job_title=state["job_title"],
        jd_text=state["jd_text"],
        resume=state.get("resume", ""),
        draft=_numbered(draft),
    )

    started = time.perf_counter()
    try:
        payload = chat_json(
            REVIEW_SYSTEM_PROMPT,
            user_prompt,
            max_tokens=800,
            temperature=0.2,
            config=_llm_config(config),
        )
        if "passed" not in payload and "pass" in payload:
            payload["passed"] = payload.pop("pass")
        review = ReviewResult.model_validate(payload).model_dump()
    except (LLMServiceError, ValidationError, ValueError, TypeError) as exc:
        # 评审本身挂了不该拖垮整次分析：降级为通过，但日志留痕
        _log("review_node", f"评审调用失败，降级放行：{exc}", "!")
        return {
            "review": {
                "passed": True,
                "score": 0,
                "ai_cliche": [],
                "missing_keywords": [],
                "issues": [],
                "suggestions": [],
                "degraded": True,
                "error": str(exc),
            }
        }

    # 模型偶尔会"嘴上通过、分数很低"，用分数线兜住，保证判定自洽
    if review["passed"] and review["score"] < PASS_SCORE:
        _log(
            "review_node",
            f"模型判了通过但只有 {review['score']} 分（低于 {PASS_SCORE}），按不通过处理",
            "!",
        )
        review["passed"] = False

    verdict = "通过" if review["passed"] else "不通过"
    _log(
        "review_node",
        f"{verdict}｜score={review['score']}｜套话 {len(review['ai_cliche'])} 处"
        f"｜覆盖 {len(review.get('covered_keywords') or [])} 个硬要求"
        f"｜未覆盖 {len(review['missing_keywords'])} 个｜{time.perf_counter() - started:.1f}s",
        "✔" if review["passed"] else "✖",
    )
    if not review["passed"] and review["issues"]:
        _log("review_node", f"首要问题：{review['issues'][0]}")

    return {"review": review}


def route_after_review(state: GreetingState) -> str:
    """条件边：通过或额度用完就结束，否则打回生成节点。"""
    review = state.get("review") or {}
    retry = state.get("retry_count", 0)

    if review.get("passed"):
        _log("条件边", f"评审通过（score={review.get('score')}），输出最终稿", "→")
        return END
    if state.get("rewrite_error"):
        _log("条件边", "重写失败，带上一版收尾", "→")
        return END
    if retry >= MAX_REWRITES:
        _log("条件边", f"已重写 {retry} 次仍未通过，按上限收尾（保留最后一版）", "→")
        return END

    _log("条件边", f"打回重写｜retry_count={retry}/{MAX_REWRITES}", "↺")
    return "generate_node"


def build_graph():
    graph = StateGraph(GreetingState)
    graph.add_node("generate_node", generate_node)
    graph.add_node("review_node", review_node)
    graph.add_edge(START, "generate_node")
    graph.add_edge("generate_node", "review_node")
    graph.add_conditional_edges(
        "review_node",
        route_after_review,
        {"generate_node": "generate_node", END: END},
    )
    return graph.compile()


GRAPH = build_graph()


def analyze_with_review(
    job_title: str, jd_text: str, config: LLMConfig | None = None
) -> AnalyzeResponse:
    """对外唯一入口：跑完反思循环，返回与改造前完全一致的 AnalyzeResponse。"""
    resume = load_resume()
    started = time.perf_counter()
    _log("graph", f"启动｜{job_title}｜重写上限 {MAX_REWRITES} 次", "◆")

    final_state = GRAPH.invoke(
        {
            "job_title": job_title,
            "jd_text": jd_text,
            "resume": resume,
            "retry_count": 0,
        },
        config={"configurable": {"llm_config": config}},
    )

    analysis = final_state.get("analysis") or {}
    draft = final_state.get("draft") or []
    review = final_state.get("review") or {}
    if not analysis or not draft:
        raise LLMServiceError("反思循环没有产出有效结果，请检查模型返回。")

    # 最后再过一次与接口相同的校验，防止任何路径漏出不合规文案
    try:
        result = AnalyzeResponse(
            match_score=analysis["match_score"],
            pros=analysis["pros"],
            gaps=analysis["gaps"],
            greetings=draft,
        )
    except ValueError as exc:  # 含 pydantic.ValidationError，映射成 502 而不是 500
        raise LLMServiceError(f"最终结果未通过校验：{exc}") from exc

    _log(
        "graph",
        f"结束｜重写 {final_state.get('retry_count', 0)} 次"
        f"｜最终 {'通过' if review.get('passed') else '未通过'}"
        f"｜总耗时 {time.perf_counter() - started:.1f}s",
        "◆",
    )
    return result
