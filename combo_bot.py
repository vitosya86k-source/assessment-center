"""
combo_bot.py — КОМБО-бот @assessment_center_analyzer_bot (Направление 2).

Отдельный бот (не пульсовой @HRV_monitor_bot). Принимает видео (или аудио),
гоняет оффлайн-разбор: эмоции (+поза/пульс/речь по мере подключения модулей),
копит результаты по участникам и присылает сводку + файлы.

Запуск:  ./start_combo_bot.sh    (venv_new, токен из combo_config / COMBO_BOT_TOKEN)

Это «база» (по договорённости: реализуем → Codex ревьюит → проверяем). Live-режимы
(оверлей поверх браузера, real-time, audio-only по речи) — следующие модули; здесь
заложен оффлайн-конвейер «любое видео → единый отчёт», на который они лягут.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          filters, ContextTypes)

import combo_config as cfg
import combo_analyze
import ac_engine

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler(cfg.LOGS_DIR / "combo_bot.log", encoding="utf-8"),
              logging.StreamHandler()],
)
logger = logging.getLogger("combo_bot")

WELCOME = (
    "🎛 *Анализ участника ассессмента*\n\n"
    "📱 *Live в моменте:*\n"
    "• */analyze* — камера на человека или на экран (ноут/телефон с видео): речь, эмоции, поза, пульс + CSV\n"
    "• */overlay* — накладка: камера + HUD + PiP поверх Гранатума в браузере\n"
    "• */dashboard* — панель: analyze, overlay, история сессий\n\n"
    "📎 Или пришли *видеозапись* — разберу офлайн: пульс, эмоции, поза, речь + "
    "*состояние* (стресс, утомление, вовлечённость, стрессоустойчивость), давление, "
    "зажимы/SpO₂ и карточки-итоги. Верну сводку + CSV.\n\n"
    "Команды: /analyze, /overlay, /dashboard, /live, /help, /status"
)

HELP = (
    "*Как пользоваться*\n\n"
    "📱 */analyze* — камера / файл / ссылка. Наводишь на участника или экран с видео — "
    "речь, эмоции, поза, пульс. Запись сессии в CSV.\n\n"
    "📲 */overlay* — *накладка на телефоне*: камера + HUD, PiP 📌 поверх Гранатума, CSV.\n\n"
    "📊 */dashboard* — панель АЦ: ссылки на режимы + история сессий на устройстве.\n\n"
    "📎 *Видеозапись* — пришли файл (mp4/mov) или видео-сообщение, подпись = метка участника. "
    "Прогоню полный движок (эмоции/поза/пульс/речь + wellness + состояние Neiry: стресс, "
    "утомление, вовлечённость, стрессоустойчивость, давление) и верну сводку с итогами + CSV.\n\n"
    "Если лица нет (наушники/очки/только аудио) — визуальные каналы честно помечаются недоступными, бот не падает."
)


def _miniapp_kb(label: str, page: str) -> tuple[str, InlineKeyboardMarkup]:
    """Ссылка на мини-апп: сначала браузер (надёжно), потом Web App в Telegram."""
    url = f"{cfg.COMBO_MINIAPP_URL}/{page}?cb={int(time.time())}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌐 {label}", url=url)],
        [InlineKeyboardButton("📲 В Telegram (мини-апп)", web_app=WebAppInfo(url=url))],
    ])
    return url, kb


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown(WELCOME)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown(HELP)


async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Мини-апка live-анализа по видео (камера / файл / ссылка, тап-выбор человека) как Telegram Web App."""
    if not cfg.COMBO_MINIAPP_URL:
        await update.message.reply_text(
            "⚠️ Мини-апка не настроена: задай COMBO_MINIAPP_URL (HTTPS-база, где лежит "
            "phone_analyze.html) в .env."
        )
        return
    url, kb = _miniapp_kb("📷 Открыть анализ", "phone_analyze.html")
    await update.message.reply_text(
        "📷 Live-анализ по видео — Камера / Файл / Ссылка\n\n"
        "Нажми верхнюю кнопку (браузер) — надёжнее для камеры.\n"
        "Открой → тапни по человеку → метрики вживую.\n"
        "⚠️ Старые кнопки в чате не обновляются — всегда шли команду заново.",
        reply_markup=kb,
    )


async def cmd_overlay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Мобильная накладка: камера + HUD поверх картинки (не захват вкладки ПК)."""
    if not cfg.COMBO_MINIAPP_URL:
        await update.message.reply_text("⚠️ Задай COMBO_MINIAPP_URL (HTTPS) в .env.")
        return
    _, kb = _miniapp_kb("📲 Накладка (камера)", "mobile_overlay.html")
    await update.message.reply_text(
        "📲 Накладка на телефоне\n\n"
        "Верхняя кнопка — открыть в Safari/Chrome (лучше для PiP и камеры).\n"
        "● запись → 📌 PiP → 🌐 Гранатум.\n"
        "⚠️ Старые кнопки в чате не обновляются — шли /overlay заново.",
        reply_markup=kb,
    )


async def cmd_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Панель АЦ: analyze, overlay, история сессий на устройстве."""
    if not cfg.COMBO_MINIAPP_URL:
        await update.message.reply_text(
            "📊 Дашборд паутинки HRV — у @HRV_monitor_bot (/dashboard там).\n\n"
            "У АЦ: /analyze, /overlay, видео в бот. Для панели задай COMBO_MINIAPP_URL.",
        )
        return
    _, kb = _miniapp_kb("📊 Панель АЦ", "ac_hub.html")
    await update.message.reply_text(
        "📊 Панель АЦ\n\n"
        "Live-анализ, накладка с PiP для Гранатума, история CSV-сессий на этом телефоне.\n\n"
        "Паутинку HRV — у @HRV_monitor_bot (/dashboard там).",
        reply_markup=kb,
    )


async def cmd_live(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Живая панель участника (речь/эмоции/поза/пульс) как Telegram Web App."""
    if not cfg.COMBO_MINIAPP_URL:
        await update.message.reply_text("⚠️ Панель не настроена: задай COMBO_MINIAPP_URL (HTTPS) в .env.")
        return
    _, kb = _miniapp_kb("📊 Открыть панель", "combo_live.html")
    await update.message.reply_text(
        "📊 Живая панель участника (речь / эмоции / поза / пульс).\n"
        "Данные идут, когда на ноуте запущен ./start_ac_live.sh (захват экрана Гранатума).\n"
        "Открывается и на телефоне (тут), и на ноуте в браузере.",
        reply_markup=kb,
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    results = sorted(cfg.RESULTS_DIR.glob("*/summary.json"))
    incoming = list(cfg.INCOMING_DIR.glob("*"))
    avail = []
    for key, script, _py in combo_analyze._MODULES:
        avail.append(f"{'✅' if (cfg.BASE_DIR / script).exists() else '⬜'} {key}")
    await update.message.reply_markdown(
        f"*Статус комбо-бота*\n"
        f"Разборов сохранено: {len(results)}\n"
        f"Файлов в очереди: {len(incoming)}\n"
        f"Модули: " + ", ".join(avail)
    )


def _format_ac_reply(result: dict, label: str) -> str:
    """Человекочитаемая сводка из движка ac_engine: итоги + состояние + тело + wellness."""
    m = result.get("metrics", {}) or {}
    ni = (result.get("state", {}) or {}).get("neiry", {}) or {}
    cards = (result.get("state", {}) or {}).get("cards", []) or []
    well = result.get("wellness", {}) or {}
    pulse = m.get("pulse", {}) or {}
    bp = m.get("bp", {}) or {}
    spo2 = m.get("spo2", {}) or {}

    lines = [f"🎛 Разбор участника: {label or '—'}", f"Режим: {result.get('mode', '—')}", ""]

    if cards:
        lines.append("📝 Итоги:")
        lines += [f"• {c.get('label')}: {c.get('text')}" for c in cards]
        lines.append("")

    nrow = []
    for key, name in (("stress", "стресс"), ("fatigue", "утомление"),
                      ("engagement", "вовлечённость")):
        if ni.get(key) is not None:
            nrow.append(f"{name} {ni[key]}")
    if nrow:
        lines.append("📊 Состояние: " + " · ".join(nrow))
        if ni.get("verdict"):
            lines.append(f"  {ni['verdict']}")
        lines.append("")

    brow = []
    if pulse.get("hr_median"):
        brow.append(f"пульс {pulse['hr_median']} уд/мин")
    if bp.get("available") and bp.get("sbp"):
        brow.append(f"давление ~{bp['sbp']}/{bp['dbp']}")
    if spo2.get("available") and spo2.get("spo2"):
        brow.append(f"SpO₂ ~{spo2['spo2']}%")
    if brow:
        lines.append("❤️ Тело: " + " · ".join(brow))
        lines.append("")

    if well.get("narrative"):
        lines.append("💬 " + well["narrative"])
        lines.append("")

    lines.append("Оценки по видео — маркеры состояния, не медизмерение.")
    return "\n".join(lines)


async def _process_video(update: Update, file_obj, label: str, transcript: str | None = None):
    msg = update.message
    INCOMING = cfg.INCOMING_DIR
    suffix = Path(file_obj.file_name).suffix if getattr(file_obj, "file_name", None) else ".mp4"
    dest = INCOMING / f"{msg.chat_id}_{msg.message_id}{suffix}"

    await msg.reply_text("📥 Принял, скачиваю…")
    tg_file = await file_obj.get_file()
    await tg_file.download_to_drive(custom_path=str(dest))

    tnote = " + транскрипт (контент-анализ)" if transcript else ""
    await msg.reply_text(f"🔬 Анализирую ({label or 'без метки'}){tnote}… это может занять время.")
    try:
        # движок ac_engine: combo (эмоции/поза/пульс/речь) + wellness + Neiry-композиты
        result = await asyncio.to_thread(ac_engine.analyze_ac_clip, dest,
                                         transcript=transcript, speaker="Спикер 0",
                                         name=label or "Участник", label=label)
    except Exception as e:
        logger.exception("Ошибка анализа")
        await msg.reply_text(f"⚠️ Ошибка анализа: {e}")
        return

    # Telegram markdown капризен — шлём как обычный текст, чтобы не падать на спецсимволах
    await msg.reply_text(_format_ac_reply(result, label)[:4000])

    workdir = result.get("workdir")
    if workdir:
        wd = Path(workdir)
        for csv_file in sorted(wd.glob("*.csv")):
            try:
                await msg.reply_document(document=open(csv_file, "rb"), filename=csv_file.name)
            except Exception:
                pass
        for name in ("report.md", "report.docx", "content.md", "report_rich.md", "report_rich.docx"):
            f = wd / name
            if f.exists():
                try:
                    await msg.reply_document(document=open(f, "rb"), filename=name)
                except Exception:
                    pass


async def on_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    label = (msg.caption or "").strip()
    file_obj = msg.video or (msg.document if msg.document else None)
    if file_obj is None:
        await msg.reply_text("Не вижу видео в сообщении.")
        return
    transcript = ctx.user_data.pop("transcript", None)
    await _process_video(update, file_obj, label, transcript)


async def on_transcript(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Приём транскрипта (.docx/.txt) — применяется к следующему видео (контент-анализ)."""
    msg = update.message
    doc = msg.document
    dest = cfg.INCOMING_DIR / f"transcript_{msg.chat_id}_{msg.message_id}_{doc.file_name}"
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(custom_path=str(dest))
    ctx.user_data["transcript"] = str(dest)
    await msg.reply_text(
        "📝 Транскрипт принят. Пришли видео этого участника — добавлю контент-анализ речи "
        "(MBTI, OCEAN, радикалы, дисфлюенции…) и богатый отчёт.\n\n"
        "⚠️ Формат строк: `[Ч:ММ:СС] Спикер 0: текст` (с таймштампом). Участник по умолчанию — «Спикер 0»."
    )


async def on_audio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Чистое аудио (без видео) — речевой анализ: F0, громкость, темп, паузы, E/I."""
    msg = update.message
    obj = msg.audio or msg.voice or msg.document
    if obj is None:
        await msg.reply_text("Не вижу аудио.")
        return
    ext = Path(getattr(obj, "file_name", "") or "").suffix or ".ogg"
    dest = cfg.INCOMING_DIR / f"audio_{msg.chat_id}_{msg.message_id}{ext}"
    await msg.reply_text("🎧 Аудио принято, считаю речевые признаки…")
    tg_file = await obj.get_file()
    await tg_file.download_to_drive(custom_path=str(dest))
    try:
        import analyze_video_rppg as avr
        sp = await asyncio.to_thread(avr.analyze_speech, str(dest))
    except Exception as e:
        await msg.reply_text(f"⚠️ Ошибка речевого анализа: {e}")
        return
    if not sp.get("available"):
        await msg.reply_text(f"Речь не выделилась: {sp.get('note') or sp.get('error') or '—'}")
        return
    await msg.reply_text(
        "🗣 *Речевой разбор (audio-only)*\n"
        f"• Доля речи: {round(sp['speech_ratio']*100)} %\n"
        f"• Питч F0: {sp['pitch_mean_hz']}±{sp['pitch_std_hz']} Гц\n"
        f"• Темп: {sp['tempo_onsets_per_min']}/мин, паузы>1с: {sp['pauses_over_1s']}\n"
        f"• Динам. диапазон громкости: {sp['loudness_iqr_db']} дБ\n"
        f"• E/I: {sp['ei_label']} (score {sp['ei_score']})",
        parse_mode="Markdown",
    )


def build_app() -> Application:
    app = Application.builder().token(cfg.require_token()).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("overlay", cmd_overlay))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("live", cmd_live))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.AUDIO, on_audio))
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("docx") | filters.Document.FileExtension("txt"), on_transcript))
    return app


def main():
    app = build_app()
    logger.info("Комбо-бот запущен (@assessment_center_analyzer_bot)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
