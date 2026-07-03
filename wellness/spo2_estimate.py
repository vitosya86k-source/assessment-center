#!/usr/bin/env python3
"""SpO2-прокси по видео (rPPG) — оценка, НЕ медизмерение.

Реальный принцип пульсоксиметрии — ratio-of-ratios: R = (AC/DC)_red / (AC/DC)_blue,
SpO2 ≈ A − B·R. Камера не равна клиническому оксиметру (нет калиброванных длин волн),
поэтому это ОЦЕНКА/тренд, требует персональной калибровки настоящим пульсоксиметром.

Сигнал берём со лба (forehead ROI через FaceLandmarker) — красный и синий каналы.
Калибровка: spo2_fit.json — {A, B}. По умолчанию — эмпирические A=110, B=25.
"""
from __future__ import annotations

import json
from pathlib import Path

ENABLED = True
_MODEL = Path(__file__).resolve().parent / "face_landmarker.task"
_FIT = Path(__file__).resolve().parent / "spo2_fit.json"
_DEFAULT = {"A": 110.0, "B": 25.0, "calibrated": False}

# точки лба (FaceLandmarker): центр лба и межбровье-вверх
_FOREHEAD = (10, 151, 9, 8, 107, 336)


def _load():
    if _FIT.exists():
        try:
            return {**_DEFAULT, **json.loads(_FIT.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return dict(_DEFAULT)


def analyze(video_path: str, max_seconds: float = 60.0, stride: int = 1) -> dict:
    if not ENABLED:
        return {"available": False, "reason": "SpO2 выключен"}
    try:
        import cv2
        import numpy as np
        import mediapipe as mp
        from mediapipe.tasks import python as mpp
        from mediapipe.tasks.python import vision
        from scipy.signal import butter, filtfilt
    except Exception as e:
        return {"available": False, "reason": f"deps: {e}"}
    if not _MODEL.exists():
        return {"available": False, "reason": "нет модели"}

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {"available": False, "reason": "видео не открылось"}
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0

    lmk = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
        base_options=mpp.BaseOptions(model_asset_path=str(_MODEL)),
        running_mode=vision.RunningMode.VIDEO, num_faces=1))

    R, B, i = [], [], 0
    while i < int(max_seconds * fps):
        ok, frame = cap.read()
        if not ok:
            break
        if i % stride == 0:
            h, w = frame.shape[:2]
            res = lmk.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)),
                int(i / fps * 1000))
            if res.face_landmarks:
                lm = res.face_landmarks[0]
                xs = [lm[k].x * w for k in _FOREHEAD]
                ys = [lm[k].y * h for k in _FOREHEAD]
                cx, cy = int(sum(xs) / len(xs)), int(sum(ys) / len(ys))
                s = max(6, int(0.04 * w))
                patch = frame[max(0, cy - s):cy + s, max(0, cx - s):cx + s]
                if patch.size:
                    mean = patch.reshape(-1, 3).mean(axis=0)   # BGR
                    B.append(mean[0]); R.append(mean[2])
        i += 1
    cap.release()
    lmk.close()

    if len(R) < int(fps * 5):
        return {"available": False, "reason": "лоб/лицо мало видно для SpO2"}

    R = np.array(R); B = np.array(B)
    eff = fps / stride

    def acdc(x):
        dc = x.mean()
        lo, hi = 0.7 / (eff / 2), 4.0 / (eff / 2)
        if hi >= 1:
            hi = 0.99
        bb, aa = butter(2, [lo, hi], btype="band")
        ac = filtfilt(bb, aa, x - dc)
        return float(np.sqrt((ac ** 2).mean())), float(dc)

    try:
        ac_r, dc_r = acdc(R); ac_b, dc_b = acdc(B)
    except Exception as e:
        return {"available": False, "reason": f"фильтр: {e}"}
    if dc_r <= 0 or dc_b <= 0 or ac_b <= 0:
        return {"available": False, "reason": "слабый сигнал"}

    ratio = (ac_r / dc_r) / (ac_b / dc_b)
    f = _load()
    spo2 = f["A"] - f["B"] * ratio
    spo2 = max(85.0, min(100.0, spo2))

    conf = "indicative" if not f.get("calibrated") else "ok"
    return {
        "available": True, "ok": True,
        "spo2": round(spo2),
        "ratio": round(ratio, 3),
        "confidence": conf,
        "calibrated": bool(f.get("calibrated")),
        "note": "SpO2-оценка по видео (ratio-of-ratios), НЕ медизмерение. "
                "Низкое значение по видео НЕ диагноз — мерить настоящим пульсоксиметром.",
    }


def calibrate(true_spo2: float, ratio: float, second: tuple[float, float] | None = None) -> dict:
    """Калибровка по настоящему пульсоксиметру. Один замер двигает A (при B по умолч.);
    два замера (true, ratio) задают линию A,B точно. second=(true2, ratio2)."""
    f = _load()
    if second:
        t2, r2 = second
        f["B"] = (true_spo2 - t2) / (r2 - ratio) if r2 != ratio else f["B"]
        f["A"] = true_spo2 + f["B"] * ratio
    else:
        f["A"] = true_spo2 + f["B"] * ratio
    f["calibrated"] = True
    _FIT.write_text(json.dumps(f, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"calibrated": True, "fit": f}
