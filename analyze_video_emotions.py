"""
analyze_video_emotions.py — оффлайн-разбор ЭМОЦИЙ по видео-ФАЙЛУ (для комбо-бота).

Запускать в emo_venv:
    emo_venv/bin/python analyze_video_emotions.py --video in.mp4 \
        --out-csv emo.csv --out-json emo.json [--sample-fps 5]

Логика 1:1 с live-версией emotion_live.py (HSEmotion enet_b0_8_best_afew, 8 классов
AffectNet, валентность/возбуждение, FWHR, восприятие через face_perception), но
источник кадров — файл, а не захват экрана. Универсально: любое видео.

Кейс «нет лица» (наушники/очки/только аудио) обрабатывается штатно: пишем face_ok=0,
не падаем. Если лиц нет вообще — сводка помечается available=false.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import urllib.request  # noqa: F401 — фикс бага загрузчика hsemotion-onnx
import cv2
import numpy as np

try:
    from hsemotion_onnx.facial_emotions import HSEmotionRecognizer
except Exception as e:  # pragma: no cover
    print(json.dumps({"ok": False, "error": f"hsemotion-onnx не найден: {e}"}))
    sys.exit(2)

try:
    import face_perception as fp
    _FP_OK = True
except Exception:
    _FP_OK = False

HSEMO_8 = ["Anger", "Contempt", "Disgust", "Fear", "Happiness", "Neutral", "Sadness", "Surprise"]
NEG = {"Anger", "Contempt", "Disgust", "Fear", "Sadness"}
PROC_W = 640


def build_cascades():
    c1 = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    c2 = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml")

    def find_face(gray):
        for casc, mn in ((c1, 6), (c2, 4), (c1, 3)):
            fs = casc.detectMultiScale(gray, 1.15, mn, minSize=(40, 40))
            if len(fs):
                return tuple(sorted(fs, key=lambda b: b[2] * b[3])[-1])
        return None

    return find_face


def analyze(video_path: str, out_csv: str, out_json: str, sample_fps: float = 5.0) -> dict:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {"ok": False, "error": f"не открыть видео: {video_path}"}

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, int(round(src_fps / max(sample_fps, 0.1))))
    find_face = build_cascades()
    rec = HSEmotionRecognizer(model_name="enet_b0_8_best_afew")

    out_csv_f = open(out_csv, "w", newline="", encoding="utf-8")
    w = csv.writer(out_csv_f)
    w.writerow(["t_sec", "face_ok", "dominant", "valence", "arousal",
                *[f"e_{k}" for k in HSEMO_8], "fwhr"])

    timeline = []
    frames_total = 0
    frames_with_face = 0
    idx = -1
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        idx += 1
        if idx % step != 0:
            continue
        t_sec = round(idx / src_fps, 2)
        frames_total += 1

        H, W = frame.shape[:2]
        sc = PROC_W / float(W) if W > PROC_W else 1.0
        small = cv2.resize(frame, (int(W * sc), int(H * sc))) if sc < 1 else frame
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        box = find_face(gray)

        face_ok = 0
        dominant, valence, arousal, fwhr = "-", 0.0, 0.0, 0.0
        scores = [0.0] * 8
        if box is not None:
            fx, fy, fw, fh = box
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
                    frames_with_face += 1
                    timeline.append({"dominant": dominant, "scores": dist,
                                     "valence": valence, "arousal": arousal,
                                     "fwhr": fwhr, "t_sec": t_sec})
                except Exception:
                    pass

        w.writerow([t_sec, face_ok, dominant, valence, arousal, *[round(s, 4) for s in scores], fwhr])

    cap.release()
    out_csv_f.close()

    # Сводка
    summary = {
        "ok": True,
        "module": "emotions",
        "video": video_path,
        "frames_sampled": frames_total,
        "frames_with_face": frames_with_face,
        "face_coverage": round(frames_with_face / frames_total, 3) if frames_total else 0.0,
        "csv": out_csv,
    }
    if timeline and _FP_OK:
        try:
            perc = fp.summarize_timeline(timeline)
            summary["perception"] = perc
        except Exception as e:
            summary["perception_error"] = str(e)
    if timeline:
        # Средние валентность/возбуждение и доминирующая эмоция по всему видео
        import collections
        dom = collections.Counter(t["dominant"] for t in timeline)
        mean_scores = {k: round(float(np.mean([t["scores"][k] for t in timeline])), 3) for k in HSEMO_8}
        summary["dominant_overall"] = dom.most_common(1)[0][0]
        summary["mean_scores"] = mean_scores
        # средние valence/arousal по всему клипу (нужны Neiry-стрессу в offline-движке)
        summary["valence"] = round(float(np.mean([t["valence"] for t in timeline])), 3)
        summary["arousal"] = round(float(np.mean([t["arousal"] for t in timeline])), 3)
        summary["available"] = True
    else:
        summary["available"] = False
        summary["note"] = "лицо не найдено ни в одном кадре (возможно audio-only / очки / ракурс)"

    Path(out_json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--sample-fps", type=float, default=5.0)
    args = ap.parse_args()
    res = analyze(args.video, args.out_csv, args.out_json, args.sample_fps)
    # stdout — компактный JSON для оркестратора
    print(json.dumps({k: v for k, v in res.items() if k in
                      ("ok", "available", "dominant_overall", "face_coverage", "error", "note")},
                     ensure_ascii=False))
    sys.exit(0 if res.get("ok") else 1)


if __name__ == "__main__":
    main()
