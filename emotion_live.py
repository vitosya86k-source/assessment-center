#!/usr/bin/env python3
"""
emotion_live.py — ОТДЕЛЬНЫЙ live-анализ ЭМОЦИЙ участника для АЦ (HSEmotion ONNX).
Работает ПАРАЛЛЕЛЬНО с rppg_screen.py / pose_live.py и НЕ трогает их.

Захватывает плитку участника, ловит лицо (Haar), гонит HSEmotion enet_b0_8 (8
классов AffectNet), считает валентность/возбуждение и — через face_perception —
восприятие по Тодорову + динамику мимики. Пишет CSV с таймштампами в data/.

Запуск (emo_venv):  ./start_emotion.sh чай
Клавиши в окне EMO: r — переобвести плитку, q — выход.

Рамка: это PERCEPTION + видимое выражение, НЕ «эмоция/характер» (Барретт/Тодоров).
"""
from __future__ import annotations

import urllib.request  # фикс бага загрузчика hsemotion-onnx (он не импортит request)
import argparse
import csv
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import mss
import numpy as np

try:
    from hsemotion_onnx.facial_emotions import HSEmotionRecognizer
except Exception as e:  # pragma: no cover
    print("hsemotion-onnx не найден:", e)
    sys.exit(1)

import face_perception as fp

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
HSEMO_8 = ["Anger", "Contempt", "Disgust", "Fear", "Happiness", "Neutral", "Sadness", "Surprise"]
NEG = {"Anger", "Contempt", "Disgust", "Fear", "Sadness"}
PROC_W = 480
EMO_EVERY = 0.4          # как часто гонять эмоцию (сек)
SUMM_EVERY = 6.0        # как часто пересчитывать восприятие/динамику


def select_zone(sct, monitor):
    """Полный экран → ручной выбор плитки мышью (namedWindow + callback)."""
    full = np.ascontiguousarray(np.array(sct.grab(monitor))[:, :, :3])
    H, W = full.shape[:2]
    scale = min(1.0, 1280.0 / W)
    disp = cv2.resize(full, (int(W * scale), int(H * scale))) if scale < 1 else full
    win = "Obvedi uchastnika (EMO)"
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
        cv2.putText(vis, "drag mouse | ENTER - ok | c - cancel", (10, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(vis, "drag mouse | ENTER - ok | c - cancel", (10, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 1, cv2.LINE_AA)
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

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{args.exercise}" if args.exercise else ""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / f"emo_{stamp}{suffix}.csv"
    log_f = open(log_path, "a", newline="")
    logger = csv.writer(log_f)
    logger.writerow(["timestamp", "face_ok", "src", "dominant", "valence", "arousal",
                     *[f"e_{k}" for k in HSEMO_8], "fwhr",
                     "perceived_trust", "perceived_dominance", "switches_per_min"])
    log_f.flush()

    def _stop(*_):
        raise KeyboardInterrupt
    for s in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(s, _stop)
        except Exception:
            pass

    cascade = cv2.CascadeClassifier(
        os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
    cascade2 = cv2.CascadeClassifier(
        os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_alt2.xml"))

    def find_face(gray):
        """Два каскада с разными порогами; None если лицо не найдено."""
        for casc, mn in ((cascade, 6), (cascade2, 4), (cascade, 3)):
            fs = casc.detectMultiScale(gray, 1.15, mn, minSize=(40, 40))
            if len(fs):
                return tuple(sorted(fs, key=lambda b: b[2] * b[3])[-1])
        return None

    rec = HSEmotionRecognizer(model_name="enet_b0_8_best_afew")

    sct = mss.mss()
    monitor = sct.monitors[1]
    zone = select_zone(sct, monitor)
    if zone is None:
        print("Зона не выбрана — выход.")
        return
    print(f"зона участника: {zone}\nлог эмоций: {log_path}\nокно EMO: r — переобвести, q — выход")

    cv2.namedWindow("EMO", cv2.WINDOW_AUTOSIZE)
    try:  # парковка справа-внизу (поза слева-внизу, плитка вверху — не перекрываем)
        cv2.moveWindow("EMO", max(0, monitor["width"] - zone[2] - 40),
                       max(0, monitor["height"] - zone[3] - 80))
    except Exception:
        pass

    timeline = deque(maxlen=600)
    last_emo = 0.0
    last_summ = 0.0
    perc = {"available": False}
    dominant, valence, arousal = "-", 0.0, 0.0
    scores = [0.0] * 8
    src = "-"
    t_start = time.time()

    try:
        while True:
            zx, zy, zw, zh = zone
            grab = {"top": monitor["top"] + zy, "left": monitor["left"] + zx,
                    "width": zw, "height": zh}
            frame = np.ascontiguousarray(np.array(sct.grab(grab))[:, :, :3])
            H, W = frame.shape[:2]
            sc = PROC_W / float(W) if W > PROC_W else 1.0
            small = cv2.resize(frame, (int(W * sc), int(H * sc))) if sc < 1 else frame.copy()

            face_ok = 0
            now = time.time()
            if now - last_emo >= EMO_EVERY:
                last_emo = now
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                box = find_face(gray)
                sh, sw = small.shape[:2]
                if box is not None:
                    fx, fy, fw, fh = box
                    src = "face"
                    col = (0, 230, 0)
                else:
                    # запасной режим: центр плитки (там его голова) — Haar не нашёл лицо
                    fw = fh = int(min(sw, sh) * 0.6)
                    fx = (sw - fw) // 2
                    fy = max(0, int(sh * 0.30) - fh // 2) if sh > fh else 0
                    src = "zone"
                    col = (0, 170, 255)
                if True:
                    cv2.rectangle(small, (fx, fy), (fx + fw, fy + fh), col, 2)
                    m = int(0.15 * fw)
                    x0, y0 = max(fx - m, 0), max(fy - m, 0)
                    x1, y1 = min(fx + fw + m, small.shape[1]), min(fy + fh + m, small.shape[0])
                    crop = small[y0:y1, x0:x1]
                    if crop.size:
                        try:
                            rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
                            emo, sv = rec.predict_emotions(rgb, logits=False)
                            sv = np.asarray(sv, dtype=float)
                            sv = sv / (sv.sum() + 1e-9)
                            scores = [float(x) for x in sv]
                            dist = {HSEMO_8[k]: scores[k] for k in range(8)}
                            dominant = emo if isinstance(emo, str) else HSEMO_8[int(np.argmax(sv))]
                            valence = round(dist["Happiness"] - sum(dist[k] for k in NEG), 3)
                            arousal = round(dist["Anger"] + dist["Fear"] + dist["Surprise"]
                                            + dist["Happiness"] - dist["Neutral"], 3)
                            fwhr = round(float(fw) / max(float(fh), 1.0), 3)
                            face_ok = 1
                            timeline.append({"dominant": dominant, "scores": dist,
                                             "fwhr": fwhr, "t_sec": round(now - t_start, 2)})
                        except Exception:
                            pass

            if now - last_summ >= SUMM_EVERY and len(timeline) >= 4:
                last_summ = now
                try:
                    perc = fp.summarize_timeline(list(timeline))
                except Exception:
                    perc = {"available": False}

            # панель
            y0 = 22

            def line(txt, col, dy=24):
                nonlocal y0
                cv2.putText(small, txt, (8, y0), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
                cv2.putText(small, txt, (8, y0), cv2.FONT_HERSHEY_SIMPLEX, 0.6, col, 1, cv2.LINE_AA)
                y0 += dy
            if face_ok or timeline:
                vcol = (0, 220, 0) if valence >= 0 else (0, 140, 255)
                line(f"EMO: {dominant} [{src}]", (255, 255, 255) if src == "face" else (0, 200, 255))
                line(f"valence: {valence:+.2f}", vcol)
                line(f"arousal: {arousal:+.2f}", (0, 180, 255) if arousal > 0.2 else (200, 200, 200))
                if perc.get("available"):
                    p = perc["perception"]
                    dyn = perc["dynamics"]
                    line(f"vospr: trust {p['perceived_trust']:.2f} dom {p['perceived_dominance']:.2f}",
                         (220, 220, 0))
                    line(f"dinamika: perekl {dyn['emotion_switches_per_min']}/min  expr {dyn['expressivity_index']}",
                         (200, 200, 200))
                else:
                    line("vospr: kopim...", (160, 160, 160))
            else:
                line("litso ne naydeno", (0, 140, 255))
            cv2.imshow("EMO", small)

            # лог раз в EMO_EVERY (по факту face-апдейта)
            if face_ok:
                logger.writerow([
                    datetime.now().isoformat(timespec="seconds"), 1, src, dominant,
                    f"{valence:.3f}", f"{arousal:.3f}",
                    *[f"{s:.3f}" for s in scores],
                    f"{timeline[-1]['fwhr']:.3f}" if timeline else "",
                    f"{perc['perception']['perceived_trust']:.3f}" if perc.get("available") else "",
                    f"{perc['perception']['perceived_dominance']:.3f}" if perc.get("available") else "",
                    f"{perc['dynamics']['emotion_switches_per_min']:.1f}" if perc.get("available") else "",
                ])
                log_f.flush()

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("r"):
                nz = select_zone(sct, monitor)
                if nz:
                    zone = nz
                    timeline.clear()
                    try:
                        cv2.moveWindow("EMO", max(0, monitor["width"] - zone[2] - 40),
                                       max(0, monitor["height"] - zone[3] - 80))
                    except Exception:
                        pass
                    print(f"новая зона: {zone}")
    except KeyboardInterrupt:
        pass
    finally:
        log_f.close()
        cv2.destroyAllWindows()
        print(f"\nготово. лог эмоций: {log_path}")


if __name__ == "__main__":
    main()
