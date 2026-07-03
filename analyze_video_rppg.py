"""
analyze_video_rppg.py — оффлайн пульс-по-видео (rPPG) + РЕЧЬ по видео-ФАЙЛУ.

Запускать в venv_new:
    venv_new/bin/python analyze_video_rppg.py --video in.mp4 \
        --out-csv rppg.csv --out-json rppg.json [--sample-fps 5]

Два канала из одного файла:
  • Пульс: POS (Wang 2017) по ROI лба, скользящие окна → HR+SNR (логика из rppg_screen.py).
  • Речь: аудиодорожка (ffmpeg → wav 16k) → громкость/dB, доля речи, питч F0,
    темп, паузы, и прокси E/I (диапазон громкости + вариативность интонации).

КЛЮЧЕВОЕ: если лица нет (наушники/очки/только аудио) — пульс недоступен, но
**речевой канал работает** (режим audio-only, ровно как просили). Если нет
аудиодорожки — наоборот. Модуль не падает ни в одном из случаев.

Функции сигналов скопированы из rppg_screen.py (не импортируем его — он тянет
mss/PIL/sounddevice, не нужные для разбора файла).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import cv2
import numpy as np
from scipy.signal import butter, filtfilt, welch

HERE = Path(__file__).resolve().parent
_CASCADE = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"

HR_MIN_HZ, HR_MAX_HZ = 0.7, 3.5
HR_SNR_MIN = 3.0
TARGET_FPS = 15

AUDIO_SR = 16000
AUDIO_BLOCK = 1024
PITCH_MIN_HZ, PITCH_MAX_HZ = 70, 350


# ---------- сигналы (из rppg_screen.py) ----------
def bandpass(sig, fps, lo, hi):
    nyq = fps / 2.0
    lo_n, hi_n = max(lo / nyq, 1e-3), min(hi / nyq, 0.99)
    if hi_n <= lo_n:
        return sig
    b, a = butter(3, [lo_n, hi_n], btype="band")
    return filtfilt(b, a, sig)


def pos_signal(rgb, fps):
    eps = 1e-9
    n = rgb.shape[0]
    H = np.zeros(n)
    win = max(int(1.6 * fps), 8)
    proj = np.array([[0.0, 1.0, -1.0], [-2.0, 1.0, 1.0]])
    for s in range(0, max(1, n - win)):
        e = s + win
        C = rgb[s:e].T
        mean_c = np.mean(C, axis=1, keepdims=True)
        Cn = C / (mean_c + eps)
        S = proj @ Cn
        std0, std1 = np.std(S[0]), np.std(S[1])
        h = S[0] + (std0 / (std1 + eps)) * S[1]
        H[s:e] += h - np.mean(h)
    return H


def dominant_freq_bpm(sig, fps, lo, hi):
    if len(sig) < fps * 4:
        return None, 0.0
    f, pxx = welch(sig, fs=fps, nperseg=min(len(sig), int(fps * 8)))
    band = (f >= lo) & (f <= hi)
    if not np.any(band):
        return None, 0.0
    fb, pb = f[band], pxx[band]
    peak = np.argmax(pb)
    snr = pb[peak] / (np.mean(pb) + 1e-9)
    return fb[peak] * 60.0, float(snr)


def estimate_pitch(x, sr, fmin=PITCH_MIN_HZ, fmax=PITCH_MAX_HZ):
    x = x.astype(np.float32)
    x = x - x.mean()
    if x.std() < 0.005 or len(x) < int(sr / fmin) + 1:
        return None
    n = len(x)
    ac = np.correlate(x, x, mode="full")[n - 1:]
    if ac[0] <= 0:
        return None
    ac = ac / ac[0]
    lag_min = max(2, int(sr / fmax))
    lag_max = min(len(ac) - 1, int(sr / fmin))
    if lag_max - lag_min < 5:
        return None
    sub = ac[lag_min:lag_max]
    peak = int(np.argmax(sub)) + lag_min
    if ac[peak] < 0.35:
        return None
    return float(sr / peak)


# ---------- пульс по видео ----------
def analyze_pulse(video_path: str):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {"available": False, "error": "не открыть видео"}, []
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, int(round(src_fps / TARGET_FPS)))
    eff_fps = src_fps / step
    cascade = cv2.CascadeClassifier(_CASCADE)

    rgb_series = []          # mean RGB лба
    t_series = []
    valid = []
    box = None
    idx = -1
    n_face = 0
    detect_every = 4
    sampled = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        idx += 1
        if idx % step != 0:
            continue
        sampled += 1
        t_sec = idx / src_fps
        H, W = frame.shape[:2]
        if sampled % detect_every == 1 or box is None:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            fs = cascade.detectMultiScale(gray, 1.15, 5, minSize=(60, 60))
            if len(fs):
                box = tuple(sorted(fs, key=lambda b: b[2] * b[3])[-1])
        if box is not None:
            x, y, w, h = box
            # ROI лба: центр верхней трети лица
            rx0, rx1 = int(x + 0.3 * w), int(x + 0.7 * w)
            ry0, ry1 = int(y + 0.05 * h), int(y + 0.25 * h)
            rx0, ry0 = max(0, rx0), max(0, ry0)
            rx1, ry1 = min(W, rx1), min(H, ry1)
            roi = frame[ry0:ry1, rx0:rx1]
            if roi.size:
                b, g, r = [float(roi[:, :, c].mean()) for c in range(3)]
                rgb_series.append([r, g, b])
                t_series.append(t_sec)
                valid.append(True)
                n_face += 1
                continue
        rgb_series.append([np.nan, np.nan, np.nan])
        t_series.append(t_sec)
        valid.append(False)
    cap.release()

    timeline = []
    if n_face < eff_fps * 4:
        return {"available": False, "frames_with_face": n_face,
                "note": "лицо найдено слишком редко для пульса (audio-only?)"}, timeline

    rgb = np.array(rgb_series, dtype=float)
    # заполним короткие пропуски интерполяцией по каждому каналу
    for c in range(3):
        col = rgb[:, c]
        nans = np.isnan(col)
        if nans.any() and (~nans).sum() > 2:
            col[nans] = np.interp(np.flatnonzero(nans), np.flatnonzero(~nans), col[~nans])
            rgb[:, c] = col

    win = int(eff_fps * 10)
    stepw = max(1, int(eff_fps * 1))
    hrs = []
    for s in range(0, max(1, len(rgb) - win), stepw):
        seg = rgb[s:s + win]
        if np.isnan(seg).any():
            continue
        pulse = pos_signal(seg, eff_fps)
        pulse = bandpass(pulse, eff_fps, HR_MIN_HZ, HR_MAX_HZ)
        hr, snr = dominant_freq_bpm(pulse, eff_fps, HR_MIN_HZ, HR_MAX_HZ)
        if hr is not None and snr >= HR_SNR_MIN and 40 <= hr <= 180:
            t_c = round(t_series[min(s + win // 2, len(t_series) - 1)], 1)
            timeline.append((t_c, round(hr, 1), round(snr, 1)))
            hrs.append(hr)

    if not hrs:
        return {"available": False, "frames_with_face": n_face,
                "note": "пульс не выделился (низкий SNR — свет/движение/сжатие видео)"}, timeline
    return {
        "available": True,
        "frames_with_face": n_face,
        "eff_fps": round(eff_fps, 1),
        "hr_median": round(float(np.median(hrs)), 1),
        "hr_min": round(float(np.min(hrs)), 1),
        "hr_max": round(float(np.max(hrs)), 1),
        "windows": len(hrs),
    }, timeline


# ---------- речь ----------
def _extract_wav(video_path: str) -> str | None:
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
           "-ac", "1", "-ar", str(AUDIO_SR), "-vn", tmp.name]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception:
        return None
    if r.returncode != 0 or Path(tmp.name).stat().st_size < 1000:
        return None
    return tmp.name


def analyze_speech(video_path: str):
    wav = _extract_wav(video_path)
    if wav is None:
        return {"available": False, "note": "нет аудиодорожки или ffmpeg недоступен"}
    try:
        with wave.open(wav, "rb") as wf:
            sr = wf.getframerate()
            n = wf.getnframes()
            raw = wf.readframes(n)
    except Exception as e:
        return {"available": False, "error": f"чтение wav: {e}"}
    finally:
        try:
            Path(wav).unlink()
        except Exception:
            pass

    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if len(x) < sr:
        return {"available": False, "note": "аудио слишком короткое"}

    noise_floor = 0.002
    voiced_buf = np.zeros(0, dtype=np.float32)
    rms_voiced_db = []
    pitches = []
    voiced_flags = []
    onsets = 0
    prev_voiced = False
    silence_run = 0
    pauses = []
    block_sec = AUDIO_BLOCK / sr

    for i in range(0, len(x) - AUDIO_BLOCK, AUDIO_BLOCK):
        blk = x[i:i + AUDIO_BLOCK]
        rms = float(np.sqrt(np.mean(blk * blk)) + 1e-12)
        thr = max(noise_floor * 3.5, 0.004)
        voiced = rms > thr
        if not voiced:
            noise_floor = 0.95 * noise_floor + 0.05 * rms
            silence_run += 1
        else:
            if silence_run * block_sec > 1.0:
                pauses.append(silence_run * block_sec)
            silence_run = 0
            rms_voiced_db.append(20.0 * np.log10(rms + 1e-9))
            voiced_buf = np.concatenate([voiced_buf, blk])[-int(sr * 0.7):]
            if len(voiced_buf) >= int(sr * 0.3):
                p = estimate_pitch(voiced_buf, sr)
                if p:
                    pitches.append(p)
        if voiced and not prev_voiced:
            onsets += 1
        prev_voiced = voiced
        voiced_flags.append(voiced)

    total = len(voiced_flags)
    dur_min = (total * block_sec) / 60.0 if total else 0.0
    speech_ratio = round(sum(voiced_flags) / total, 3) if total else 0.0
    if not rms_voiced_db:
        return {"available": False, "note": "речь не обнаружена (тишина/музыка)"}

    db = np.array(rms_voiced_db)
    loud_iqr = float(np.percentile(db, 75) - np.percentile(db, 25))
    pitch_std = round(float(np.std(pitches)), 1) if len(pitches) > 1 else 0.0
    pitch_mean = round(float(np.mean(pitches)), 1) if pitches else 0.0
    tempo = round(onsets / dur_min, 1) if dur_min > 0 else 0.0

    # Прокси E/I: широкий динамический диапазон громкости + вариативная интонация → экстраверсия
    ei_score = round(loud_iqr / 6.0 + pitch_std / 25.0, 2)  # ~0..2+, эмпирически
    if ei_score >= 1.3:
        ei_label = "скорее экстраверсия (широкий диапазон громкости + живая интонация)"
    elif ei_score <= 0.7:
        ei_label = "скорее интроверсия (ровная громкость/интонация)"
    else:
        ei_label = "амбиверсия / неопределённо"

    return {
        "available": True,
        "duration_min": round(dur_min, 2),
        "speech_ratio": speech_ratio,
        "pitch_mean_hz": pitch_mean,
        "pitch_std_hz": pitch_std,
        "loudness_iqr_db": round(loud_iqr, 2),
        "tempo_onsets_per_min": tempo,
        "pauses_over_1s": len(pauses),
        "pause_mean_sec": round(float(np.mean(pauses)), 2) if pauses else 0.0,
        "ei_score": ei_score,
        "ei_label": ei_label,
    }


def analyze(video_path: str, out_csv: str, out_json: str, sample_fps: float = 5.0) -> dict:
    pulse, timeline = analyze_pulse(video_path)
    speech = analyze_speech(video_path)

    # CSV пульса по времени
    import csv as _csv
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        wcsv = _csv.writer(f)
        wcsv.writerow(["t_sec", "hr_bpm", "snr"])
        for row in timeline:
            wcsv.writerow(row)

    summary = {
        "ok": True, "module": "rppg", "video": video_path, "csv": out_csv,
        "pulse": pulse, "speech": speech,
        "available": bool(pulse.get("available") or speech.get("available")),
        "mode": ("полный" if pulse.get("available") and speech.get("available")
                 else "audio-only" if speech.get("available")
                 else "video-only" if pulse.get("available") else "нет данных"),
    }
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
    print(json.dumps({"ok": res["ok"], "mode": res["mode"],
                      "pulse": res["pulse"].get("available"),
                      "speech": res["speech"].get("available")}, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
