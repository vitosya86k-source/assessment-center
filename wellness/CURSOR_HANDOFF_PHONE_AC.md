# Хендофф Курсору — телефонный АЦ (делаем завтра)

Цель: вынести наш анализ (combo + wellness) в телефонный формат АЦ. Архитектура уже
обсуждена и согласована — ниже всё необходимое.

## Архитектура (целевая, не менять)
```
Телефон/браузер (тонкий клиент): снимает видео+звук участника, показывает живой пульс
        ↓ (по запросу будит)
Спящий Railway HRV_backend (polar-hrv, /telegram/webhook — УЖЕ сделано тобой 29.06)
        ↓ на нём полный Python-стек
   combo + wellness считают видео/звук → возвращают метрики + состояние + компетентностный контекст
```
- **Webhook/Railway sleep уже готовы** — НЕ переизучать (см. `RAILWAY_WEBHOOK_HANDOFF_FOR_CLAUDE.md`).
- Тонкий клиент только снимает и шлёт; считает сервер (не телефон).
- Overlay на записанное видео — НЕ делать (отдельная тема, только по команде).

## Что вынести на сервер (весь движок)
Один движок обслуживает И кружок, И телефонный АЦ. Все анализаторы принимают путь к
видео/аудио и возвращают dict.

**Combo (АЦ live, ноут — стабилен):** `HRV_Monitor_bot/NEW VERSION/`
- `analyze_video_rppg.py` / rppg — пульс, дыхание, ВСР
- эмоции (ONNX) + Тодоров (доверие/доминантность)
- поза (MediaPipe Pose, `pose_landmarker_lite.task`)
- речь (prosody/eGeMAPS) + типология (`analysis/technical_analysis.py`: MBTI/радикалы/OCEAN/Павлов/тёмная тетрада)
- `combo_neiry.py` — стресс-индекс / утомление

**Wellness:** `HRV_Monitor_bot/NEW VERSION/wellness/` (8 модулей, см. WELLNESS_README.md)
- eye_markers, bp_estimate, tension_markers, voice_wellness, skin_wellness, spo2_estimate, face_fitness, wellness_summary
- модель `face_landmarker.task` (рядом)

## Зависимости на сервер (Railway образ)
```
pip install mediapipe opencv-python scipy numpy onnxruntime opensmile pymorphy2
```
Модели положить рядом: `face_landmarker.task`, `pose_landmarker_lite.task`, ONNX эмоций.
Опционально (тяжёлое, апгрейд давления): TensorFlow + веса PPG2ABP — НЕ в первую очередь.

## Серверный endpoint (что построить)
`POST /ac/analyze` (или расширить существующий): принимает клип участника →
1. combo: пульс/эмоции/поза/Тодоров/речь/типология/Neiry
2. wellness: eye/bp/tension/voice/skin/spo2 → `wellness_summary.compose(...)`
3. вернуть: метрики + **состояние участника в моменте** (стресс, напряжение/мышечный панцирь, усталость, вокальный стресс) + компетентностный контекст.

## Зачем это в АЦ (ценность)
К оценке компетенций добавляется **как участник держится под нагрузкой**: стресс растёт/нет,
челюсть зажата, голос дрожит, утомление к концу. Это верифицирующий слой к компетенциям
(ровно то, что делает HRV-инструмент ценным для HR).

## Рамки/осторожно
- BP и SpO2 — ОЦЕНКИ, не медизмерение (калибровка манжетой/оксиметром; флаги ENABLED).
- Все wellness — «маркеры/тренды, не диагноз».
- Не путать webhook (Telegram/Railway, готов) с «новым» webhook.

## Где код (персистентно, забирать отсюда)
`HRV_Monitor_bot/NEW VERSION/` + `.../wellness/`. НЕ из `openclaw-vault` (read-only, реверсит).
Подробности подключения — `wellness/WELLNESS_README.md`.
