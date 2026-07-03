"""Единый Railway-сервис: мини-апп (статика) + Telegram webhook + POST /ac/analyze.

Старт: uvicorn combo_server:app --host 0.0.0.0 --port $PORT
Env: COMBO_BOT_TOKEN, COMBO_MINIAPP_URL (или RAILWAY_PUBLIC_DOMAIN), COMBO_RUNTIME_DIR=/tmp/combo
"""
from __future__ import annotations

import logging
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from telegram import Update

import combo_bot
from ac_engine import analyze_ac_clip

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("combo_server")

_BASE = Path(__file__).resolve().parent
_WEBAPP = Path(os.environ.get("COMBO_WEBAPP_DIR", str(_BASE / "webapp")))
_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
_WEBAPP_PAGES = ("phone_analyze.html", "combo_live.html", "browser_overlay.html")

ptb_app = combo_bot.build_app()
_webhook_secret = os.environ.get("WEBHOOK_SECRET", "").strip()


def _public_base_url() -> str:
    explicit = (os.environ.get("COMBO_MINIAPP_URL") or "").rstrip("/")
    if explicit:
        return explicit
    static = (os.environ.get("RAILWAY_STATIC_URL") or "").rstrip("/")
    if static:
        return static
    domain = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if domain:
        return f"https://{domain}"
    return ""


def _ensure_railway_env() -> None:
    base = _public_base_url()
    if base and not os.environ.get("COMBO_MINIAPP_URL"):
        os.environ["COMBO_MINIAPP_URL"] = base
    if not os.environ.get("COMBO_RUNTIME_DIR"):
        os.environ["COMBO_RUNTIME_DIR"] = "/tmp/combo"
    for key in ("COMBO_EMO_PYTHON", "COMBO_MAIN_PYTHON"):
        if not os.environ.get(key):
            os.environ[key] = shutil.which("python3") or "python3"


_ensure_railway_env()


@asynccontextmanager
async def lifespan(app: FastAPI):
    base = _public_base_url()
    if not base:
        logger.warning("COMBO_MINIAPP_URL / RAILWAY_PUBLIC_DOMAIN не задан — /analyze без кнопки WebApp")
    webhook_url = f"{base}/telegram/webhook" if base else None

    await ptb_app.initialize()
    await ptb_app.start()
    if webhook_url:
        kwargs = {"url": webhook_url, "drop_pending_updates": True}
        if _webhook_secret:
            kwargs["secret_token"] = _webhook_secret
        await ptb_app.bot.set_webhook(**kwargs)
        logger.info("Webhook: %s", webhook_url)
    else:
        logger.error("Webhook не установлен — задайте публичный HTTPS URL (Railway domain)")
    yield
    try:
        await ptb_app.bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass
    await ptb_app.stop()
    await ptb_app.shutdown()


app = FastAPI(title="Assessment Center", version="1.0.0", lifespan=lifespan)
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


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if _webhook_secret:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token != _webhook_secret:
            raise HTTPException(status_code=403, detail="bad webhook secret")
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return {"ok": True}


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

    import tempfile

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
