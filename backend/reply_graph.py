"""聊天页智能回复：意图识别 → 条件路由 → 差异化生成（LangGraph StateGraph）。

图结构：
    START ─▶ intent_node ─┬─ PENDING_REVIEW ──▶ generate_hold_replies_node ──────┐
                          ├─ SCREENING ───────▶ generate_screening_replies_node ─┤
                          ├─ INTERVIEW_INVITE ▶ generate_interview_replies_node ─┼─▶ END
                          └─ 其余三类 ────────▶ generate_etiquette_replies_node ─┘

为什么先判意图再分流：
- 「等通知」和「期望薪资多少」要的是完全相反的回复：前者越短越好，后者要进退有度；
- 把 6 类意图收敛成 4 条分支，每条分支一套固定风格与硬约束，提示词更短、输出更稳；
- 意图、潜台词、策略都落在 state 里，前端可以先讲清局势，再让人选话术。

成本：固定 2 次模型调用（意图 1 次 + 生成 1 次）。生成不合规会带着错误原因重试，最多 3 次；
意图输出无法解析时按关键词兜底，不额外调用模型。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
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
    from .schemas import (
        INTENT_LABELS,
        ReplySuggestion,
        SuggestReplyResponse,
    )
except ImportError:  # 从 backend/ 目录直接启动（uvicorn main:app）
    from llm_service import (  # type: ignore[no-redef]
        ConfigurationError,
        LLMConfig,
        LLMServiceError,
        chat_json,
        load_resume,
    )
    from schemas import (  # type: ignore[no-redef]
        INTENT_LABELS,
        ReplySuggestion,
        SuggestReplyResponse,
    )

MAX_HISTORY_IN_PROMPT = 15
MAX_RESUME_CHARS = 4000
# 判意图只需要判断"对方在干什么"，不需要通读简历，截短省钱也更聚焦
INTENT_RESUME_CHARS = 1200

ROLE_LABEL = {"hr": "HR", "me": "我", "system": "系统"}


@dataclass(frozen=True)
class IntentPlaybook:
    """一类意图对应的一套生成约束。

    styles 与 style_hints 一一对应：风格名会原样出现在前端标签上，
    所以模型写歪时按位置回填，保证三条标签始终是预期的三个。
    """

    code: str
    branch: str
    styles: tuple[str, str, str]
    style_hints: tuple[str, str, str]
    guidance: str
    analysis_default: str
    max_chars: int
    max_questions: int

    @property
    def label(self) -> str:
        return INTENT_LABELS[self.code]


# 6 类意图 → 4 条生成分支。分支划分依据是"回复要做的事"：
#   hold      评估挂起：短、收尾、不纠缠
#   screening 探薪资/背景：给范围、谈价值、先聊岗位
#   interview 约面试：确认时间、问形式
#   etiquette 要材料 / 已婉拒 / 日常：按职场礼仪自然回应
INTENT_PLAYBOOKS: dict[str, IntentPlaybook] = {
    "REQUEST_INFO": IntentPlaybook(
        code="REQUEST_INFO",
        branch="etiquette",
        styles=("立刻给到", "给到+补匹配点", "给到+确认下一步"),
        style_hints=(
            "一句话答应并说明什么时候给，不铺垫、不解释原因",
            "答应之余，用简历里一个具体项目或数字对上岗要求",
            "答应并顺势问对方接下来看什么、大概什么时候有反馈",
        ),
        guidance=(
            "对方在要简历 / 作品集 / 联系方式，核心是让他立刻拿到东西。"
            "不要反问为什么要、不要先谈薪资、不要说「稍后整理」这类拖延话；"
            "联系方式只在对方明确索要时才给。"
        ),
        analysis_default="在收集材料，属于推进信号，给得越快越容易进初筛。",
        max_chars=55,
        max_questions=2,
    ),
    "PENDING_REVIEW": IntentPlaybook(
        code="PENDING_REVIEW",
        branch="hold",
        styles=("礼貌收尾", "留个钩子", "轻问进度"),
        style_hints=(
            "一句话收住，表示等消息，不加任何追问",
            "收尾时补一句与岗位相关的具体能力，给对方一个记住你的理由（唯一允许轻微推进的一条）",
            "礼貌问一个时间点（大概多久有反馈），只问一次",
        ),
        guidance=(
            "对方在走内部流程或让你等通知。三条都要短，绝不长篇大论、不表忠心、"
            "不追问「我哪里不合适」；除「留个钩子」「轻问进度」外不要主动推销自己。"
        ),
        analysis_default="标准流程话术，大概率在走内部初筛，此时追问会显得急。",
        max_chars=40,
        max_questions=1,
    ),
    "SCREENING": IntentPlaybook(
        code="SCREENING",
        branch="screening",
        styles=("锚定区间反问预算", "能力对价", "先聊岗位再谈薪"),
        style_hints=(
            "给一个区间（不高于市场、不低于底线），紧接着反问对方这个岗位的预算区间",
            "把数字挂到具体能力和成果上，说明区间依据，再问对方更看重哪块",
            "不报数字，把话题拉回岗位职责与匹配度，约到面谈再谈钱",
        ),
        guidance=(
            "对方在探你的价格、到岗时间或经历细节。不要编造当前薪资，不要一次给精确数字，"
            "不要承诺具体入职日期（可以说「最快两周内」这类区间）；"
            "被问离职原因时一句话带过，不抱怨前公司。"
        ),
        analysis_default="在摸你的价格和底线，先给范围再反问，别急着交底。",
        max_chars=60,
        max_questions=3,
    ),
    "INTERVIEW_INVITE": IntentPlaybook(
        code="INTERVIEW_INVITE",
        branch="interview",
        styles=("确认时间+问形式", "给两个备选时段", "先应下再问细节"),
        style_hints=(
            "直接答应并确认时间，同时问清线上还是线下、几轮、用什么工具",
            "给出两个具体可面试的时段让对方挑，减少来回",
            "先明确表示可以参加，再问面试流程、时长和要准备什么",
        ),
        guidance=(
            "对方已经推进到面试，回复要干脆，别在这时候谈薪资或反复确认岗位。"
            "时间要给具体时段（如「周三下午两点后都行」），不要说「都可以」「随时」；"
            "一轮回复里最多问两个问题。"
        ),
        analysis_default="已经过了初筛，接下来只看时间能不能对上，回复要干脆。",
        max_chars=60,
        max_questions=2,
    ),
    "REJECTED": IntentPlaybook(
        code="REJECTED",
        branch="etiquette",
        styles=("体面收尾", "留个后手", "轻问方向"),
        style_hints=(
            "一句话致谢并结束，不辩解、不追问原因",
            "致谢后留一句「后续有合适岗位可以再联系」，保持体面",
            "只问一句以后往哪个方向准备更合适，语气要轻，不能变成申诉",
        ),
        guidance=(
            "对方已经明确拒绝或岗位关闭。三条都要克制：不解释、不反驳、不列举自己的优势、"
            "不问「为什么」；「轻问方向」也只能问一句建议，不能变成申诉。"
        ),
        analysis_default="流程性婉拒，纠缠没有收益，留个印象比争一句更重要。",
        max_chars=40,
        max_questions=1,
    ),
    "GENERAL": IntentPlaybook(
        code="GENERAL",
        branch="etiquette",
        styles=("顺着聊", "补一条信息", "轻推进"),
        style_hints=(
            "接住对方的话，回一句自然回应，不硬转话题",
            "顺着话题补一个与岗位相关的事实，让对方多知道一点",
            "自然把话题往下一步带（问流程、问团队、问接下来做什么）",
        ),
        guidance=(
            "对方只是寒暄或泛泛而谈，没有明确诉求。保持对话温度即可："
            "不长篇自我介绍、不突然推销、不连续发问，三条都要像随口回的。"
        ),
        analysis_default="只是维持对话温度，不用急着证明自己。",
        max_chars=60,
        max_questions=1,
    ),
}

# 分支名 → 图谱节点名。路由函数直接返回节点名，日志里能看出走了哪条分支。
BRANCH_NODES: dict[str, str] = {
    "hold": "generate_hold_replies_node",
    "screening": "generate_screening_replies_node",
    "interview": "generate_interview_replies_node",
    "etiquette": "generate_etiquette_replies_node",
}

# 模型偶尔会把 6 类代号写成中文，这里做一层兜底映射（旧版标签也兼容）
INTENT_ALIASES: dict[str, str] = {
    "索要简历": "REQUEST_INFO",
    "要简历": "REQUEST_INFO",
    "要作品集": "REQUEST_INFO",
    "要联系方式": "REQUEST_INFO",
    "已查看待评估": "PENDING_REVIEW",
    "待评估": "PENDING_REVIEW",
    "等待回复": "PENDING_REVIEW",
    "已收简历": "PENDING_REVIEW",
    "询问薪资": "SCREENING",
    "询问到岗时间": "SCREENING",
    "核实经历": "SCREENING",
    "约面试": "INTERVIEW_INVITE",
    "婉拒": "REJECTED",
    "拒绝": "REJECTED",
    "闲聊": "GENERAL",
    "其他": "GENERAL",
    "介绍岗位": "GENERAL",
}

INTENT_SYSTEM_PROMPT = """你是 Boss 直聘聊天记录的分析师。任务：判断这段对话里 HR 走到了哪一步，并拆出潜台词、给出应对策略。

只输出一个 JSON 对象，不要 Markdown，不要解释文字：
{"intent": "意图代号", "confidence": "高", "analysis": "一句话潜台词", "strategy": "一句话策略"}

intent 只能取以下 6 个代号之一，必须原样输出英文：
REQUEST_INFO：要简历、要作品集、要联系方式（微信/电话）
PENDING_REVIEW：已收简历、转交用人部门评估、让等通知（挂起态）
SCREENING：探询期望薪资、到岗时间、技术栈细节、离职原因
INTERVIEW_INVITE：约面试时间、发面试链接、问方便面试的时段
REJECTED：委婉拒绝、岗位已招满、不匹配
GENERAL：其他日常沟通或简单寒暄

判断规则：
1. 以 HR 最后一条真实发言为准。role=system 的是平台提示（如"对方已查看附件简历""已发送简历""对方已转电话沟通"），只能辅助判断，不能当成 HR 说的话。
2. 平台显示"已查看 / 已转交 / 评估中"、HR 又没有新问题时，判 PENDING_REVIEW，不要判 GENERAL。
3. 最后一条是我发的、HR 还没回，判 PENDING_REVIEW（挂起等待），confidence 给"中"。
4. 一句话里出现多个诉求时，按"下一步动作"只取一个，优先级：
   REJECTED > REQUEST_INFO > INTERVIEW_INVITE > SCREENING > PENDING_REVIEW > GENERAL。
   例："方便发份简历吗？另外什么时候能到岗" → REQUEST_INFO。
5. 常见信号：
   - "发份简历 / 作品集""加个微信""留个电话" → REQUEST_INFO
   - "简历收到了""转给业务部门""等通知""有消息联系你" → PENDING_REVIEW
   - "期望薪资多少""目前多少""多久能到岗""为什么离职""用过 XX 吗" → SCREENING
   - "什么时候方便面""发你个面试链接""明天下午有空吗" → INTERVIEW_INVITE
   - "不太合适""岗位已经关了""招满了""方向不匹配" → REJECTED
   - 表情、寒暄、天气、泛泛的"在吗" → GENERAL
6. confidence 只能是"高""中""低"：高=有明确关键词且无歧义；中=靠上下文推断；低=信息不足或信号矛盾。
7. analysis 用一句话拆穿潜台词，20-40 字，要点出对方真实在做什么、顾虑什么，例如：
   "标准流程话术，大概率在走内部初筛，此时追问会显得急"
   "在摸你的价格和底线，先答完再看要不要继续投入"
8. strategy 用一句话说清"这条回复要达成什么"，20-40 字，必须可执行，例如：
   "简短收尾，给一个大概反馈时间，不再追加自我介绍"
   "给区间并反问对方预算，不先交底"
9. 不要输出分析过程，不要复述聊天记录，不要输出中文意图名。"""

INTENT_USER_TEMPLATE = """【岗位】
{job_line}

【简历摘要】
{resume}

【平台系统提示】（不是 HR 说的话，只作状态参考）
{system_hints}

【聊天记录（从旧到新，最多 15 条）】
{transcript}

【HR 最后一句】
{last_hr}

请按系统要求输出 JSON。"""

GENERATE_BASE_PROMPT = """你是候选人的聊天代笔，在 Boss 直聘上回复 HR。要像本人用手机打字：短、具体、有分寸。

只输出一个 JSON 对象，不要 Markdown，不要解释文字：
{"replies": [
  {"style": "风格名", "content": "回复正文"},
  {"style": "风格名", "content": "回复正文"},
  {"style": "风格名", "content": "回复正文"}
]}

通用硬性要求：
1. 每条 1-2 句话，口语化，像微信聊天；超过字数上限就删字，不要用分号把三件事串成一句。
2. 严禁"贵司""深耕""期待与您共创辉煌""您好""非常荣幸""很高兴认识您""期待您的回复""希望能有机会""祝您"等套话；不要 emoji，不要营销腔。
3. 只能使用简历里真实存在的经历、技术栈和数字，禁止编造公司、项目、指标。
4. 不替候选人承诺薪资和入职意向：薪资只给范围，到岗时间只给区间。
5. 三条之间不能同义重复，切入角度要明显不同；style 必须原样照抄下面给的三个风格名，顺序不能变。
6. 不要复述整段简历，不要写"以上"这类总结句。"""

GENERATE_SITUATION_TEMPLATE = """【当前局势】
HR 意图：{label}（{code}）
潜台词：{analysis}
本次策略：{strategy}

【三条回复的风格与分工】（顺序固定，style 原样照抄）
1. {style1}：{hint1}
2. {style2}：{hint2}
3. {style3}：{hint3}

【本类意图的额外约束】
{guidance}

【本类意图的长度与提问上限】
每条不超过 {max_chars} 个汉字；三条里最多 {max_questions} 条带问号。"""

GENERATE_USER_TEMPLATE = """【岗位】
{job_line}

【聊天记录（从旧到新）】
{transcript}

【平台系统提示】
{system_hints}

【简历摘要】
{resume}

请按系统要求输出 3 条回复的 JSON。"""


class ReplyState(TypedDict, total=False):
    """节点之间传递的工单。意图节点的结论会被生成节点当作硬约束使用。"""

    job_info: dict[str, Any]
    history: list[dict[str, str]]
    resume: str
    intent: str
    intent_label: str
    confidence: str
    analysis: str
    strategy: str
    branch: str
    replies: list[dict[str, str]]


class IntentResult(BaseModel):
    """意图节点的原始输出。字段一律宽松接收，归一化交给下面的函数。"""

    intent: Any = None
    confidence: Any = None
    analysis: Any = None
    strategy: Any = None


class ReplyBatch(BaseModel):
    """生成节点的输出约束：恰好 3 条，内容不能重复。"""

    replies: list[ReplySuggestion] = Field(..., min_length=3, max_length=3)

    @field_validator("replies")
    @classmethod
    def _distinct(cls, items: list[ReplySuggestion]) -> list[ReplySuggestion]:
        contents = [item.content for item in items]
        if len(set(contents)) != len(contents):
            raise ValueError("3 条回复内容不能重复")
        return items


def _log(node: str, message: str, mark: str = "·") -> None:
    """终端流转日志，与 agent_graph 同样的格式，方便一起看。"""
    print(f"[reply-graph {time.strftime('%H:%M:%S')}] {mark} {node:<29} {message}", flush=True)


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


def _system_hints(history: list[dict[str, str]]) -> str:
    """平台系统提示单独拎出来，避免模型把"已查看简历"当成 HR 说的话。"""
    hints = [
        message.get("content", "").strip()
        for message in history
        if message.get("role") == "system" and message.get("content", "").strip()
    ]
    return "；".join(hints[-5:]) or "（无）"


def _last_hr_message(history: list[dict[str, str]]) -> str:
    for message in reversed(history):
        if message.get("role") == "hr" and message.get("content", "").strip():
            return message["content"].strip()
    return ""


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


def _text(value: Any, limit: int) -> str:
    """把任意类型的模型输出压成一行短文本。"""
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _normalize_intent(raw: Any) -> str | None:
    """把模型写的意图归一成 6 个代号之一。认不出来返回 None，交给关键词兜底。"""
    text = str(raw or "").strip()
    if not text:
        return None

    upper = text.upper()
    for code in INTENT_PLAYBOOKS:
        if code in upper:
            return code
    for alias, code in INTENT_ALIASES.items():
        if alias in text:
            return code
    for code, label in INTENT_LABELS.items():
        if label and label in text:
            return code
    return None


def _normalize_confidence(raw: Any) -> str:
    """兼容"高/中/低"、high/medium/low、以及 0-1 的小数。"""
    if isinstance(raw, bool):
        return "中"
    if isinstance(raw, (int, float)):
        score = float(raw)
        return "高" if score >= 0.75 else "中" if score >= 0.45 else "低"

    text = str(raw or "").strip()
    for label in ("高", "中", "低"):
        if label in text:
            return label

    upper = text.upper()
    if upper in ("HIGH", "H"):
        return "高"
    if upper in ("MEDIUM", "MID", "M"):
        return "中"
    if upper in ("LOW", "L"):
        return "低"

    try:
        score = float(text)
    except (TypeError, ValueError):
        return "中"
    return "高" if score >= 0.75 else "中" if score >= 0.45 else "低"


# 关键词兜底：只在模型输出不可解析时使用，不额外花一次调用。
# 顺序按关键词的特异性排，不照抄提示词里的诉求优先级：
# 「收到 / 转交」比「简历」更具体，先判挂起，避免把"简历收到了"误判成索要资料。
_FALLBACK_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("REJECTED", ("不合适", "不匹配", "招满", "岗位关闭", "已关闭", "遗憾", "暂时不需要")),
    ("PENDING_REVIEW", ("等通知", "转交", "用人部门", "评估", "收到", "有消息")),
    ("INTERVIEW_INVITE", ("面试", "面谈", "线上面", "面试链接", "方便面")),
    ("SCREENING", ("薪资", "期望", "到岗", "离职", "预算", "多少钱", "最低")),
    ("REQUEST_INFO", ("简历", "作品集", "附件", "微信", "电话", "联系方式")),
)


def _guess_intent(history: list[dict[str, str]]) -> str:
    """模型输出不可用时的确定性兜底：先看 HR 最后一句，再看平台提示。"""
    last_hr = _last_hr_message(history)
    for code, keywords in _FALLBACK_RULES:
        if any(keyword in last_hr for keyword in keywords):
            return code

    # HR 的话没有可用信号（或还没说话）：平台提示显示看过/收过简历，就是挂起等待
    hints = _system_hints(history)
    if any(keyword in hints for keyword in ("查看", "转交", "评估", "收到")):
        return "PENDING_REVIEW"
    return "GENERAL"


def intent_node(state: ReplyState, config: RunnableConfig) -> dict[str, Any]:
    """意图节点：把 HR 的处境归入 6 类，并拆出潜台词与应对策略。"""
    history = state.get("history") or []
    _log("intent_node", f"分析 {len(history)} 条记录，定位 HR 意图", "▶")
    started = time.perf_counter()

    payload = chat_json(
        INTENT_SYSTEM_PROMPT,
        INTENT_USER_TEMPLATE.format(
            job_line=_job_line(state.get("job_info") or {}),
            resume=(state.get("resume") or "（未提供）")[:INTENT_RESUME_CHARS],
            system_hints=_system_hints(history),
            transcript=_transcript(history),
            last_hr=_last_hr_message(history) or "（HR 还没有发过消息）",
        ),
        max_tokens=500,
        temperature=0.1,
        config=_llm_config(config),
    )

    try:
        parsed = IntentResult.model_validate(payload)
    except (ValidationError, ValueError):
        parsed = IntentResult()

    intent = _normalize_intent(parsed.intent)
    if intent is None:
        intent = _guess_intent(history)
        confidence = "低"
    else:
        confidence = _normalize_confidence(parsed.confidence)

    playbook = INTENT_PLAYBOOKS[intent]
    analysis = _text(parsed.analysis, 120) or playbook.analysis_default
    strategy = _text(parsed.strategy, 120) or "按职场礼仪回应，别主动加码"

    _log(
        "intent_node",
        f"{intent}（{playbook.label}）｜置信 {confidence}｜{playbook.branch} 分支"
        f"｜{time.perf_counter() - started:.1f}s",
        "✔",
    )
    _log("intent_node", f"潜台词：{analysis}")
    return {
        "intent": intent,
        "intent_label": playbook.label,
        "confidence": confidence,
        "analysis": analysis,
        "strategy": strategy,
        "branch": playbook.branch,
    }


def _brief_error(exc: Exception) -> str:
    """把 pydantic 的多层报错压成一行人话，方便在终端定位是哪条规则没过。"""
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors()[:3]:
            loc = ".".join(str(item) for item in err.get("loc", ()))
            parts.append(f"{loc} → {err.get('msg', '')}")
        return "；".join(parts) or str(exc)[:160]
    return " ".join(str(exc).split())[:160]


def _normalize_replies(
    payload: dict[str, Any], styles: tuple[str, str, str]
) -> list[dict[str, str]]:
    """兼容模型把 replies 写成字符串数组、或字段名写成中文的情况。

    风格名不在本分支的预期三元组里时，按位置回填——前端标签和分支语义才不会漂。
    """
    raw = payload.get("replies") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []

    normalized: list[dict[str, str]] = []
    for index, item in enumerate(raw[:3]):
        default_style = styles[index]
        if isinstance(item, str):
            normalized.append({"style": default_style, "content": item.strip()})
        elif isinstance(item, dict):
            style = str(item.get("style") or item.get("风格") or "").strip()
            content = item.get("content") or item.get("text") or item.get("回复") or ""
            normalized.append(
                {
                    "style": style if style in styles else default_style,
                    "content": str(content).strip(),
                }
            )
    return normalized


def _check_playbook_rules(replies: list[ReplySuggestion], playbook: IntentPlaybook) -> None:
    """分支级硬约束：字数上限与带问号的条数。不满足就带着原因重试。"""
    for index, item in enumerate(replies, start=1):
        if len(item.content) > playbook.max_chars:
            raise ValueError(
                f"「{playbook.label}」第 {index} 条 {len(item.content)} 字，"
                f"超过 {playbook.max_chars} 字上限"
            )

    questions = sum(1 for item in replies if "？" in item.content or "?" in item.content)
    if questions > playbook.max_questions:
        raise ValueError(
            f"「{playbook.label}」有 {questions} 条带问号，最多 {playbook.max_questions} 条"
        )


def _generate_system_prompt(playbook: IntentPlaybook, analysis: str, strategy: str) -> str:
    situation = GENERATE_SITUATION_TEMPLATE.format(
        label=playbook.label,
        code=playbook.code,
        analysis=analysis,
        strategy=strategy,
        style1=playbook.styles[0],
        hint1=playbook.style_hints[0],
        style2=playbook.styles[1],
        hint2=playbook.style_hints[1],
        style3=playbook.styles[2],
        hint3=playbook.style_hints[2],
        guidance=playbook.guidance,
        max_chars=playbook.max_chars,
        max_questions=playbook.max_questions,
    )
    return f"{GENERATE_BASE_PROMPT}\n\n{situation}"


def generate_replies_node(
    state: ReplyState, config: RunnableConfig, *, branch: str | None = None
) -> dict[str, Any]:
    """生成节点：按意图匹配到的 playbook 写 3 条短回复。

    4 条分支节点共用这一份实现，差异全部来自 INTENT_PLAYBOOKS：
    风格三元组、每条怎么写、字数上限、最多几条带问号。
    """
    intent = state.get("intent") or "GENERAL"
    playbook = INTENT_PLAYBOOKS.get(intent) or INTENT_PLAYBOOKS["GENERAL"]
    branch = branch or playbook.branch
    node_name = BRANCH_NODES[branch]

    analysis = state.get("analysis") or playbook.analysis_default
    strategy = state.get("strategy") or "按常识应对，别主动加码"
    _log(node_name, f"按「{playbook.label}」写 3 条（≤{playbook.max_chars} 字）", "▶")
    started = time.perf_counter()

    system_prompt = _generate_system_prompt(playbook, analysis, strategy)
    user_prompt = GENERATE_USER_TEMPLATE.format(
        job_line=_job_line(state.get("job_info") or {}),
        transcript=_transcript(state.get("history") or []),
        system_hints=_system_hints(state.get("history") or []),
        resume=state.get("resume") or "（未提供）",
    )

    last_error: Exception | None = None
    for attempt in range(1, 4):
        prompt = user_prompt
        if last_error is not None:
            prompt += (
                f"\n\n上一次输出不合格（{last_error}）。"
                f"请重新输出 3 条，每条 1-2 句、≤{playbook.max_chars} 字，"
                f"最多 {playbook.max_questions} 条带问号，只返回 JSON。"
            )
        payload = chat_json(
            system_prompt,
            prompt,
            max_tokens=800,
            temperature=0.8,
            config=_llm_config(config),
        )
        try:
            batch = ReplyBatch.model_validate(
                {"replies": _normalize_replies(payload, playbook.styles)}
            )
            _check_playbook_rules(batch.replies, playbook)
            replies = [item.model_dump() for item in batch.replies]
            _log(
                node_name,
                f"3 条完成（第 {attempt} 次尝试）｜{time.perf_counter() - started:.1f}s",
                "✔",
            )
            return {"replies": replies, "branch": branch}
        except (ValidationError, ValueError) as exc:
            last_error = exc
            _log(node_name, f"第 {attempt} 次不合格：{_brief_error(exc)}", "!")

    raise LLMServiceError(f"回复未通过校验（已尝试 3 次）：{last_error}")


def generate_hold_replies_node(state: ReplyState, config: RunnableConfig) -> dict[str, Any]:
    """PENDING_REVIEW 分支：评估挂起，短句收尾，绝不死缠烂打。"""
    return generate_replies_node(state, config, branch="hold")


def generate_screening_replies_node(state: ReplyState, config: RunnableConfig) -> dict[str, Any]:
    """SCREENING 分支：探薪资/背景，给出三种进退有度的报价策略。"""
    return generate_replies_node(state, config, branch="screening")


def generate_interview_replies_node(state: ReplyState, config: RunnableConfig) -> dict[str, Any]:
    """INTERVIEW_INVITE 分支：确认时间，顺带问清面试形式与轮次。"""
    return generate_replies_node(state, config, branch="interview")


def generate_etiquette_replies_node(state: ReplyState, config: RunnableConfig) -> dict[str, Any]:
    """REQUEST_INFO / REJECTED / GENERAL 分支：按职场礼仪自然回应。"""
    return generate_replies_node(state, config, branch="etiquette")


def route_by_intent(state: ReplyState) -> str:
    """条件边：按意图把流程打到对应生成分支，返回目标节点名。"""
    intent = state.get("intent") or "GENERAL"
    playbook = INTENT_PLAYBOOKS.get(intent) or INTENT_PLAYBOOKS["GENERAL"]
    target = BRANCH_NODES[playbook.branch]
    _log("route_by_intent", f"{intent}（{playbook.label}）→ {target}", "→")
    return target


def build_graph():
    graph = StateGraph(ReplyState)
    graph.add_node("intent_node", intent_node)
    graph.add_node("generate_hold_replies_node", generate_hold_replies_node)
    graph.add_node("generate_screening_replies_node", generate_screening_replies_node)
    graph.add_node("generate_interview_replies_node", generate_interview_replies_node)
    graph.add_node("generate_etiquette_replies_node", generate_etiquette_replies_node)

    graph.add_edge(START, "intent_node")
    graph.add_conditional_edges(
        "intent_node",
        route_by_intent,
        {node: node for node in BRANCH_NODES.values()},
    )
    for node in BRANCH_NODES.values():
        graph.add_edge(node, END)
    return graph.compile()


GRAPH = build_graph()


def suggest_replies(
    *,
    job_info: dict[str, Any],
    history: list[dict[str, str]],
    resume: str | None = None,
    config: LLMConfig | None = None,
) -> SuggestReplyResponse:
    """对外唯一入口：跑完意图识别与对应分支，返回意图、潜台词、策略与 3 条回复。"""
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
            intent_label=final_state.get("intent_label", ""),
            confidence=final_state.get("confidence", "中"),
            analysis=final_state.get("analysis", ""),
            strategy=final_state.get("strategy", ""),
            replies=final_state["replies"],
        )
    except (KeyError, ValidationError, ValueError) as exc:
        raise LLMServiceError(f"回复结果结构异常：{exc}") from exc

    _log(
        "graph",
        f"结束｜意图 {result.intent}（{result.intent_label}）｜{len(result.replies)} 条回复"
        f"｜总耗时 {time.perf_counter() - started:.1f}s",
        "◆",
    )
    return result
