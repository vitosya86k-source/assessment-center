#!/usr/bin/env python3
"""Карта телесного напряжения («мышечный панцирь») из видео лица.

По blendshapes FaceLandmarker считает хронические зоны зажима: челюсть, лоб/межбровье,
глаза, рот — концепция мышечного панциря (Райх): где тело держит напряжение. Плюс
мимическая «замороженность» (флэт-аффект) и асимметрия.

Поведенческие маркеры, НЕ диагноз. Хорошо ложится в wellness-вывод
(«челюсть зажата», «лоб напряжён», «лицо застывшее»).
"""
from __future__ import annotations

from pathlib import Path

_MODEL = Path(__file__).resolve().parent / "face_landmarker.task"


def _g(bs, name):
    """Значение blendshape по имени (0..1) или 0."""
    return bs.get(name, 0.0)


def analyze(video_path: str, max_seconds: float = 90.0, stride: int = 3) -> dict:
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

    lmk = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
        base_options=mpp.BaseOptions(model_asset_path=str(_MODEL)),
        running_mode=vision.RunningMode.VIDEO, num_faces=1,
        output_face_blendshapes=True))

    frames = []   # список dict {name: value}
    i = 0
    while i < int(max_seconds * fps):
        ok, frame = cap.read()
        if not ok:
            break
        if i % stride == 0:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            res = lmk.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), int(i / fps * 1000))
            if res.face_blendshapes:
                frames.append({c.category_name: c.score for c in res.face_blendshapes[0]})
        i += 1
    cap.release()
    lmk.close()

    if len(frames) < 5:
        return {"available": False, "reason": "лицо почти не видно"}

    def mean(name):
        return sum(_g(f, name) for f in frames) / len(frames)

    # --- зоны зажима (0..1) ---
    jaw = min(1.0, (mean("mouthPressLeft") + mean("mouthPressRight")) / 2 * 1.5
              + mean("mouthPucker") * 0.5)                                   # челюсть/рот
    brow = min(1.0, (mean("browDownLeft") + mean("browDownRight")) / 2
               + mean("browInnerUp") * 0.5)                                  # лоб/межбровье
    eyes = min(1.0, (mean("eyeSquintLeft") + mean("eyeSquintRight")) / 2
               + (mean("cheekSquintLeft") + mean("cheekSquintRight")) / 2 * 0.5)  # глаза/прищур
    nose = min(1.0, (mean("noseSneerLeft") + mean("noseSneerRight")) / 2)    # нос (напряж./неприязнь)

    # --- мимическая замороженность: низкая суммарная подвижность blendshapes ---
    import statistics as st
    keys = set().union(*[set(f) for f in frames])
    movement = sum(st.pstdev([f.get(k, 0.0) for f in frames]) for k in keys)
    frozen = round(max(0.0, 1.0 - movement / 1.5), 2)   # 1 = застывшее лицо

    # --- асимметрия напряжения (лево/право) ---
    asym = round(abs(mean("mouthPressLeft") - mean("mouthPressRight"))
                 + abs(mean("browDownLeft") - mean("browDownRight")), 3)

    armor = round((jaw + brow + eyes) / 3, 2)   # «мышечный панцирь» — общий зажим

    # --- человекочитаемые сигналы ---
    zones = []
    if jaw >= 0.25:
        zones.append("челюсть зажата")
    if brow >= 0.25:
        zones.append("лоб/межбровье напряжены")
    if eyes >= 0.25:
        zones.append("глаза прищурены/напряжены")
    if frozen >= 0.7:
        zones.append("лицо малоподвижное, застывшее")

    return {
        "available": True, "ok": True,
        "jaw_clench": round(jaw, 2),
        "brow_tension": round(brow, 2),
        "eye_tension": round(eyes, 2),
        "nose_tension": round(nose, 2),
        "armor_index": armor,            # общий «мышечный панцирь» 0..1
        "facial_frozenness": frozen,     # 0..1, выше = застывшее
        "tension_asymmetry": asym,
        "zones": zones,                  # для wellness-вывода
        "frames": len(frames),
        "note": "Карта напряжения по мимике (поведенческий маркер, не диагноз).",
    }
