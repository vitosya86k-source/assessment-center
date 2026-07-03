"""
analyze_video_pose.py — оффлайн-разбор ПОЗЫ по видео-ФАЙЛУ (для комбо-бота).

Запускать в venv_new:
    venv_new/bin/python analyze_video_pose.py --video in.mp4 \
        --out-csv pose.csv --out-json pose.json [--sample-fps 5]

Логика 1:1 с live pose_live.py (MediaPipe Tasks PoseLandmarker, рука-у-лица,
ёрзанье, наклон головы, разворот плеч/lean), но кадры берутся из файла.
Универсально: любое видео. Нет фигуры → pose_ok=0, не падаем.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import cv2
import numpy as np

try:
    import mediapipe as mp
    from mediapipe.tasks import python as mpp
    from mediapipe.tasks.python import vision
except Exception as e:  # pragma: no cover
    print(json.dumps({"ok": False, "error": f"MediaPipe Tasks не найден: {e}"}))
    sys.exit(2)

HERE = Path(__file__).resolve().parent
MODEL_PATH = HERE / "pose_landmarker_lite.task"
PROC_W = 480
FACE_K = 0.55
CONN_PTS = [0, 7, 8, 11, 12, 13, 14, 15, 16, 23, 24]  # для справки


def dist(a, b):
    return float(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5)


def analyze(video_path: str, out_csv: str, out_json: str, sample_fps: float = 5.0) -> dict:
    if not MODEL_PATH.exists():
        return {"ok": False, "error": f"нет модели {MODEL_PATH.name}"}
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {"ok": False, "error": f"не открыть видео: {video_path}"}

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, int(round(src_fps / max(sample_fps, 0.1))))

    opts = vision.PoseLandmarkerOptions(
        base_options=mpp.BaseOptions(model_asset_path=str(MODEL_PATH)),
        running_mode=vision.RunningMode.VIDEO, num_poses=1,
        min_pose_detection_confidence=0.5, min_tracking_confidence=0.5)
    landmarker = vision.PoseLandmarker.create_from_options(opts)

    out_f = open(out_csv, "w", newline="", encoding="utf-8")
    w = csv.writer(out_f)
    w.writerow(["t_sec", "pose_ok", "hand_to_face", "fidget_idx",
                "head_tilt_deg", "shoulder_w_norm", "lean_proxy"])

    prev_nose = prev_sh = None
    fidget_ema = 0.0
    hand_events = 0
    hand_active = False
    n_pose = 0
    n_total = 0
    hand_frames = 0
    fidget_vals = []
    head_tilt_vals = []
    ts_prev = -1
    idx = -1

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        idx += 1
        if idx % step != 0:
            continue
        n_total += 1
        t_sec = round(idx / src_fps, 2)
        H, W = frame.shape[:2]
        sc = PROC_W / float(W) if W > PROC_W else 1.0
        small = cv2.resize(frame, (int(W * sc), int(H * sc))) if sc < 1 else frame
        rgb = np.ascontiguousarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))

        ts = int(t_sec * 1000)
        if ts <= ts_prev:
            ts = ts_prev + 1
        ts_prev = ts

        pose_ok = hand_to_face = 0
        head_tilt = sh_w_norm = lean = 0.0
        try:
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            res = landmarker.detect_for_video(mp_img, ts)
        except Exception:
            res = None

        if res and res.pose_landmarks:
            pose_ok = 1
            n_pose += 1
            lm = res.pose_landmarks[0]

            def P(i):
                return (lm[i].x, lm[i].y)
            nose = P(0)
            lsh, rsh = P(11), P(12)
            lwr, rwr = P(15), P(16)
            lear, rear = P(7), P(8)
            shoulder_w = dist(lsh, rsh) + 1e-6
            sh_w_norm = round(shoulder_w, 3)
            lean = sh_w_norm
            d = min(dist(lwr, nose), dist(rwr, nose))
            if d < FACE_K * shoulder_w:
                hand_to_face = 1
                hand_frames += 1
            dx, dy = (rear[0] - lear[0]), (rear[1] - lear[1])
            head_tilt = round(float(np.degrees(np.arctan2(dy, dx))), 1)
            head_tilt_vals.append(head_tilt)
            sh_mid = ((lsh[0] + rsh[0]) / 2, (lsh[1] + rsh[1]) / 2)
            if prev_nose is not None:
                mv = dist(nose, prev_nose) + dist(sh_mid, prev_sh)
                fidget_ema = 0.8 * fidget_ema + 0.2 * mv
                fidget_vals.append(fidget_ema)
            prev_nose, prev_sh = nose, sh_mid
            if hand_to_face and not hand_active:
                hand_events += 1
            hand_active = bool(hand_to_face)
        else:
            prev_nose = prev_sh = None
            hand_active = False

        w.writerow([t_sec, pose_ok, hand_to_face, f"{fidget_ema:.4f}",
                    f"{head_tilt:.1f}" if pose_ok else "",
                    f"{sh_w_norm:.3f}" if pose_ok else "",
                    f"{lean:.3f}" if pose_ok else ""])

    cap.release()
    out_f.close()
    try:
        landmarker.close()
    except Exception:
        pass

    summary = {
        "ok": True, "module": "pose", "video": video_path, "csv": out_csv,
        "frames_sampled": n_total, "frames_with_pose": n_pose,
        "pose_coverage": round(n_pose / n_total, 3) if n_total else 0.0,
        "available": n_pose > 0,
    }
    if n_pose > 0:
        summary["hand_to_face_events"] = hand_events
        summary["hand_to_face_rate"] = round(hand_frames / n_pose, 3)
        summary["fidget_mean"] = round(float(np.mean(fidget_vals)), 4) if fidget_vals else 0.0
        summary["fidget_level"] = ("низкое" if summary["fidget_mean"] < 0.01
                                   else "среднее" if summary["fidget_mean"] < 0.03 else "высокое")
        # средний наклон головы (для вовлечённости/фокуса в offline-движке)
        summary["head_tilt_mean"] = round(float(np.mean(head_tilt_vals)), 1) if head_tilt_vals else 0.0
    else:
        summary["note"] = "фигура/плечи не найдены ни в одном кадре"

    Path(out_json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--sample-fps", type=float, default=5.0)
    a = ap.parse_args()
    res = analyze(a.video, a.out_csv, a.out_json, a.sample_fps)
    print(json.dumps({k: v for k, v in res.items()
                      if k in ("ok", "available", "pose_coverage", "fidget_level", "error", "note")},
                     ensure_ascii=False))
    sys.exit(0 if res.get("ok") else 1)


if __name__ == "__main__":
    main()
