"""Railway: мини-апп + Telegram webhook + POST /ac/analyze (паттерн HRV_backend)."""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from ac_engine import analyze_ac_clip
from bot.webhook import router as telegram_router, setup_webhook, shutdown_ptb

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("combo_server")

_BASE = Path(__file__).resolve().parent
_WEBAPP = Path(os.environ.get("COMBO_WEBAPP_DIR", str(_BASE / "webapp")))
_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
_WEBAPP_PAGES = ("phone_analyze.html", "combo_live.html", "browser_overlay.html", "mobile_overlay.html")


def _public_base_url() -> str:
    from bot.webhook import _api_base
    return _api_base()


def _ensure_railway_env() -> None:
    base = _public_base_url()
    if base and not os.environ.get("COMBO_MINIAPP_URL"):
        os.environ["COMBO_MINIAPP_URL"] = base
    if base and not os.environ.get("COMBO_API_URL"):
        os.environ["COMBO_API_URL"] = base
    if not os.environ.get("COMBO_RUNTIME_DIR"):
        os.environ["COMBO_RUNTIME_DIR"] = "/tmp/combo"
    for key in ("COMBO_EMO_PYTHON", "COMBO_MAIN_PYTHON"):
        if not os.environ.get(key):
            os.environ[key] = shutil.which("python3") or "python3"


_ensure_railway_env()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await setup_webhook()
    logger.info("Assessment Center API запущен (Telegram webhook)")
    yield
    await shutdown_ptb()


app = FastAPI(title="Assessment Center", version="1.0.0", lifespan=lifespan)
app.include_router(telegram_router)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.get("/")
async def root():
    return {
        "service": "Assessment Center",
        "miniapp": _public_base_url(),
        "webhook": "/telegram/webhook",
        "analyze_api": "POST /ac/analyze",
    }


@app.get("/health")
async def health():
    return {"status": "healthy", "miniapp_url": _public_base_url()}


@app.post("/ac/analyze")
async def ac_analyze(
    video: UploadFile = File(...),
    transcript: UploadFile | None = File(None),
    speaker: str = Form("Спикер 0"),
    name: str | None = Form(None),
    label: str = Form(""),
):
    suffix = Path(video.filename or "clip.mp4").suffix.lower() or ".mp4"
    if suffix not in _VIDEO_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"неподдерживаемый формат: {suffix}")

    tmpdir = Path(tempfile.mkdtemp(prefix="ac_"))
    try:
        vpath = tmpdir / f"clip{suffix}"
        with vpath.open("wb") as f:
            shutil.copyfileobj(video.file, f)
        tpath = None
        if transcript is not None and transcript.filename:
            tpath = tmpdir / Path(transcript.filename).name
            with tpath.open("wb") as f:
                shutil.copyfileobj(transcript.file, f)
        try:
            return analyze_ac_clip(
                vpath,
                transcript=str(tpath) if tpath else None,
                speaker=speaker,
                name=name,
                label=label,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"разбор упал: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _webapp_route(page: str):
    path = _WEBAPP / page
    if not path.exists():
        return

    @app.get(f"/{page}")
    async def _serve(_p=path):
        return FileResponse(str(_p), media_type="text/html")


for _page in _WEBAPP_PAGES:
    _webapp_route(_page)
