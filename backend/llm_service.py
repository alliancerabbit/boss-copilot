"""调用 DeepSeek（OpenAI 兼容接口）完成 JD × 简历的匹配分析。

设计要点：
- 走 JSON Mode（response_format={"type": "json_object"}）+ Pydantic 强校验，
  模型输出只要有一处不符合 AnalyzeResponse，就带错误原因重试一次。
- 配置全部来自 backend/.env，缺失时抛 ConfigurationError（由 main 映射成 503），
  与"模型调用失败"（LLMServiceError → 502）区分开，方便插件提示用户。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError
from pydantic import ValidationError

try:  # 作为包导入（uvicorn backend.main:app）
    from .schemas import AnalyzeResponse, InterviewResponse
except ImportError:  # 从 backend/ 目录直接启动（uvicorn main:app）
    from schemas import AnalyzeResponse, InterviewResponse

BACKEND_DIR = Path(__file__).resolve().parent
ENV_PATH = BACKEND_DIR / ".env"
RESUME_PATH = BACKEND_DIR / "resume.md"

# 导入时先加载一次，/api/health 才能反映真实配置；每次请求还会再读，改 .env 无需重启。
load_dotenv(ENV_PATH)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
MAX_ATTEMPTS = 2


class ConfigurationError(RuntimeError):
    """环境没准备好：缺密钥、缺简历。属于用户可自行修复的问题。"""


class LLMServiceError(RuntimeError):
    """模型调用失败，或返回内容无法通过结构校验。"""


SYSTEM_PROMPT = """你是一位资深技术招聘顾问，擅长把候选人的真实经历与岗位 JD 做精准匹配。

只输出一个 JSON 对象，不要 Markdown 代码块，不要任何解释性文字，字段严格如下：

{
  "match_score": 0-100 的整数,
  "pros": [3 条],
  "gaps": [2-3 条],
  "greetings": [3 条]
}

字段要求：
1. match_score：完全匹配 85 以上，基本匹配 60-84，跨行 40-59，明显不符 40 以下。宁可保守，不要讨好。
2. pros：3 条核心匹配优势。每条必须落在简历里的具体经历、项目或技能上，并对应 JD 的某条要求，禁止"学习能力强""有责任心"这类空话。
3. gaps：2-3 条短板，直指 JD 要求但简历未覆盖的部分，说清缺什么、面试官会怎么追问。
4. greetings：3 条 Boss 直聘开场白，风格互不相同，依次为：
   (a) 痛点切入：点出该岗位当前最可能卡住的问题 + 我做过同类事情；
   (b) 结果开场：用一个可量化的成果或数字开场；
   (c) 提问共创：用一个与业务强相关的问题开场，引出对话。
   每条不超过 60 个汉字。严禁"您好，我对贵司很感兴趣""很高兴认识您""期待您的回复"等任何寒暄套话，
   第一句就必须进入岗位或经历，不要自报家门。
5. 全部使用简体中文，不要 emoji，不要营销腔。"""

USER_PROMPT_TEMPLATE = """【岗位】
{job_title}

【JD 原文】
{jd_text}

【简历】
{resume}

请按系统要求输出 JSON 对象。"""


def load_resume() -> str:
    """读取简历原文。每次请求都读，方便直接改文件后立即生效。"""
    if not RESUME_PATH.exists():
        raise ConfigurationError(
            f"未找到简历文件：{RESUME_PATH}。请先创建 backend/resume.md 再重试。"
        )
    text = RESUME_PATH.read_text(encoding="utf-8").strip()
    if not text:
        raise ConfigurationError(f"简历文件为空：{RESUME_PATH}")
    return text


def _build_client() -> OpenAI:
    load_dotenv(ENV_PATH)  # 放在调用时加载，改完 .env 不必重启进程
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ConfigurationError(
            "未配置 DEEPSEEK_API_KEY。请复制 backend/.env.example 为 backend/.env 并填入密钥。"
        )
    base_url = os.getenv("DEEPSEEK_BASE_URL", "").strip() or DEFAULT_BASE_URL
    return OpenAI(api_key=api_key, base_url=base_url, timeout=60.0, max_retries=1)


def _extract_json(raw: str) -> dict:
    """从模型输出里取出 JSON 对象，兼容偶发的 ```json 围栏或前后废话。"""
    text = raw.strip()
    if not text:
        raise LLMServiceError("模型返回了空内容。")

    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMServiceError(f"模型未返回 JSON 对象：{text[:120]}")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise LLMServiceError("模型返回的 JSON 不是对象。")
    return payload


def analyze_jd(job_title: str, jd_text: str) -> AnalyzeResponse:
    """分析岗位与简历的匹配度。失败时抛 ConfigurationError / LLMServiceError。"""
    resume = load_resume()
    client = _build_client()
    model = os.getenv("DEEPSEEK_MODEL", "").strip() or DEFAULT_MODEL

    base_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": USER_PROMPT_TEMPLATE.format(
                job_title=job_title, jd_text=jd_text, resume=resume
            ),
        },
    ]

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        messages = list(base_messages)
        if last_error is not None:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"上一次输出不符合要求（{last_error}）。"
                        "请重新输出，只返回一个合法 JSON 对象，严格遵守字段数量与 60 字上限。"
                    ),
                }
            )

        try:
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.7,
                max_tokens=1500,
            )
        except OpenAIError as exc:
            raise LLMServiceError(f"调用 DeepSeek 失败：{exc}") from exc

        raw = ""
        if completion.choices:
            raw = (completion.choices[0].message.content or "").strip()
        try:
            return AnalyzeResponse.model_validate(_extract_json(raw))
        except (LLMServiceError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_error = exc

    raise LLMServiceError(f"模型输出未通过结构校验（已尝试 {MAX_ATTEMPTS} 次）：{last_error}")


def _model_name() -> str:
    return os.getenv("DEEPSEEK_MODEL", "").strip() or DEFAULT_MODEL


def chat_json(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = 1500,
    temperature: float = 0.7,
) -> dict:
    """单次 JSON Mode 调用并解析为 dict。供出题、技能抽取等场景复用。"""
    client = _build_client()
    try:
        completion = client.chat.completions.create(
            model=_model_name(),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except OpenAIError as exc:
        raise LLMServiceError(f"调用 DeepSeek 失败：{exc}") from exc

    raw = ""
    if completion.choices:
        raw = (completion.choices[0].message.content or "").strip()

    try:
        return _extract_json(raw)
    except json.JSONDecodeError as exc:
        raise LLMServiceError(f"模型返回的 JSON 无法解析：{exc}") from exc


INTERVIEW_SYSTEM_PROMPT = """你是一位带过多年团队的资深技术面试官，擅长一场面试就摸清候选人的真实水平。

只输出一个 JSON 对象，不要 Markdown 代码块，不要解释文字，字段与配比严格如下：

{
  "project_questions": [{"question": "...", "answer_hint": "..."}, ...共 3 条],
  "gap_questions":     [{"question": "...", "answer_hint": "..."}, ...共 3 条],
  "scenario_questions":[{"question": "...", "answer_hint": "..."}, ...共 2 条]
}

出题规则：
1. project_questions（3 条）：只从【简历】里的具体项目/经历出发，必须点到真实的技术选型或数字，例如"订单接口 P99 从 480ms 到 120ms，具体改了哪几层"。禁止"介绍一下你的项目""你最大的挑战是什么"这类泛问。
2. gap_questions（3 条）：挑【JD】明确要求、而【简历】明显薄弱或缺失的点出题，像真实面试官那样连续追问细节，目的是逼出真实水平而不是让人背答案。
3. scenario_questions（2 条）：结合该岗位的真实业务场景出开放设计题，考察方案取舍与拆解能力（例如"如果让你在现有系统上落地 XX，你会怎么分阶段做"）。不要问八股文。
4. answer_hint：每题 2-4 句，写"答题结构"而不是标准答案——先给结论或思路，再指一个简历里的具体案例，最后落到指标、取舍或风险。
5. 语言：简体中文。题目 ≤60 字，思路 ≤120 字。总共 8 条，数量不能多也不能少。"""

INTERVIEW_USER_TEMPLATE = """【岗位】
{job_title}

【JD 原文】
{jd_text}

【简历】
{resume}

请按系统要求输出 JSON。"""


def generate_interview_questions(job_title: str, jd_text: str) -> InterviewResponse:
    """生成 8 道面试题（3 项目深挖 + 3 短板追问 + 2 场景设计）。"""
    resume = load_resume()
    base_prompt = INTERVIEW_USER_TEMPLATE.format(
        job_title=job_title, jd_text=jd_text, resume=resume
    )

    last_error: Exception | None = None
    for _ in range(MAX_ATTEMPTS):
        prompt = base_prompt
        if last_error is not None:
            prompt += (
                f"\n\n上一次输出不合格（{last_error}）。"
                "请重新输出，字段名不变，严格 3/3/2 配比，不要多写解释。"
            )
        try:
            payload = chat_json(
                INTERVIEW_SYSTEM_PROMPT, prompt, max_tokens=2200, temperature=0.6
            )
            return InterviewResponse.model_validate(payload)
        except (LLMServiceError, ValidationError, ValueError) as exc:
            last_error = exc

    raise LLMServiceError(f"面试题未通过结构校验（已尝试 {MAX_ATTEMPTS} 次）：{last_error}")


SKILL_SYSTEM_PROMPT = """你是技术招聘数据标注员，负责把 JD 文本归一化成标准技能标签。

只输出 JSON：{"skills": ["...", "..."]}

规则：
1. 只抽硬技能：编程语言、框架、中间件、数据库、云与基础设施、架构或方法论（如 RAG、微服务、Agent）、领域工具。不要抽软技能、学历、工作年限、公司名、岗位名、薪资。
2. 统一成行业通用写法，例如 Python、FastAPI、MySQL、Redis、Elasticsearch、Docker、Kubernetes、RAG、Milvus、ClickHouse、Celery、LangChain。
3. 同义合并：「大模型」并入 LLM，「向量数据库」「Milvus」「FAISS」若语境是检索，统一为「向量检索」。
4. 去重，保持出现顺序，最多 {limit} 个；文本里没有相关技能就返回空数组。
5. 严禁编造文本中未出现的技能。"""


def extract_skills(text: str, *, max_skills: int = 12) -> list[str]:
    """从 JD / 简历文本中抽取标准化技能关键词。

    上限按用途区分：JD 只看核心要求（12 个够），简历要尽量全（建议 40），
    否则简历里的后半段技能会被截断，缺口对比就会误报。
    """
    if not text or not text.strip():
        return []

    payload = chat_json(
        SKILL_SYSTEM_PROMPT.replace("{limit}", str(max_skills)),
        f"【文本】\n{text[:6000]}",
        max_tokens=400 if max_skills <= 15 else 900,
        temperature=0.1,
    )

    raw = payload.get("skills")
    if not isinstance(raw, list):
        return []

    seen: set[str] = set()
    skills: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip().strip("，,。;；、")
        if not name or len(name) > 40:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        skills.append(name)
        if len(skills) >= max_skills:
            break
    return skills
