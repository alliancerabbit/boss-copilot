"""BossCopilot 本地后端入口。

启动方式（二选一）：
    cd backend && uvicorn main:app --reload --port 8000
    python backend/main.py

接口：
    POST /api/analyze        JD + 简历 → 匹配度、优势、短板、打招呼语
    POST /api/interview      JD + 简历 → 8 道面试题
    GET  /api/resume         读取当前简历
    POST /api/resume         保存粘贴/微调后的简历
    POST /api/resume/upload  上传 .pdf / .md / .txt 简历并解析
    GET  /api/health         自检（简历是否存在、密钥是否已配）
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

try:  # 作为包导入（uvicorn backend.main:app）
    from .llm_service import (
        RESUME_PATH,
        ConfigurationError,
        LLMServiceError,
        analyze_jd,
        generate_interview_questions,
    )
    from .schemas import (
        AnalyzeRequest,
        AnalyzeResponse,
        InterviewRequest,
        InterviewResponse,
        ResumeState,
        ResumeText,
    )
except ImportError:  # 从 backend/ 目录启动（uvicorn main:app）
    from llm_service import (
        RESUME_PATH,
        ConfigurationError,
        LLMServiceError,
        analyze_jd,
        generate_interview_questions,
    )
    from schemas import (
        AnalyzeRequest,
        AnalyzeResponse,
        InterviewRequest,
        InterviewResponse,
        ResumeState,
        ResumeText,
    )

BACKEND_DIR = Path(__file__).resolve().parent
DATA_DIR = BACKEND_DIR / "data"
JOBS_LOG = DATA_DIR / "jobs.jsonl"

# 同一进程内并发写 jsonl 时避免行与行交错。多 worker 部署需换文件锁，本地单进程够用。
_LOG_LOCK = threading.Lock()

logger = logging.getLogger("bosscopilot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("BossCopilot backend 就绪，分析记录将写入 %s", JOBS_LOG)
    yield


app = FastAPI(
    title="BossCopilot API",
    version="0.1.0",
    description="基于本地 DeepSeek 的 Boss 直聘 JD 匹配分析服务",
    lifespan=lifespan,
)

# Chrome 扩展的 side panel 发出的请求，Origin 形如 chrome-extension://<扩展ID>，
# 扩展 ID 在开发期会变，因此放开来源；不带 Cookie，故 allow_credentials 必须为 False。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def append_job_record(record: dict) -> None:
    """把一次分析追加到 jobs.jsonl，一行一条，中文不转义。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _LOG_LOCK, JOBS_LOG.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "resume_loaded": RESUME_PATH.exists(),
        "api_key_configured": bool(os.getenv("DEEPSEEK_API_KEY", "").strip()),
        "log_file": str(JOBS_LOG),
    }


@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    try:
        result = analyze_jd(request.job_title, request.jd_text)
    except ConfigurationError as exc:
        # 缺密钥 / 缺简历：环境问题，让调用方知道该去配什么
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    record = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "job_title": request.job_title,
        "jd_text": request.jd_text,
        "result": result.model_dump(),
    }
    try:
        append_job_record(record)
    except OSError as exc:
        # 落盘失败不该让用户丢掉已经拿到的分析结果，只记录告警
        logger.warning("写入 %s 失败：%s", JOBS_LOG, exc)

    return result


@app.post("/api/interview", response_model=InterviewResponse)
def interview(request: InterviewRequest) -> InterviewResponse:
    """按 JD + 简历生成 8 道面试题：3 项目深挖 + 3 短板追问 + 2 场景设计。"""
    try:
        return generate_interview_questions(request.job_title, request.jd_text)
    except ConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


ALLOWED_RESUME_SUFFIXES = {".pdf", ".md", ".txt"}
MIN_RESUME_CHARS = 20


def normalize_resume_text(text: str) -> str:
    """统一换行、压缩空白，避免 PDF 抽取出来的碎行污染后续 prompt。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def decode_text_file(raw: bytes) -> str:
    """md/txt 可能是 GBK 存的，按 UTF-8 → GB18030 依次尝试。"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_pdf_text(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # 依赖没装时给出可执行的提示
        raise HTTPException(
            status_code=500,
            detail="缺少 pypdf 依赖，请执行 pip install -r backend/requirements.txt",
        ) from exc

    try:
        reader = PdfReader(BytesIO(raw))
    except Exception as exc:  # pypdf 的异常类型随版本变化，统一兜住
        raise HTTPException(status_code=400, detail=f"PDF 解析失败：{exc}") from exc

    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return "\n".join(pages)


def read_resume() -> tuple[str, bool]:
    if not RESUME_PATH.exists():
        return "", False
    content = RESUME_PATH.read_text(encoding="utf-8")
    return content, bool(content.strip())


def save_resume_text(text: str) -> None:
    RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESUME_PATH.write_text(text.rstrip() + "\n", encoding="utf-8")


@app.get("/api/resume", response_model=ResumeState)
def get_resume() -> ResumeState:
    content, exists = read_resume()
    return ResumeState(exists=exists, content=content, chars=len(content))


@app.post("/api/resume", response_model=ResumeState)
def put_resume(payload: ResumeText) -> ResumeState:
    try:
        save_resume_text(payload.content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入简历失败：{exc}") from exc

    content, exists = read_resume()
    return ResumeState(exists=exists, content=content, chars=len(content))


@app.post("/api/resume/upload", response_model=ResumeState)
async def upload_resume(file: UploadFile = File(...)) -> ResumeState:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_RESUME_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型「{suffix or '未知'}」，仅支持 .pdf / .md / .txt",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件是空的。")

    text = extract_pdf_text(raw) if suffix == ".pdf" else decode_text_file(raw)
    text = normalize_resume_text(text)

    if len(text) < MIN_RESUME_CHARS:
        raise HTTPException(
            status_code=400,
            detail="解析出的文字太少（可能是扫描版 PDF 或空文件）。请改用 .md/.txt，或直接在文本框里粘贴。",
        )

    try:
        save_resume_text(text)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入简历失败：{exc}") from exc

    content, exists = read_resume()
    return ResumeState(exists=exists, content=content, chars=len(content))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.getenv("PORT", "8000")),
    )
