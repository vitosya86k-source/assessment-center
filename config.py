"""
Конфигурация бота NeuroHRV Monitor
"""
import os
from pathlib import Path

# Токен Telegram бота — ТОЛЬКО из env/.env, без хардкода (ревью PUF).
# Старый токен лежал в коде/чате → скомпрометирован, перевыпустить у @BotFather.
from env_loader import get_secret  # noqa: E402

BASE_DIR = Path(__file__).parent
_ENV_FILES = [BASE_DIR / ".env", BASE_DIR.parent / ".env"]
TELEGRAM_BOT_TOKEN = get_secret(
    "HRV_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN",
    files=_ENV_FILES,
) or ""

# Публичный HTTPS для телефонного замера (Web Bluetooth). Из env/.env — НЕ os.getenv,
# т.к. .env не попадает в окружение процесса. Туннель: hrv-tunnel-start.sh.
HRV_WEBAPP_URL = (get_secret("HRV_WEBAPP_URL", files=_ENV_FILES) or "").rstrip("/")
HRV_API_URL = (get_secret("HRV_API_URL", files=_ENV_FILES) or "").rstrip("/")

# Возраст для био-возраста: из USER_AGE или вычислить из USER_BIRTHDATE (ГГГГ-ММ-ДД).
# Кладём в os.environ — его читает hrv_calculator._calculate_biological_age.
from datetime import date as _date  # noqa: E402
USER_AGE = get_secret("USER_AGE", files=_ENV_FILES)
_bd = get_secret("USER_BIRTHDATE", files=_ENV_FILES)
if not USER_AGE and _bd:
    try:
        _y, _m, _d = (int(x) for x in _bd.split("-"))
        _t = _date.today()
        USER_AGE = str(_t.year - _y - ((_t.month, _t.day) < (_m, _d)))
    except Exception:
        USER_AGE = None
if USER_AGE:
    os.environ["USER_AGE"] = str(USER_AGE)

# Пути (runtime; на read-only ФС переопределить через env, иначе пишет рядом с кодом).
# HRV_RUNTIME_DIR — общий корень writable-хранилища (на сервере: openclaw-vault).
_RUNTIME = Path(os.environ.get("HRV_RUNTIME_DIR", str(BASE_DIR)))
DATA_DIR = Path(os.environ.get("HRV_DATA_DIR", str(_RUNTIME / "data")))
DASHBOARDS_DIR = Path(os.environ.get("HRV_DASHBOARDS_DIR", str(_RUNTIME / "dashboards")))
EXPORTS_DIR = Path(os.environ.get("HRV_EXPORTS_DIR", str(_RUNTIME / "exports")))
LOGS_DIR = Path(os.environ.get("HRV_LOGS_DIR", str(_RUNTIME / "logs")))
WEBAPP_DIR = Path(os.environ.get("HRV_WEBAPP_DIR", str(BASE_DIR / "webapp")))

# Создаем необходимые директории (не падаем на read-only)
for directory in [DATA_DIR, DASHBOARDS_DIR, EXPORTS_DIR, LOGS_DIR]:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

# Настройки дашбордов
DASHBOARD_CONFIG = {
    "profile": {"width": 1200, "height": 900},
    "swot": {"width": 1200, "height": 700},
    "dynamics": {"width": 1200, "height": 800},
    "reference": {"width": 1200, "height": 1000}
}

# Цветовая схема
COLORS = {
    'background': '#1a1a2e',
    'text_primary': '#ffffff',
    'text_secondary': '#a0a0a0',
    'bar_background': '#2d2d44',
    'excellent': '#00d4aa',
    'good': '#a8e063',
    'normal': '#ffd93d',
    'low': '#ff8c42',
    'critical': '#ff4757',
    'spider_line': '#4ecdc4',
    'spider_fill': '#4ecdc44d',
    'green_zone': '#00d4aa',
    'yellow_zone': '#ffd93d',
    'red_zone': '#ff4757'
}

# Настройки Android интеграции
ANDROID_DATA_PATH = "/storage/emulated/0/HRV_Monitor/data"  # Путь на Android устройстве
LOCAL_ANDROID_SYNC_PATH = BASE_DIR / "android_sync"  # Локальная папка для синхронизации

# Контекст для формулировок (cognitive | physical | universal)
CONTEXT_MODE = "cognitive"
