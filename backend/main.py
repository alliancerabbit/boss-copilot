"""BossCopilot 本地后端入口。

启动方式（二选一）：
    cd backend && uvicorn main:app --reload --port 8000
    python backend/main.py

接口：
    POST /api/analyze   JD + 简历 → 匹配度、优势、短板、打招呼语
    GET  /api/health    自检（简历是否存在、密钥是否已配）
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

try:  # 作为包导入（uvicorn backend.main:app）
    from .llm_service import (
        RESUME_PATH,
        ConfigurationError,
        LLMServiceError,
        analyze_jd,
    )
    from .schemas import AnalyzeRequest, AnalyzeResponse
except ImportError:  # 从 backend/ 目录启动（uvicorn main:app）
    from llm_service import (
        RESUME_PATH,
        ConfigurationError,
        LLMServiceError,
        analyze_jd,
    )
    from schemas import AnalyzeRequest, AnalyzeResponse

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.getenv("PORT", "8000")),
    )
