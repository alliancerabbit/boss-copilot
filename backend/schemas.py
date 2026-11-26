"""接口数据模型：/api/analyze 的请求与响应结构。

响应模型同时承担"输出契约"的作用——LLM 返回的 JSON 必须能通过这里的校验，
否则视为失败（llm_service 会带着错误原因重试一次）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

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
        cleaned: list[str] = []
        for item in items:
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
