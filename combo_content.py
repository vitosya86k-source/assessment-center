"""
combo_content.py — подключение контент-анализа речи (12 модулей) к комбо-разбору.

Если к видео приложен транскрипт (.docx), гоняем analysis/analyze_participant.py
(MBTI, OCEAN, Павлов, радикалы, Dark Tetrad, дисфлюенции, STAR и т.д.) с rPPG-логом
комбо-разбора и получаем markdown-раздел. Запуск — subprocess в venv_new (тяжёлые
зависимости pymorphy2/transformers там).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import combo_config as cfg

ANALYZER = cfg.BASE_DIR / "analysis" / "analyze_participant.py"


def run_content_analysis(transcript_docx: str | Path, rppg_csv: str | Path | None,
                         speaker: str = "Спикер 0", name: str = "Участник",
                         out_md: str | Path | None = None) -> dict:
    """Гоняет analyze_participant.py. Возвращает {ok, md_path, text|error}."""
    transcript_docx = Path(transcript_docx)
    if not ANALYZER.exists():
        return {"ok": False, "error": "нет analysis/analyze_participant.py"}
    if not transcript_docx.exists():
        return {"ok": False, "error": f"нет транскрипта: {transcript_docx}"}

    out_md = Path(out_md) if out_md else transcript_docx.with_suffix(".content.md")
    cmd = [cfg.MAIN_PYTHON, str(ANALYZER), "--docx", str(transcript_docx),
           "--speaker", speaker, "--name", name, "--out", str(out_md)]
    if rppg_csv and Path(rppg_csv).exists():
        cmd += ["--log", str(rppg_csv)]
    try:
        proc = subprocess.run(cmd, cwd=str(cfg.BASE_DIR), capture_output=True,
                              text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "контент-анализ: timeout"}
    if out_md.exists():
        return {"ok": True, "md_path": str(out_md), "text": out_md.read_text(encoding="utf-8")}
    return {"ok": False, "error": (proc.stderr or proc.stdout or "нет вывода")[-600:]}
