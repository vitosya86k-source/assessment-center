"""
API сервер для приема данных от Android приложения
Работает на Railway вместе с ботом
"""
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import os
import pandas as pd
from datetime import datetime
import logging
from pathlib import Path

from config import DATA_DIR, TELEGRAM_BOT_TOKEN, HRV_API_URL
from telegram import Bot
import asyncio

from hrv_calculator import HRVCalculator
from hrv_calibration import calibration_info

logger = logging.getLogger(__name__)

app = FastAPI(title="NeuroHRV API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


LATEST_DIR = DATA_DIR / "latest"


def compute_calibrated_metrics(df: pd.DataFrame) -> dict:
    """Считает КАЛИБРОВАННЫЕ метрики из RR/HR того же калькулятора, что и бот.
    Так телефонный замер (Web Bluetooth) совпадает с Kubios, как ноутбучный BLE."""
    return compute_full(df)["metrics"]


def compute_full(df: pd.DataFrame) -> dict:
    """Полный расчёт для дашборда: метрики + 7 осей паутинки + общий балл."""
    hr_data = df['Heart_Rate_bpm'].tolist() if 'Heart_Rate_bpm' in df else []
    rr_data = df['RR_Interval_ms'].tolist() if 'RR_Interval_ms' in df else None
    calc = HRVCalculator(hr_data, rr_intervals=rr_data)
    metrics = calc.calculate_all_metrics()
    try:
        axes = calc.calculate_axis_scores(metrics)
        overall = calc.calculate_overall_score(axes)
    except Exception:
        axes, overall = {}, None
    return {"metrics": metrics, "axes": axes, "overall": overall}


def save_latest(user_id: int, full: dict, n_points: int):
    """Сохраняет последний результат пользователя для дашборда (/dashboard)."""
    try:
        LATEST_DIR.mkdir(parents=True, exist_ok=True)
        try:
            real_age = int(os.environ.get("USER_AGE", "0")) or None
        except ValueError:
            real_age = None
        rec = {
            "user_id": user_id,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "n_points": n_points,
            "real_age": real_age,
            "overall": full.get("overall"),
            "axes": full.get("axes", {}),
            "metrics": full.get("metrics", {}),
        }
        (LATEST_DIR / f"user_{user_id}.json").write_text(
            __import__("json").dumps(rec, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"latest не сохранён: {e}")


def format_metrics_message(metrics: dict, n_points: int, device: str | None) -> str:
    """Короткая сводка для пользователя в Telegram (калиброванные значения)."""
    def g(k, fmt="{:.1f}"):
        v = metrics.get(k)
        return fmt.format(v) if isinstance(v, (int, float)) else "—"
    bio = metrics.get('biological_age')
    bio_s = f"\n🎂 Био-возраст: {bio} лет" if bio else ""
    return (
        f"✅ Замер с телефона получен и посчитан!\n\n"
        f"❤️ ЧСС средн.: {g('mean_hr')} уд/мин\n"
        f"📈 SDNN: {g('sdnn')} мс  |  RMSSD: {g('rmssd')} мс\n"
        f"😌 PNS index: {g('pns_index','{:+.2f}')}  |  😣 SNS index: {g('sns_index','{:+.2f}')}\n"
        f"⚡ Индекс стресса: {g('stress_index','{:.0f}')}"
        f"{bio_s}\n\n"
        f"📊 Точек RR: {n_points} · {device or 'Polar Verity'}\n\n"
        f"Используйте /analyze для дашбордов и /export для выгрузки."
    )

# Модели данных
class HRDataPoint(BaseModel):
    heart_rate_bpm: float
    timestamp_iso: Optional[str] = None
    rr_interval_ms: Optional[float] = None

class HRVDataRequest(BaseModel):
    user_id: int
    data: List[HRDataPoint]
    device_name: Optional[str] = None

# Telegram бот для отправки уведомлений
telegram_bot = None

async def init_telegram_bot():
    """Инициализация Telegram бота"""
    global telegram_bot
    if telegram_bot is None:
        telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
    return telegram_bot

@app.on_event("startup")
async def startup_event():
    """Инициализация при запуске"""
    await init_telegram_bot()
    logger.info("✅ API сервер запущен")

@app.get("/")
async def root():
    """Корневой endpoint"""
    return {
        "service": "NeuroHRV API",
        "version": "1.0.0",
        "status": "running"
    }

@app.get("/health")
async def health_check():
    """Проверка здоровья сервиса"""
    return {"status": "healthy"}


# Раздаём саму страницу замера (Web Bluetooth) с того же origin, что и бэкенд —
# один туннель отдаёт И страницу, И /api. Web Bluetooth требует HTTPS (туннель даёт).
WEBAPP_FILE = Path(os.getenv(
    "HRV_WEBAPP_FILE",
    str(Path(__file__).resolve().parents[1] / "polar_webapp_fixed.html")
))


@app.get("/polar_webapp_fixed.html")
@app.get("/measure")
async def serve_webapp():
    if WEBAPP_FILE.exists():
        return FileResponse(str(WEBAPP_FILE), media_type="text/html",
                            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})
    raise HTTPException(status_code=404, detail=f"webapp не найден: {WEBAPP_FILE}")


DASHBOARD_FILE = Path(os.getenv(
    "HRV_DASHBOARD_FILE",
    str(Path(__file__).resolve().parents[1] / "hrv_dashboard.html")
))


_NOCACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


@app.get("/dashboard")
async def serve_dashboard():
    if DASHBOARD_FILE.exists():
        return FileResponse(str(DASHBOARD_FILE), media_type="text/html", headers=_NOCACHE)
    raise HTTPException(status_code=404, detail=f"дашборд не найден: {DASHBOARD_FILE}")


@app.get("/api/v1/latest")
async def latest_metrics(uid: int):
    """Последний посчитанный результат пользователя — для дашборда."""
    f = LATEST_DIR / f"user_{uid}.json"
    if not f.exists():
        raise HTTPException(status_code=404, detail="нет данных — сделай замер")
    import json as _json
    return JSONResponse(_json.loads(f.read_text(encoding="utf-8")))

@app.post("/api/v1/hrv-data")
async def receive_hrv_data(request: HRVDataRequest):
    """
    Прием данных HRV от Android приложения
    
    Android приложение отправляет данные сюда после сбора с Polar устройства
    """
    try:
        user_id = request.user_id
        data_points = request.data
        
        if not data_points:
            raise HTTPException(status_code=400, detail="Нет данных для обработки")
        
        logger.info(f"📥 Получены данные от пользователя {user_id}: {len(data_points)} точек")
        
        # Конвертируем в DataFrame
        data_list = []
        for point in data_points:
            timestamp = point.timestamp_iso or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rr = point.rr_interval_ms
            if rr is None:
                rr = 60000.0 / point.heart_rate_bpm
            
            data_list.append({
                'Heart_Rate_bpm': point.heart_rate_bpm,
                'Timestamp_ISO': timestamp,
                'RR_Interval_ms': rr
            })
        
        df = pd.DataFrame(data_list)
        
        # Сохраняем данные
        data_file = DATA_DIR / f"user_{user_id}_latest.csv"
        df.to_csv(data_file, index=False)
        
        logger.info(f"✅ Данные сохранены: {data_file}")
        
        # Считаем КАЛИБРОВАННЫЕ метрики + оси паутинки (тот же путь, что у бота)
        metrics, full = {}, {}
        try:
            full = compute_full(df)
            metrics = full.get("metrics", {})
        except Exception as e:
            logger.warning(f"Не удалось посчитать метрики: {e}")

        # Guard: короткий замер не должен выдавать «надёжные» метрики.
        # SDNN/RMSSD требуют 2–3 мин непрерывного RR (≥~120 интервалов).
        n = len(data_points)
        warn = ""
        if n < 60:
            warn = (f"⚠️ Замер слишком короткий — {n} RR-интервалов.\n"
                    f"Для надёжных метрик нужно 2–3 минуты непрерывного RR-стрима. ")
            if n < 20:
                warn += "Метрики не считаю — повтори замер подольше.\n\n"
                metrics = {}  # не показываем мусорные цифры на горстке точек
            else:
                warn += "Значения ниже — ОРИЕНТИРОВОЧНЫЕ.\n\n"

        # Сохраняем последний результат для дашборда
        dash_link = ""
        if metrics:
            save_latest(user_id, full, n)
            base = HRV_API_URL or os.getenv("HRV_PUBLIC_URL", "")
            if base:
                dash_link = f"\n\n📊 Дашборд: {base.rstrip('/')}/dashboard?uid={user_id}"

        # Отправляем результат пользователю в Telegram
        try:
            bot = await init_telegram_bot()
            if metrics:
                message = warn + format_metrics_message(metrics, n, request.device_name) + dash_link
            else:
                message = warn or f"✅ Данные получены ({n} точек). Используйте /analyze"
            await bot.send_message(chat_id=user_id, text=message)
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление: {e}")
        
        return {
            "status": "success",
            "message": "Данные успешно получены и сохранены",
            "data_points": len(data_points),
            "metrics": {k: metrics[k] for k in ('sdnn', 'rmssd', 'pns_index', 'sns_index', 'stress_index', 'biological_age') if k in metrics},
            "file": str(data_file)
        }
        
    except Exception as e:
        logger.error(f"❌ Ошибка при обработке данных: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/hrv-data-simple")
async def receive_hrv_data_simple(
    user_id: int,
    heart_rates: List[float],
    timestamps: Optional[List[str]] = None,
    device_name: Optional[str] = None
):
    """
    Упрощенный endpoint для приема данных
    
    Параметры:
    - user_id: ID пользователя в Telegram
    - heart_rates: Список значений HR
    - timestamps: Список временных меток (опционально)
    - device_name: Название устройства (опционально)
    """
    try:
        if not heart_rates:
            raise HTTPException(status_code=400, detail="Нет данных HR")
        
        # Создаем временные метки если нет
        if not timestamps:
            from datetime import timedelta
            start_time = datetime.now()
            timestamps = [
                (start_time - timedelta(seconds=len(heart_rates)-i)).strftime("%Y-%m-%d %H:%M:%S")
                for i in range(len(heart_rates))
            ]
        
        # Создаем DataFrame
        df = pd.DataFrame({
            'Heart_Rate_bpm': heart_rates,
            'Timestamp_ISO': timestamps,
            'RR_Interval_ms': [60000.0 / hr for hr in heart_rates]
        })
        
        # Сохраняем
        data_file = DATA_DIR / f"user_{user_id}_latest.csv"
        df.to_csv(data_file, index=False)
        
        logger.info(f"✅ Данные сохранены: {len(heart_rates)} точек")
        
        # Уведомление
        try:
            bot = await init_telegram_bot()
            await bot.send_message(
                chat_id=user_id,
                text=f"✅ Получено {len(heart_rates)} точек данных. Используйте /analyze"
            )
        except:
            pass
        
        return {
            "status": "success",
            "data_points": len(heart_rates)
        }
        
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
