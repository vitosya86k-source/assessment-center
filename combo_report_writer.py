"""
combo_report_writer.py — богатый ассессмент-отчёт через `claude` CLI (паттерн FS).

ВАЖНО (CLAUDE.md): никакого anthropic SDK. Только subprocess на подписочный CLI.
Собираем всё (поведение из combo + контент-анализ речи + опц. HRV) в бриф и просим
`claude -p` написать связный отчёт по участнику; затем → .docx через report_docx.

Если claude CLI недоступен/упал — честный fallback: склеиваем report.md + контент-md.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import combo_config as cfg

SYSTEM_BRIEF = """Ты пишешь профессиональный отчёт по участнику ассессмент-центра.
Тебе дают: (1) поведенческие сигналы по видео (речь, эмоции, поза, пульс) и
(2) контент-анализ речи (MBTI, OCEAN, радикалы, темперамент, дисфлюенции, стресс/coping).
Напиши цельный отчёт ПРО ЭТОГО человека: портрет, сильные стороны, риски, как проявлялся
в упражнениях, связь физиологии (пульс/напряжение) с поведением. Пиши по-русски, конкретно,
без воды и без пересказа сырых чисел — интерпретируй. Не выдумывай фактов сверх данных.
Структура: ## Портрет, ## Сильные стороны, ## Зоны риска, ## Поведение в моменте, ## Итог.
"""


def _claude_available() -> bool:
    return shutil.which("claude") is not None


def write_rich_report(behavior_md: str, content_md: str, out_md: str | Path,
                      name: str = "Участник", timeout: int = 240) -> dict:
    """Собирает бриф и зовёт claude CLI. Возвращает {ok, md_path, engine}."""
    out_md = Path(out_md)
    brief = (f"# Данные по участнику: {name}\n\n"
             f"## Поведенческие сигналы (видео-разбор)\n{behavior_md}\n\n"
             f"## Контент-анализ речи\n{content_md or '(транскрипт не приложен)'}\n")

    if _claude_available():
        try:
            proc = subprocess.run(
                ["claude", "-p", SYSTEM_BRIEF],
                input=brief, capture_output=True, text=True, timeout=timeout,
            )
            text = (proc.stdout or "").strip()
            if proc.returncode == 0 and len(text) > 200:
                out_md.write_text(text, encoding="utf-8")
                return {"ok": True, "md_path": str(out_md), "engine": "claude-cli"}
        except Exception:
            pass

    # Fallback без LLM: честная склейка
    merged = (f"# Отчёт по участнику: {name}\n\n_(собран без LLM — claude CLI недоступен)_\n\n"
              f"## Поведение\n{behavior_md}\n\n## Контент-анализ речи\n{content_md or '—'}\n")
    out_md.write_text(merged, encoding="utf-8")
    return {"ok": True, "md_path": str(out_md), "engine": "fallback"}
