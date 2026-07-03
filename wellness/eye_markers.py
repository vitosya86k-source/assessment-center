#!/usr/bin/env python3
"""Глазные маркеры из видео для кружка (wellness-слой).

MediaPipe FaceLandmarker (Tasks API, iris): частота морганий (EAR), PERCLOS,
частота саккад, стабильность фиксации, прокси нистагма и вестибулярный сигнал.

ВАЖНО: поведенческие маркеры/тренды, НЕ диагноз. Нистагм/вестибулярный сигнал —
повод присмотреться, не неврологическое заключение.

Зависимости для запуска: mediapipe, opencv-python, файл `face_landmarker.task`
рядом с модулем. Проверено на реальном видео-кружке (384×384): лицо детектится,
метрики осмысленны.
"""
from __future__ import annotations

import math
from pathlib import Path

_MODEL = Path(__file__).resolve().parent / "face_landmarker.task"

# индексы FaceLandmarker (478 точек, iris)
_L_IRIS, _R_IRIS = 468, 473
_L_EYE = (33, 160, 158, 133, 153, 144)   # внеш, верх1, верх2, внутр, низ2, низ1
_R_EYE = (362, 385, 387, 263, 373, 380)


def _ear(lm, idx, w, h):
    p = [(lm[i].x * w, lm[i].y * h) for i in idx]
    def d(a, b):
        return math.hypot(p[a][0] - p[b][0], p[a][1] - p[b][1])
    return (d(1, 5) + d(2, 4)) / (2 * (d(0, 3) + 1e-6))


def analyze(video_path: str, max_seconds: float = 90.0, stride: int = 2) -> dict:
    try:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks import python as mpp
        from mediapipe.tasks.python import vision
    except Exception as e:
        return {"available": False, "reason": f"нет cv2/mediapipe: {e}"}
    if not _MODEL.exists():
        return {"available": False, "reason": f"нет модели {_MODEL.name}"}

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {"available": False, "reason": "видео не открылось"}
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    max_frames = int(max_seconds * fps)

    opts = vision.FaceLandmarkerOptions(
        base_options=mpp.BaseOptions(model_asset_path=str(_MODEL)),
        running_mode=vision.RunningMode.VIDEO, num_faces=1)
    landmarker = vision.FaceLandmarker.create_from_options(opts)

    ear_series, ix_series, iy_series, t_series = [], [], [], []
    i = 0
    eff_fps = fps / stride
    while i < max_frames:
        ok, frame = cap.read()
        if not ok:
            break
        if i % stride == 0:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            res = landmarker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), int(i / fps * 1000))
            if res.face_landmarks:
                lm = res.face_landmarks[0]
                h, w = frame.shape[:2]
                ear = (_ear(lm, _L_EYE, w, h) + _ear(lm, _R_EYE, w, h)) / 2
                ear_series.append(ear)
                ix_series.append((lm[_L_IRIS].x + lm[_R_IRIS].x) / 2)
                iy_series.append((lm[_L_IRIS].y + lm[_R_IRIS].y) / 2)
                t_series.append(i / fps)
        i += 1
    cap.release()
    landmarker.close()

    if len(ix_series) < int(eff_fps * 3):
        return {"available": False, "reason": "лицо/глаза почти не видны"}

    import statistics as st
    dur_min = max(1e-6, (t_series[-1] - t_series[0]) / 60.0)

    base = st.median(ear_series)
    thr = base * 0.6
    blinks, closed = 0, False
    for e in ear_series:
        if e < thr and not closed:
            blinks += 1; closed = True
        elif e >= thr:
            closed = False
    blink_rate = round(blinks / dur_min, 1)
    perclos = round(100 * sum(e < thr for e in ear_series) / len(ear_series), 1)

    vel = [abs(ix_series[k] - ix_series[k - 1]) * eff_fps for k in range(1, len(ix_series))]
    saccades = sum(v > 0.015 * eff_fps for v in vel)
    saccade_rate = round(saccades / dur_min, 1)

    fix_stab = round(1.0 / (1.0 + 50 * (st.pstdev(ix_series) + st.pstdev(iy_series))), 2)
    nys = _nystagmus_proxy(ix_series, eff_fps)

    if nys is not None and nys >= 0.30:
        vestibular = "ритмичные колебания взгляда — обратить внимание"
    elif fix_stab < 0.15:
        vestibular = "неустойчивая фиксация взгляда"
    else:
        vestibular = "норма"

    return {
        "available": True, "ok": True,
        "blink_rate_per_min": blink_rate,
        "perclos_pct": perclos,
        "saccade_rate_per_min": saccade_rate,
        "fixation_stability": fix_stab,
        "nystagmus_proxy": nys,
        "vestibular_signal": vestibular,
        "frames_with_face": len(ix_series),
        "note": "Поведенческие маркеры, не диагноз. Нистагм/вестибулярный сигнал — повод присмотреться.",
    }


def _nystagmus_proxy(series, fps):
    n = len(series)
    if n < 16 or fps < 6:
        return None
    m = sum(series) / n
    x = [v - m for v in series]
    best, f = 0.0, 2.0
    while f <= 8.0:
        c = s = 0.0
        for k, v in enumerate(x):
            ang = 2 * math.pi * f * (k / fps)
            c += v * math.cos(ang); s += v * math.sin(ang)
        best = max(best, math.hypot(c, s) / n)
        f += 0.5
    energy = (sum(v * v for v in x) / n) ** 0.5 + 1e-9
    return round(min(1.0, best / energy * 2.0), 2)
