"""
combo_live_emotion.py — companion ЖИВЫХ эмоций для combo_live_daemon (emo_venv).

Эмоции живут в emo_venv (hsemotion-onnx), а демон — в venv_new. Поэтому эмоции
считает отдельный лёгкий процесс и кладёт текущее состояние в combo/live/emo_state.json,
откуда демон его подхватывает. Это кооперация через файл, без тяжёлого realtime в
одном процессе (см. [[project_combo_bot_architecture]]).

Запуск (emo_venv), параллельно демону:
  emo_venv/bin/python combo_live_emotion.py --source screen --zone 0,0,960,540
  emo_venv/bin/python combo_live_emotion.py --source video.mp4 --duration 20
"""
from __future__ import annotations

import argparse
import json
import time
from collections import deque
from pathlib import Path

import urllib.request  # noqa: F401 — фикс загрузчика hsemotion-onnx
import cv2
import numpy as np

from hsemotion_onnx.facial_emotions import HSEmotionRecognizer

try:
    import face_perception as fp  # Тодоров (доверие/доминантность), лабильность, экспрессивность
except Exception:
    fp = None

import os
HERE = Path(__file__).resolve().parent
# runtime-каталог конфигурируем (Dropbox на сервере read-only)
LIVE_DIR = Path(os.environ.get("COMBO_RUNTIME_DIR", str(HERE))) / "combo" / "live"
try:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
STATE = LIVE_DIR / "emo_state.json"
FACEBOX = LIVE_DIR / "face_box.json"  # демон (MediaPipe) кладёт сюда рамку лица — надёжнее Haar на плитке


def read_face_box(sw, sh):
    """Рамка лица от демона (нормализованная) → пиксели текущего кадра. None если нет/устарела."""
    try:
        d = json.loads(FACEBOX.read_text(encoding="utf-8"))
        if time.time() - float(d.get("t", 0)) > 3.0:
            return None
        x, y = int(d["x"]*sw), int(d["y"]*sh)
        w, h = int(d["w"]*sw), int(d["h"]*sh)
        if w > 8 and h > 8:
            return (max(0, x), max(0, y), w, h)
    except Exception:
        pass
    return None

HSEMO_8 = ["Anger", "Contempt", "Disgust", "Fear", "Happiness", "Neutral", "Sadness", "Surprise"]
NEG = {"Anger", "Contempt", "Disgust", "Fear", "Sadness"}


def cascades():
    c1 = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    c2 = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml")

    def find(gray):
        for c, mn in ((c1, 6), (c2, 4), (c1, 3)):
            fs = c.detectMultiScale(gray, 1.15, mn, minSize=(40, 40))
            if len(fs):
                return tuple(sorted(fs, key=lambda b: b[2]*b[3])[-1])
        return None
    return find


def write_state(d):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="screen")
    ap.add_argument("--zone", default=None)
    ap.add_argument("--duration", type=float, default=0)
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args()
    zone = tuple(int(v) for v in a.zone.split(",")) if a.zone else None

    find = cascades()
    rec = HSEmotionRecognizer(model_name="enet_b0_8_best_afew")
    is_video = a.source != "screen"
    cap = cv2.VideoCapture(a.source) if is_video else None
    sct = None
    if not is_video:
        import mss
        sct = mss.mss()
        mon = sct.monitors[1]
        grab = ({"top": mon["top"]+zone[1], "left": mon["left"]+zone[0], "width": zone[2], "height": zone[3]}
                if zone else mon)

    print(f"эмоции-companion: source={a.source} → {STATE}")
    t0 = time.monotonic()
    last = 0.0
    tl = deque(maxlen=120)   # таймлайн эмоций (~2 мин) для Тодоров/лабильности
    while True:
        now = time.monotonic()
        if now - last < a.interval:
            time.sleep(0.02)   # гейт ДО захвата — иначе busy-loop жрёт CPU как старый emotion_live
            continue
        last = now
        if is_video:
            ok, frame = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0); continue
        else:
            frame = np.ascontiguousarray(np.array(sct.grab(grab))[:, :, :3])
        H, W = frame.shape[:2]
        sc = 640.0 / W if W > 640 else 1.0
        small = cv2.resize(frame, (int(W*sc), int(H*sc))) if sc < 1 else frame
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        # сначала рамка от демона (MediaPipe, ловит на плитке с экрана), иначе свой Haar
        box = read_face_box(small.shape[1], small.shape[0]) or find(gray)
        state = {"face_ok": 0, "dominant": "-", "valence": 0.0, "arousal": 0.0, "t": round(now-t0, 1)}
        if box is not None:
            x, y, w, h = box
            m = int(0.15*w)
            crop = small[max(0, y-m):min(small.shape[0], y+h+m), max(0, x-m):min(small.shape[1], x+w+m)]
            if crop.size:
                try:
                    emo, sv = rec.predict_emotions(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB), logits=False)
                    sv = np.asarray(sv, float); sv = sv/(sv.sum()+1e-9)
                    dist = {HSEMO_8[k]: float(sv[k]) for k in range(8)}
                    dom = emo if isinstance(emo, str) else HSEMO_8[int(np.argmax(sv))]
                    fwhr = round(w/h, 3) if h else None
                    state = {"face_ok": 1, "dominant": dom,
                             "valence": round(dist["Happiness"] - sum(dist[k] for k in NEG), 3),
                             "arousal": round(dist["Anger"]+dist["Fear"]+dist["Surprise"]+dist["Happiness"]-dist["Neutral"], 3),
                             "emotions": {k: round(v, 3) for k, v in dist.items()},
                             "fwhr": fwhr, "t": round(now-t0, 1)}
                    tl.append({"scores": dist, "dominant": dom, "fwhr": fwhr, "t_sec": now-t0})
                    if fp is not None and len(tl) >= 4:
                        try:
                            s = fp.summarize_timeline(list(tl))
                            if s.get("available"):
                                pp, dd = s["perception"], s["dynamics"]
                                state["perceived_trust"] = pp["perceived_trust"]
                                state["perceived_dominance"] = pp["perceived_dominance"]
                                state["switches_per_min"] = dd["emotion_switches_per_min"]
                                state["emotional_stability_pct"] = dd["emotional_stability_pct"]
                                state["expressivity"] = dd["expressivity_index"]
                        except Exception:
                            pass
                except Exception:
                    pass
        write_state(state)
        if a.duration and now - t0 >= a.duration:
            break
    if cap:
        cap.release()
    print("эмоции-companion остановлен")


if __name__ == "__main__":
    main()
