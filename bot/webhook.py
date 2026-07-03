"""Telegram webhook для FastAPI — sleep-friendly, как HRV_backend."""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, Response
from telegram import Update, MenuButtonWebApp, WebAppInfo

import combo_bot

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/telegram/webhook"
router = APIRouter()

_ptb_app = None
_ptb_started = False


def _api_base() -> str:
    for key in ("COMBO_API_URL", "COMBO_MINIAPP_URL", "RAILWAY_STATIC_URL"):
        v = (os.environ.get(key) or "").rstrip("/")
        if v:
            if not v.startswith("http"):
                return f"https://{v}"
            return v
    domain = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    return f"https://{domain}" if domain else ""


def webhook_secret() -> str:
    explicit = os.environ.get("WEBHOOK_SECRET", "").strip()
    if explicit:
        return explicit
    token = (os.environ.get("COMBO_BOT_TOKEN") or "").strip()
    if token:
        return hashlib.sha256(token.encode()).hexdigest()[:32]
    return ""


def public_webhook_url() -> Optional[str]:
    base = _api_base()
    return f"{base}{WEBHOOK_PATH}" if base else None


async def _ensure_ptb_running():
    """Ленивый старт PTB — как HRV: первый POST после sleep не ждёт lifespan."""
    global _ptb_app, _ptb_started
    if _ptb_started and _ptb_app is not None:
        return _ptb_app
    _ptb_app = combo_bot.build_app()
    await _ptb_app.initialize()
    await _ptb_app.start()
    _ptb_started = True
    return _ptb_app


async def setup_webhook() -> None:
    if not os.environ.get("COMBO_BOT_TOKEN"):
        logger.warning("COMBO_BOT_TOKEN не задан — webhook пропущен")
        return
    url = public_webhook_url()
    if not url:
        logger.warning("COMBO_API_URL не задан — webhook не зарегистрирован")
        return
    app = await _ensure_ptb_running()
    secret = webhook_secret()
    base = _api_base().rstrip("/")
    await app.bot.set_webhook(
        url=url,
        secret_token=secret or None,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )
    hub = f"{base}/ac_hub.html"
    try:
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="АЦ", web_app=WebAppInfo(url=hub)),
        )
        logger.info("Menu button → %s", hub)
    except Exception as e:
        logger.warning("Menu button не установлен (нужен /setdomain в @BotFather): %s", e)
    logger.info("Telegram webhook зарегистрирован: %s", url)


async def shutdown_ptb() -> None:
    """Останавливаем PTB, webhook в Telegram НЕ снимаем (sleep будит POST на URL)."""
    global _ptb_started, _ptb_app
    if not _ptb_started or _ptb_app is None:
        return
    await _ptb_app.stop()
    await _ptb_app.shutdown()
    _ptb_started = False
    _ptb_app = None


@router.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None),
):
    expected = webhook_secret()
    if expected and x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=403, detail="invalid webhook secret")
    app = await _ensure_ptb_running()
    data = await request.json()
    update = Update.de_json(data, app.bot)
    await app.process_update(update)
    return Response(status_code=200)
