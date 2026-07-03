"""
combo_config.py — конфигурация КОМБО-бота (@assessment_center_analyzer_bot).

Без хардкода секретов и путей (ревью PUF):
- Токен — ТОЛЬКО из env/.env (COMBO_BOT_TOKEN). В коде токена нет.
- Runtime-хранилище — COMBO_RUNTIME_DIR (по умолчанию рядом с кодом; на сервере,
  где Dropbox read-only, задать writable путь, напр. /home/node/openclaw-vault/combo).
- Интерпретеры venv — COMBO_MAIN_PYTHON / COMBO_EMO_PYTHON (по умолчанию локальные venv).
"""
import os
from pathlib import Path

from env_loader import get_secret

BASE_DIR = Path(__file__).parent

# --- секрет: только из окружения/.env, НИКОГДА не в коде ---
COMBO_BOT_TOKEN = get_secret(
    "COMBO_BOT_TOKEN",
    files=[BASE_DIR / ".env", BASE_DIR.parent / ".env"],
)

# --- runtime-хранилище (writable). Dropbox на сервере read-only → выносим через env ---
RUNTIME_DIR = Path(os.environ.get("COMBO_RUNTIME_DIR", str(BASE_DIR)))
COMBO_DIR = RUNTIME_DIR / "combo"
INCOMING_DIR = COMBO_DIR / "incoming"
RESULTS_DIR = COMBO_DIR / "results"
EXPORTS_DIR = COMBO_DIR / "exports"
LOGS_DIR = COMBO_DIR / "logs"
LIVE_DIR = COMBO_DIR / "live"

# webapp обслуживает combo_data.json — по умолчанию рядом с кодом, переопределяемо
WEBAPP_DIR = Path(os.environ.get("COMBO_WEBAPP_DIR", str(BASE_DIR / "webapp")))

# создаём каталоги, но не падаем на read-only ФС (тогда задать COMBO_RUNTIME_DIR)
for d in (COMBO_DIR, INCOMING_DIR, RESULTS_DIR, EXPORTS_DIR, LOGS_DIR, LIVE_DIR, WEBAPP_DIR):
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

# --- интерпретеры анализаторов (каждый модуль в своём venv) ---
EMO_PYTHON = os.environ.get("COMBO_EMO_PYTHON", str(BASE_DIR / "emo_venv" / "bin" / "python"))
MAIN_PYTHON = os.environ.get("COMBO_MAIN_PYTHON", str(BASE_DIR / "venv_new" / "bin" / "python"))

# Частота семплирования кадров при оффлайн-разборе видео
SAMPLE_FPS = float(os.environ.get("COMBO_SAMPLE_FPS", "5"))

# Мини-апки (phone_analyze.html / combo_live.html) для Telegram Web App. HTTPS обязателен.
# Сейчас — cloudflared-туннель; для постоянной — домен Railway.
COMBO_MINIAPP_URL = (get_secret("COMBO_MINIAPP_URL", files=[BASE_DIR / ".env", BASE_DIR.parent / ".env"]) or "").rstrip("/")


def require_token() -> str:
    """Вернуть токен или явная ошибка (для точек запуска бота)."""
    if not COMBO_BOT_TOKEN:
        raise SystemExit(
            "COMBO_BOT_TOKEN не задан. Создай .env рядом с кодом или экспортируй переменную:\n"
            "  COMBO_BOT_TOKEN=<токен @assessment_center_analyzer_bot>\n"
            "ВАЖНО: старый токен скомпрометирован (был в коде/чате) — перевыпусти у @BotFather."
        )
    return COMBO_BOT_TOKEN
