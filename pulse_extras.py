"""
pulse_extras.py — доп-команды пульсового бота @HRV_monitor_bot.

Подключается из telegram_bot.py одной строкой register(application) и НЕ трогает
рабочую BLE/PMD-логику. Добавляет:
  /metric [имя]     — описания HRV-метрик (из HRV_METRICS_REFERENCE)
  /mark <метка>     — отметка сегмента во время длительной записи (упражнение/пауза/нагрузка)
  /segments         — показать отметки текущей сессии
  /export_xlsx      — выгрузка Excel (.xlsx): метрики + сырые RR + сегменты
  /dashboard        — собрать данные для веб-дашборда (webapp/hrv.html)
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pandas as pd
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from config import DATA_DIR, EXPORTS_DIR
from hrv_calculator import HRVCalculator

# Краткие описания метрик (по HRV_METRICS_REFERENCE.md)
METRICS = {
    "sdnn": ("SDNN", "Общая вариабельность (откалибровано под Kubios). Норма 50–100 мс. "
                     "<30 — низкая адаптация, >100 — отличная."),
    "rmssd": ("RMSSD", "Активность восстановления / парасимпатика. Норма 25–40 мс. "
                       "<15 — сильное напряжение, >40 — отличное восстановление."),
    "pnn50": ("pNN50", "Гибкость нервной системы. Норма 10–20 %. <3 % — низкая."),
    "mean_rr": ("Mean RR", "Средний интервал между ударами. Норма 600–1200 мс (50–100 уд/мин)."),
    "sd1": ("SD1", "Краткосрочная вариабельность (≈RMSSD). Норма 25–50 мс."),
    "sd2": ("SD2", "Долгосрочная вариабельность. Норма 100–150 мс."),
    "lf_hf_ratio": ("LF/HF", "Баланс симпатики/парасимпатики. Норма 0.5–2.0. >4 — высокий стресс."),
    "stress_index": ("Stress Index", "Индекс напряжения Баевского. Норма 50–150. >400 — истощение."),
    "pns_index": ("PNS Index", "Kubios: парасимпатика (восстановление). 0 — здоровый взрослый, +выше — отдых."),
    "sns_index": ("SNS Index", "Kubios: симпатика (мобилизация). 0 — норма, +выше — стресс/нагрузка."),
    "biological_age": ("Bio-age", "Функциональный возраст по HRV (нужен реальный возраст, USER_AGE)."),
}


def _marks_path(uid: int) -> Path:
    return DATA_DIR / f"user_{uid}_marks.csv"


def _latest_csv(uid: int) -> Path:
    local = DATA_DIR / f"user_{uid}_latest.csv"
    if local.exists():
        return local
    from config import HRV_API_URL

    api = (HRV_API_URL or "").rstrip("/")
    if api:
        try:
            import requests

            r = requests.get(f"{api}/api/v1/raw", params={"uid": uid}, timeout=30)
            if r.status_code == 200 and r.content:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                local.write_bytes(r.content)
                return local
        except Exception:
            pass
    return local


async def cmd_metric(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        lines = ["📖 *Метрики HRV* — /metric <имя> для подробностей:", ""]
        lines += [f"• `{k}` — {v[0]}" for k, v in METRICS.items()]
        await update.message.reply_markdown("\n".join(lines))
        return
    key = args[0].lower().replace("/", "")
    aliases = {"lf/hf": "lf_hf_ratio", "lfhf": "lf_hf_ratio", "bioage": "biological_age",
               "pns": "pns_index", "sns": "sns_index", "stress": "stress_index"}
    key = aliases.get(key, key)
    if key not in METRICS:
        await update.message.reply_text("Не знаю такую метрику. /metric — список.")
        return
    name, desc = METRICS[key]
    await update.message.reply_markdown(f"*{name}*\n{desc}")


async def cmd_mark(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    label = " ".join(ctx.args).strip() or "метка"
    p = _marks_path(uid)
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        if new:
            wr.writerow(["timestamp_iso", "label"])
        wr.writerow([datetime.now().isoformat(timespec="seconds"), label])
    await update.message.reply_text(
        f"📍 Отметка: «{label}» ({datetime.now().strftime('%H:%M:%S')}).\n"
        f"Так размечают упражнение / паузу / нагрузку. /segments — список, /export_xlsx — выгрузка."
    )


async def cmd_segments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = _marks_path(uid)
    if not p.exists():
        await update.message.reply_text("Отметок пока нет. Во время записи: /mark <метка>.")
        return
    df = pd.read_csv(p)
    lines = ["🗂 *Сегменты сессии:*"]
    for _, r in df.iterrows():
        t = str(r["timestamp_iso"]).split("T")[-1]
        lines.append(f"• {t} — {r['label']}")
    await update.message.reply_markdown("\n".join(lines))


def _build_metrics_rows(metrics: dict) -> list[list]:
    rows = [["Метрика", "Значение", "Норма"]]
    for k, (name, desc) in METRICS.items():
        if metrics.get(k) is None:
            continue
        norm = desc.split("Норма")[1].split(".")[0].strip(" :") if "Норма" in desc else ""
        rows.append([name, round(float(metrics[k]), 2), norm])
    return rows


async def cmd_export_xlsx(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    data_file = _latest_csv(uid)
    if not data_file.exists():
        await update.message.reply_text("❌ Нет данных. Сначала измерение или пришлите CSV.")
        return
    await update.message.reply_text("📊 Готовлю Excel…")
    data = pd.read_csv(data_file)
    hr = data["Heart_Rate_bpm"].tolist() if "Heart_Rate_bpm" in data.columns else []
    rr = data["RR_Interval_ms"].tolist() if "RR_Interval_ms" in data.columns else None
    calc = HRVCalculator(hr, rr_intervals=rr)
    metrics = calc.calculate_all_metrics()
    axes = calc.calculate_axis_scores(metrics, freq_valid=True)
    overall = calc.calculate_overall_score(axes)

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = EXPORTS_DIR / f"hrv_{uid}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xl:
        # Метрики
        mrows = _build_metrics_rows(metrics)
        pd.DataFrame(mrows[1:], columns=mrows[0]).to_excel(xl, sheet_name="Метрики", index=False)
        # 7 осей + общий балл
        ax = pd.DataFrame([{"Ось": k, "Балл": v} for k, v in axes.items()] +
                          [{"Ось": "ОБЩИЙ", "Балл": overall}])
        ax.to_excel(xl, sheet_name="Оси", index=False)
        # Сырые данные
        data.to_excel(xl, sheet_name="Сырые RR", index=False)
        # Сегменты
        mp = _marks_path(uid)
        if mp.exists():
            pd.read_csv(mp).to_excel(xl, sheet_name="Сегменты", index=False)
    await update.message.reply_document(document=open(out, "rb"), filename=out.name,
                                        caption=f"Общий балл: {overall}/100")


async def cmd_trends(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Динамика длинной записи: сегменты по /mark (или окна), тренды, история."""
    uid = update.effective_user.id
    data_file = _latest_csv(uid)
    if not data_file.exists():
        await update.message.reply_text("❌ Нет записи. Сначала длительное измерение.")
        return
    window = 300
    if ctx.args:
        try:
            window = max(30, int(ctx.args[0]))
        except ValueError:
            pass
    await update.message.reply_text("📈 Считаю динамику по сегментам…")
    try:
        import hrv_session
        marks = _marks_path(uid)
        res = await __import__("asyncio").to_thread(
            hrv_session.analyze_long_recording, str(data_file),
            str(marks) if marks.exists() else None, "Участник", uid,
            str(EXPORTS_DIR / "history"), window)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка анализа динамики: {e}")
        return
    report = res["report_md"]
    # отчётом-файлом, чтобы не упереться в лимит сообщения
    out = EXPORTS_DIR / f"trends_{uid}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out.write_text(report, encoding="utf-8")
    await update.message.reply_document(document=open(out, "rb"), filename=out.name,
                                        caption=f"Сегментов: {len(res['segments'])}. "
                                                f"Метки — командой /mark во время записи.")


async def cmd_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    data_file = _latest_csv(uid)
    if not data_file.exists():
        await update.message.reply_text(
            "❌ Нет данных. Сделай замер: /measure_phone (с телефона) "
            "или /start_measurement (на ПК)."
        )
        return
    try:
        import pandas as pd
        import api_server  # тот же расчёт + сохранение latest, что у телефонного бэкенда
        from config import HRV_API_URL
        df = pd.read_csv(data_file)
        full = api_server.compute_full(df)
        api_server.save_latest(uid, full, len(df))  # чтобы открылся ngrok-дашборд

        src = str(df["RR_Source"].iloc[0]) if "RR_Source" in df.columns else "?"
        warn = ""
        if src == "derived":
            warn = ("\n\n⚠️ RR взяты из ЧСС (derived), НЕ с PMD — RMSSD/SDNN "
                    "ненадёжны, это не настоящая ВСР. Для точных метрик: "
                    "/measure_phone или /start_measurement (Verity → PMD).")
        link = (f"\n📊 Открыть на телефоне: {HRV_API_URL}/dashboard?uid={uid}"
                if HRV_API_URL else
                "\n(публичный дашборд не настроен: задай HRV_API_URL в .env)")
        await update.message.reply_text(
            f"📈 Дашборд: {round(full.get('overall') or 0)}/100{link}{warn}"
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Не удалось собрать дашборд: {e}")


def register(application):
    application.add_handler(CommandHandler("metric", cmd_metric))
    application.add_handler(CommandHandler("mark", cmd_mark))
    application.add_handler(CommandHandler("segments", cmd_segments))
    application.add_handler(CommandHandler("export_xlsx", cmd_export_xlsx))
    application.add_handler(CommandHandler("trends", cmd_trends))
    application.add_handler(CommandHandler("dashboard", cmd_dashboard))
