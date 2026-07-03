#!/usr/bin/env python3
"""Фейс-фитнес — чекер мимических упражнений по blendshapes FaceLandmarker.

Анализирует видео и по каждому движению (брови, улыбка, прищур, щёки, губы, челюсть)
даёт пиковую амплитуду и лево/право симметрию + общий «лицевой тонус». Гайдед-подсказки
(что выполнить и в каком порядке) — на стороне бота; здесь — измерение/оценка.

Для трекинга прогресса по дням: сохранять peak-амплитуды в историю.
"""
from __future__ import annotations

from pathlib import Path

_MODEL = Path(__file__).resolve().parent / "face_landmarker.task"

# движение → (левый blendshape, правый blendshape) или (одиночный, None)
_MOVES = {
    "брови_вверх": ("browOuterUpLeft", "browOuterUpRight"),
    "улыбка": ("mouthSmileLeft", "mouthSmileRight"),
    "прищур": ("eyeSquintLeft", "eyeSquintRight"),
    "щёки": ("cheekPuff", None),
    "губы_трубочкой": ("mouthPucker", None),
    "открыть_рот": ("jawOpen", None),
    "поднять_межбровье": ("browInnerUp", None),
}


def analyze(video_path: str, max_seconds: float = 60.0, stride: int = 2) -> dict:
    try:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks import python as mpp
        from mediapipe.tasks.python import vision
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
        running_mode=vision.RunningMode.VIDEO, num_faces=1,
        output_face_blendshapes=True))

    frames, i = [], 0
    while i < int(max_seconds * fps):
        ok, frame = cap.read()
        if not ok:
            break
        if i % stride == 0:
            res = lmk.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)),
                int(i / fps * 1000))
            if res.face_blendshapes:
                frames.append({c.category_name: c.score for c in res.face_blendshapes[0]})
        i += 1
    cap.release()
    lmk.close()

    if len(frames) < 5:
        return {"available": False, "reason": "лицо почти не видно"}

    def peak(name):
        return max((f.get(name, 0.0) for f in frames), default=0.0)

    results = {}
    for move, (lkey, rkey) in _MOVES.items():
        if rkey:
            pl, pr = peak(lkey), peak(rkey)
            amp = round((pl + pr) / 2, 2)
            sym = round(1 - abs(pl - pr) / (max(pl, pr) + 1e-6), 2)   # 1 = симметрично
        else:
            amp = round(peak(lkey), 2)
            sym = None
        results[move] = {"amplitude": amp, "symmetry": sym,
                         "done": amp >= 0.4}

    done = [m for m, r in results.items() if r["done"]]
    tone = round(sum(r["amplitude"] for r in results.values()) / len(results), 2)

    return {
        "available": True, "ok": True,
        "moves": results,
        "done": done,                 # какие движения зафиксированы
        "facial_tone": tone,          # общий «лицевой тонус» 0..1
        "frames": len(frames),
        "note": "Чекер мимических упражнений (амплитуда + симметрия). Прогресс — по дням.",
    }
