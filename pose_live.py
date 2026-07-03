#!/usr/bin/env python3
"""
pose_live.py — ОТДЕЛЬНЫЙ live-анализ позы участника для АЦ (MediaPipe Tasks
PoseLandmarker). Работает ПАРАЛЛЕЛЬНО с rppg_screen.py и НЕ трогает его.

Захватывает плитку участника с экрана, рисует скелет и считает state-маркеры:
  - рука у лица (self-adaptor) — валидный невербальный маркер нагрузки/стресса
  - ёрзанье (движение плеч/головы)
  - наклон головы
  - вовлечённость (ширина плеч ~ "ближе к камере / подался вперёд")
Пишет CSV с таймштампами в data/ — потом сводится с пульсом/голосом по времени.

Запуск (venv_new, где рабочий GUI):
  ./start_pose.sh чай
Сначала обведёшь мышью плитку участника (ENTER), потом идёт анализ.
Клавиши в окне POSE: r — переобвести, q — выход.
"""
from __future__ import annotations

import argparse
import csv
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import mss
import numpy as np

try:
    import mediapipe as mp
    from mediapipe.tasks import python as mpp
    from mediapipe.tasks.python import vision
except Exception as e:  # pragma: no cover
    print("MediaPipe Tasks не найден:", e)
    sys.exit(1)

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
MODEL_PATH = HERE / "pose_landmarker_lite.task"
PROC_W = 480              # ширина для инференса (downscale для скорости)
FACE_K = 0.55            # порог "рука у лица" в долях ширины плеч

# связи скелета (индексы BlazePose) для ручной отрисовки
CONN = [(11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
        (11, 23), (12, 24), (23, 24), (0, 11), (0, 12)]
PTS = [0, 7, 8, 11, 12, 13, 14, 15, 16, 23, 24]


def dist(a, b):
    return float(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5)


def select_zone(sct, monitor):
    """Полный экран → ручной выбор плитки мышью (namedWindow + callback)."""
    full = np.ascontiguousarray(np.array(sct.grab(monitor))[:, :, :3])
    H, W = full.shape[:2]
    scale = min(1.0, 1280.0 / W)
    disp = cv2.resize(full, (int(W * scale), int(H * scale))) if scale < 1 else full
    win = "Obvedi uchastnika"
    cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
    cv2.imshow(win, disp)
    cv2.waitKey(1)
    st = {"p0": None, "p1": None}

    def cb(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            st["p0"], st["p1"] = (x, y), (x, y)
        elif event == cv2.EVENT_MOUSEMOVE and st["p0"] is not None:
            st["p1"] = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and st["p0"] is not None:
            st["p1"] = (x, y)

    cv2.setMouseCallback(win, cb)
    zone = None
    while True:
        vis = disp.copy()
        if st["p0"] and st["p1"]:
            cv2.rectangle(vis, st["p0"], st["p1"], (0, 255, 0), 2)
        cv2.putText(vis, "drag mouse | ENTER - ok | c - cancel",
                    (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(vis, "drag mouse | ENTER - ok | c - cancel",
                    (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 1, cv2.LINE_AA)
        cv2.imshow(win, vis)
        k = cv2.waitKey(20) & 0xFF
        if k in (13, 10, 32):
            if st["p0"] and st["p1"]:
                (x0, y0), (x1, y1) = st["p0"], st["p1"]
                x, y = min(x0, x1), min(y0, y1)
                w, h = abs(x1 - x0), abs(y1 - y0)
                if w >= 10 and h >= 10:
                    inv = 1.0 / scale
                    zone = (int(x * inv), int(y * inv), int(w * inv), int(h * inv))
            break
        if k in (ord("c"), 27):
            break
    cv2.destroyWindow(win)
    cv2.waitKey(1)
    return zone


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exercise", "-e", default=None)
    args = ap.parse_args()

    if not MODEL_PATH.exists():
        print(f"Нет модели {MODEL_PATH.name} — скачай pose_landmarker_lite.task")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{args.exercise}" if args.exercise else ""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / f"pose_{stamp}{suffix}.csv"
    log_f = open(log_path, "a", newline="")
    logger = csv.writer(log_f)
    logger.writerow(["timestamp", "pose_ok", "hand_to_face", "fidget_idx",
                     "head_tilt_deg", "shoulder_w_norm", "lean_proxy"])
    log_f.flush()

    def _stop(*_):
        raise KeyboardInterrupt
    for s in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(s, _stop)
        except Exception:
            pass

    sct = mss.mss()
    monitor = sct.monitors[1]
    zone = select_zone(sct, monitor)
    if zone is None:
        print("Зона не выбрана — выход.")
        return
    print(f"зона участника: {zone}")
    print(f"лог позы: {log_path}")
    print("окно POSE: r — переобвести, q — выход")

    opts = vision.PoseLandmarkerOptions(
        base_options=mpp.BaseOptions(model_asset_path=str(MODEL_PATH)),
        running_mode=vision.RunningMode.VIDEO, num_poses=1,
        min_pose_detection_confidence=0.5, min_tracking_confidence=0.5)
    landmarker = vision.PoseLandmarker.create_from_options(opts)

    cv2.namedWindow("POSE", cv2.WINDOW_AUTOSIZE)
    try:
        cv2.moveWindow("POSE", 20, max(0, monitor["height"] - zone[3] - 80))
    except Exception:
        pass

    prev_nose = prev_sh = None
    fidget_ema = 0.0
    hand_events = 0
    hand_active = False
    last_log = 0.0
    t_start = time.time()
    ts_prev = -1

    try:
        while True:
            zx, zy, zw, zh = zone
            grab = {"top": monitor["top"] + zy, "left": monitor["left"] + zx,
                    "width": zw, "height": zh}
            frame = np.ascontiguousarray(np.array(sct.grab(grab))[:, :, :3])
            H, W = frame.shape[:2]
            sc = PROC_W / float(W) if W > PROC_W else 1.0
            small = cv2.resize(frame, (int(W * sc), int(H * sc))) if sc < 1 else frame.copy()
            sh_h, sh_w_px = small.shape[:2]
            rgb = np.ascontiguousarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))

            ts = int((time.time() - t_start) * 1000)
            if ts <= ts_prev:
                ts = ts_prev + 1
            ts_prev = ts

            pose_ok = 0
            hand_to_face = 0
            head_tilt = 0.0
            sh_w_norm = 0.0
            lean = 0.0
            try:
                mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                res = landmarker.detect_for_video(mp_img, ts)
            except Exception:
                res = None

            if res and res.pose_landmarks:
                pose_ok = 1
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
                dx, dy = (rear[0] - lear[0]), (rear[1] - lear[1])
                head_tilt = round(float(np.degrees(np.arctan2(dy, dx))), 1)
                sh_mid = ((lsh[0] + rsh[0]) / 2, (lsh[1] + rsh[1]) / 2)
                if prev_nose is not None:
                    mv = dist(nose, prev_nose) + dist(sh_mid, prev_sh)
                    fidget_ema = 0.8 * fidget_ema + 0.2 * mv
                prev_nose, prev_sh = nose, sh_mid
                if hand_to_face and not hand_active:
                    hand_events += 1
                hand_active = bool(hand_to_face)

                # ручная отрисовка скелета (нормир. -> пиксели small)
                def XY(i):
                    return (int(lm[i].x * sh_w_px), int(lm[i].y * sh_h))
                for a, b in CONN:
                    if lm[a].visibility > 0.4 and lm[b].visibility > 0.4:
                        cv2.line(small, XY(a), XY(b), (0, 230, 0), 2, cv2.LINE_AA)
                for i in PTS:
                    if lm[i].visibility > 0.4:
                        cv2.circle(small, XY(i), 3, (0, 200, 255), -1, cv2.LINE_AA)
            else:
                prev_nose = prev_sh = None
                hand_active = False

            panel = small
            y0 = 22

            def line(txt, col, dy=24):
                nonlocal y0
                cv2.putText(panel, txt, (8, y0), cv2.FONT_HERSHEY_SIMPLEX,
                            0.6, (0, 0, 0), 3, cv2.LINE_AA)
                cv2.putText(panel, txt, (8, y0), cv2.FONT_HERSHEY_SIMPLEX,
                            0.6, col, 1, cv2.LINE_AA)
                y0 += dy
            if pose_ok:
                line("figura: est", (0, 220, 0))
                line(f"RUKA U LICA: {'DA' if hand_to_face else 'net'} (vsego {hand_events})",
                     (0, 0, 255) if hand_to_face else (180, 180, 180))
                fl = "nizk" if fidget_ema < 0.01 else ("sredn" if fidget_ema < 0.03 else "VYSOK")
                line(f"erzanie: {fl} ({fidget_ema:.3f})",
                     (0, 180, 255) if fidget_ema >= 0.03 else (200, 200, 200))
                line(f"naklon: {head_tilt:+.0f}", (220, 220, 0))
                line(f"plechi(blizhe): {sh_w_norm:.2f}", (200, 200, 200))
            else:
                line("figura ne naydena", (0, 140, 255))
                line("nuzhen korpus/plechi v kadre", (180, 180, 180))
            cv2.imshow("POSE", panel)

            now = time.time()
            if now - last_log > 0.5:
                last_log = now
                logger.writerow([
                    datetime.now().isoformat(timespec="seconds"),
                    pose_ok, hand_to_face, f"{fidget_ema:.4f}",
                    f"{head_tilt:.1f}" if pose_ok else "",
                    f"{sh_w_norm:.3f}" if pose_ok else "",
                    f"{lean:.3f}" if pose_ok else "",
                ])
                log_f.flush()

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("r"):
                nz = select_zone(sct, monitor)
                if nz:
                    zone = nz
                    prev_nose = prev_sh = None
                    try:
                        cv2.moveWindow("POSE", 20, max(0, monitor["height"] - zone[3] - 80))
                    except Exception:
                        pass
                    print(f"новая зона: {zone}")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            landmarker.close()
        except Exception:
            pass
        log_f.close()
        cv2.destroyAllWindows()
        print(f"\nготово. лог позы: {log_path}")


if __name__ == "__main__":
    main()
