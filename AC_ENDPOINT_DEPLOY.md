# Телефонный АЦ — серверный endpoint (Ветка 2): что готово и как деплоить

Обновлено 02.07.2026. Ядро написано и проверено локально; деплой + подключение бота — дальше.

## Что готово (в `NEW VERSION/`)

| Файл | Роль | Статус |
|---|---|---|
| `ac_engine.py` | движок: клип → combo (эмоции/поза/пульс/речь) + wellness (глаза/зажимы/SpO2/голос/давление) + Neiry → единый dict | ✅ проверен: импорт, юнит-склейка, end-to-end на видео |
| `ac_api.py` | FastAPI: `POST /ac/analyze` (upload клипа) + `GET /health` | ✅ smoke: HTTP 200, полный JSON |
| `requirements-ac.txt` | тяжёлый стек endpoint'а | ✅ |
| правка `analyze_video_emotions.py` | добавил mean `valence`/`arousal` в summary (нужны Neiry-стрессу offline) | ✅ |

**Проверено:** `venv_new/bin/uvicorn ac_api:app --port 8811` → `curl -F video=@clip.mp4 .../ac/analyze` → 200 + JSON
(metrics по каналам + state.neiry + state.cards + wellness.narrative). На видео без лица — graceful
(каналы `available:False`, обёртка не падает). Калибровка давления работает сквозь движок.

## Ответ endpoint (структура)
```
{ ok, video, mode,
  metrics: { pulse, bp, spo2, eye, tension, voice, emotions, pose, speech },
  state:   { neiry:{stress,fatigue,engagement,verdict}, cards:[{label,text}] },
  wellness:{ cards, narrative, verdict, spo2 },
  content, report_md }
```

## Деплой на Railway (отдельный сервис, НЕ slim пульсовой)

1. **Отдельный сервис** `focused-clarity` рядом с `HRV_backend` (тот slim, без mediapipe/torch).
   Корень — `NEW VERSION/`. Старт: `uvicorn ac_api:app --host 0.0.0.0 --port $PORT`.
2. **requirements-ac.txt** + системный **ffmpeg** (nixpacks: добавить `ffmpeg` в apt-пакеты).
3. **Модели рядом**: `face_landmarker.task`, `pose_landmarker_lite.task`, ONNX эмоций (hsemotion качает сам).
4. **venv-пути в одном образе**: combo_analyze субпроцессит эмоции/позу/rppg по venv'ам. На сервере
   один общий python со всем стеком → задать env:
   `COMBO_EMO_PYTHON=/usr/local/bin/python`  `COMBO_MAIN_PYTHON=/usr/local/bin/python`
   (combo_config их читает — проверено).
5. **Sleep-режим**: Settings → Serverless / App Sleeping → On. Первый запрос будит (~2–3 сек),
   как у пульс-бота. Клиент шлёт клип → сервис просыпается → считает.
6. **Bp-калибровка**: положить `wellness/bp_fit.json` в образ (иначе давление от дефолта 118/76).

## Осталось после деплоя
- Подключить `@assessment_center_analyzer_bot`: бот принимает видео от пользователя → POST на `/ac/analyze`
  → показывает `wellness.narrative` + карточки + метрики. Тонкий клиент (телефон снимает и шлёт).
- **Полный прогон на реальном клипе с лицом** — проверить пульс/эмоции/wellness на живом видео
  (локально тест-видео с лицом не было, гонял синтетику + юнит-моки).
- Опц.: длинные клипы → фоновая задача + polling (сейчас разбор синхронный, для замера в моменте ок).

## Известные offline-ограничения (честно, в ответе помечено)
- rPPG по клипу даёт **ЧСС**, но НЕ ВСР (rmssd) и НЕ дыхание — давление/стресс по ЧСС+эмоции+голос+поза.
- Голос по offline-speech частичный (нет полного prosody/eGeMAPS как у кружка) → vocal_* грубее.
- `head_tilt` пока не в combo-pose summary → вклад в вовлечённость по неподвижности+лицу.
