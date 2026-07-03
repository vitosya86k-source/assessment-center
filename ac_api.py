"""Серверный endpoint телефонного АЦ: POST /ac/analyze (Ветка 2).

Тонкий клиент (телефон/браузер) снимает клип участника и шлёт СЮДА; сервер гоняет
движок ac_engine (combo + wellness + Neiry) и возвращает единый JSON: метрики + состояние
+ человекочитаемые итоги. Ровно архитектура из wellness/CURSOR_HANDOFF_PHONE_AC.md.

Railway sleep: сервис засыпает при простое и просыпается по первому запросу — та же схема,
что уже отработана на пульс-боте (focused-clarity / HRV_backend). Это конфиг Railway,
не код: Settings → Serverless / App Sleeping → On.

Запуск локально:  venv_new/bin/uvicorn ac_api:app --port 8800
Деплой: отдельный Railway-сервис с тяжёлым стеком (см. requirements-ac.txt и DEPLOY-заметку
внизу). combo венвы на сервере задаются через env COMBO_EMO_PYTHON / COMBO_MAIN_PYTHON.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from ac_engine import analyze_ac_clip

app = FastAPI(title="AC Analyze API", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}


@app.get("/")
def root():
    return {"service": "AC Analyze API", "version": "1.0.0", "status": "running"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/ac/analyze")
async def ac_analyze(
    video: UploadFile = File(..., description="клип участника (mp4/mov/webm)"),
    transcript: UploadFile | None = File(None, description="опц. .docx транскрипт для типологии"),
    speaker: str = Form("Спикер 0"),
    name: str | None = Form(None),
    label: str = Form(""),
):
    """Принимает клип → прогоняет combo+wellness → единый ответ движка.

    Синхронный разбор (клип короткий). Для длинных — вынести в фоновую задачу + polling,
    но для замера участника в моменте хватает прямого ответа.
    """
    suffix = Path(video.filename or "clip.mp4").suffix.lower() or ".mp4"
    if suffix not in _VIDEO_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"неподдерживаемый формат видео: {suffix}")

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
            result = analyze_ac_clip(
                vpath, transcript=str(tpath) if tpath else None,
                speaker=speaker, name=name, label=label,
            )
        except Exception as e:  # движок сам ловит по-модульно; это на самый крайний случай
            raise HTTPException(status_code=500, detail=f"разбор упал: {type(e).__name__}: {e}")

        return result
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8800)))
