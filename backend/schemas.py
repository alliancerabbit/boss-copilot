"""接口数据模型：/api/analyze 的请求与响应结构。

响应模型同时承担"输出契约"的作用——LLM 返回的 JSON 必须能通过这里的校验，
否则视为失败（llm_service 会带着错误原因重试一次）。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_GREETING_LEN = 60

# 明确禁止的寒暄套话。命中即判定不合格：这类开场在 Boss 直聘上会被直接划走。
BANNED_GREETING_PATTERNS = (
    "我对贵司",
    "贵司很感兴趣",
    "非常感兴趣",
    "希望能有机会",
)


class AnalyzeRequest(BaseModel):
    """前端（Chrome 侧边栏插件）传入的岗位信息。"""

    job_title: str = Field(
        ...,
        min_length=1,
        max_length=120,
        description="岗位名称，例如「AI 产品经理」",
        examples=["大模型应用开发工程师"],
    )
    jd_text: str = Field(
        ...,
        min_length=10,
        max_length=20_000,
        description="JD 原文，建议整段粘贴",
    )

    @field_validator("job_title", "jd_text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空白字符")
        return value


def clean_greetings(items: list[str]) -> list[str]:
    """校验并清洗打招呼语：非空、≤60 字、不含寒暄套话。

    接口响应校验与 agent_graph 的「重写」节点共用同一把尺子，
    避免两处规则漂移（评审说合格、响应却 422）。
    """
    cleaned: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError("打招呼语必须是字符串")
        text = item.strip()
        if not text:
            raise ValueError("打招呼语不能为空")
        if len(text) > MAX_GREETING_LEN:
            raise ValueError(
                f"打招呼语超过 {MAX_GREETING_LEN} 字（当前 {len(text)} 字）：{text[:20]}…"
            )
        for pattern in BANNED_GREETING_PATTERNS:
            if pattern in text:
                raise ValueError(f"打招呼语出现套话「{pattern}」：{text[:20]}…")
        cleaned.append(text)
    return cleaned


class AnalyzeResponse(BaseModel):
    """分析结果。字段数量与长度即为硬约束，不满足则整个响应无效。"""

    match_score: int = Field(
        ...,
        ge=0,
        le=100,
        description="0-100 的岗位匹配度",
    )
    pros: list[str] = Field(
        ...,
        min_length=3,
        max_length=3,
        description="3 条核心匹配优势，需引用简历中的具体经历",
    )
    gaps: list[str] = Field(
        ...,
        min_length=2,
        max_length=3,
        description="2-3 条短板或缺失技能",
    )
    greetings: list[str] = Field(
        ...,
        min_length=3,
        max_length=3,
        description="3 条不同风格的打招呼语，每条不超过 60 字",
    )

    @field_validator("pros", "gaps")
    @classmethod
    def _clean_items(cls, items: list[str]) -> list[str]:
        cleaned = [item.strip() for item in items]
        if any(not item for item in cleaned):
            raise ValueError("列表项不能为空")
        return cleaned

    @field_validator("greetings")
    @classmethod
    def _check_greetings(cls, items: list[str]) -> list[str]:
        return clean_greetings(items)


class InterviewRequest(AnalyzeRequest):
    """面试出题请求。字段与岗位分析完全一致，直接复用其校验规则。"""


class InterviewQuestion(BaseModel):
    """一道面试题及其回答思路。"""

    question: str = Field(
        ...,
        min_length=8,
        max_length=300,
        description="面试官视角的提问，要具体到可追问",
    )
    answer_hint: str = Field(
        ...,
        min_length=10,
        max_length=800,
        description="2-4 句回答思路：先讲什么、用什么案例、落到哪个指标",
    )

    @field_validator("question", "answer_hint")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空白字符")
        return value


class InterviewResponse(BaseModel):
    """8 道题按三类固定配比，少一道或多一道都视为模型输出不合格。"""

    project_questions: list[InterviewQuestion] = Field(
        ...,
        min_length=3,
        max_length=3,
        description="深挖简历项目的技术题，3 道",
    )
    gap_questions: list[InterviewQuestion] = Field(
        ...,
        min_length=3,
        max_length=3,
        description="针对岗位要求与简历短板的考察题，3 道",
    )
    scenario_questions: list[InterviewQuestion] = Field(
        ...,
        min_length=2,
        max_length=2,
        description="业务场景设计题，2 道",
    )

    def all_questions(self) -> list[InterviewQuestion]:
        return [*self.project_questions, *self.gap_questions, *self.scenario_questions]


class ResumeText(BaseModel):
    """简历正文提交（侧边栏粘贴/微调后保存）。"""

    content: str = Field(
        ...,
        min_length=1,
        max_length=50_000,
        description="简历全文，纯文本或 Markdown",
    )

    @field_validator("content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("简历内容不能为空")
        return value


class ResumeState(BaseModel):
    """简历的当前状态。exists 为 false 时 content 为空字符串。"""

    exists: bool = Field(..., description="本地是否已有一份可用的简历")
    content: str = Field("", description="简历全文，未配置时为空字符串")
    chars: int = Field(0, description="字符数，便于前端判断是否需要重新配置")


class TestConnectionResult(BaseModel):
    """探活结果。失败也返回 200，让前端直接展示原始报错。"""

    ok: bool = Field(..., description="Key / Base URL / 模型名 是否能正常调通")
    model: str = Field("", description="本次实际使用的模型名")
    latency_ms: int = Field(0, description="往返耗时（毫秒）")
    error: str = Field("", description="失败原因，成功时为空")


# ---------------------------------------------------------------------------
# 聊天页智能回复：/api/suggest-reply
# ---------------------------------------------------------------------------

MAX_REPLY_LEN = 80

# 聊天场景的禁用词：比打招呼语更严，"您好"这类客套在 Boss 聊天里同样假。
BANNED_REPLY_PATTERNS = (
    "贵司",
    "深耕",
    "共创辉煌",
    "期待与您",
    "您好",
    "非常荣幸",
    "很高兴认识您",
    "感谢您的关注",
    "期待您的回复",
    "希望能有机会",
    "非常感兴趣",
    "祝您",
)

# HR 意图只有 6 类，前后端与提示词共用这一份枚举，避免各写一套导致对不上。
# 具体每类对应的回复风格与约束在 reply_graph.INTENT_PLAYBOOKS 里定义。
IntentCode = Literal[
    "REQUEST_INFO",
    "PENDING_REVIEW",
    "SCREENING",
    "INTERVIEW_INVITE",
    "REJECTED",
    "GENERAL",
]

INTENT_LABELS: dict[str, str] = {
    "REQUEST_INFO": "索要资料",
    "PENDING_REVIEW": "评估挂起",
    "SCREENING": "薪资背景探询",
    "INTERVIEW_INVITE": "约面试",
    "REJECTED": "婉拒",
    "GENERAL": "日常沟通",
}


def _sentence_count(text: str) -> int:
    """按句末标点切分，统计句子数。没有标点也算 1 句。

    省略号「……」常被当作停顿而不是句末，算进去会把 2 句误判成 3 句，所以不切它。
    """
    parts = [part for part in re.split(r"[。！？!?]+", text) if part.strip()]
    return max(1, len(parts))


class ChatMessage(BaseModel):
    """聊天记录里的一条消息。role 只有三种：HR、我、系统提示。"""

    role: Literal["hr", "me", "system"] = Field(..., description="hr=对方，me=我，system=平台提示")
    content: str = Field(..., min_length=1, max_length=2000)

    @field_validator("content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("消息内容不能为空")
        return text


class JobInfo(BaseModel):
    """聊天页顶部能读到的岗位信息。读不到就传空字符串，不影响生成。"""

    company: str = Field("", max_length=100, description="公司名")
    title: str = Field("", max_length=120, description="岗位名称")
    salary: str = Field("", max_length=60, description="薪资范围原文")

    @field_validator("company", "title", "salary")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class SuggestReplyRequest(BaseModel):
    """聊天页请求。resume 可选：不传就用后端 resume.md。"""

    job_info: JobInfo = Field(default_factory=JobInfo)
    chat_history: list[ChatMessage] = Field(
        ..., min_length=1, max_length=30, description="按时间从旧到新，最近 10-15 条即可"
    )
    resume: str | None = Field(
        None, max_length=20_000, description="可选，覆盖后端简历；不传则复用 resume.md"
    )


class ReplySuggestion(BaseModel):
    """一条候选回复。硬约束：1-2 句、≤80 字、无套话。"""

    style: str = Field(..., min_length=1, max_length=20)
    content: str = Field(..., description="可直接发出的回复正文")

    @field_validator("content")
    @classmethod
    def _check_content(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("回复不能为空")
        if "\n" in text or "\r" in text:
            raise ValueError("回复必须是一段连续的话，不能换行")
        if len(text) > MAX_REPLY_LEN:
            raise ValueError(f"回复超过 {MAX_REPLY_LEN} 字（当前 {len(text)} 字）：{text[:20]}…")
        if _sentence_count(text) > 2:
            raise ValueError(f"回复超过 2 句话：{text[:30]}…")
        for pattern in BANNED_REPLY_PATTERNS:
            if pattern in text:
                raise ValueError(f"回复出现套话「{pattern}」：{text[:20]}…")
        return text

    @field_validator("style")
    @classmethod
    def _strip_style(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("风格名不能为空")
        return text


class SuggestReplyResponse(BaseModel):
    """意图判定 + 潜台词 + 策略 + 3 条不同风格的回复。

    intent 固定为 6 类代号；intent_label 由服务端补全，前端直接拿去渲染徽章，
    不需要在前端再维护一份中英对照。
    """

    intent: IntentCode = Field(..., description="HR 意图代号，6 类之一")
    intent_label: str = Field("", max_length=20, description="意图中文标签，供前端徽章展示")
    confidence: Literal["高", "中", "低"] = Field("中", description="意图判断置信度")
    analysis: str = Field("", max_length=160, description="一句话潜台词分析")
    strategy: str = Field("", max_length=120, description="一句话应对策略")
    replies: list[ReplySuggestion] = Field(..., min_length=3, max_length=3)

    @model_validator(mode="after")
    def _fill_label(self) -> "SuggestReplyResponse":
        if not self.intent_label.strip():
            self.intent_label = INTENT_LABELS.get(self.intent, "日常沟通")
        return self

    @field_validator("replies")
    @classmethod
    def _distinct_styles(cls, items: list[ReplySuggestion]) -> list[ReplySuggestion]:
        styles = [item.style for item in items]
        if len(set(styles)) != len(styles):
            raise ValueError("3 条回复的风格不能重复")
        return items
