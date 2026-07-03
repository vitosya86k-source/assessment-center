"""
combo_live_daemon.py — ЖИВОЙ захват → метрики в реальном времени → combo_data.json.

Это недостающий live-режим комбо-инструмента: один процесс берёт кадры (экран или
видео-файл), считает каналы (поза, пульс-по-видео, речь; эмоции — опциональный
companion в emo_venv) и каждые ~1.5с пишет webapp/combo_data.json, который
webapp/combo_live.html уже поллит. Никаких 3 OpenCV-окон и клавиш — всё в веб-панели.

Запуск (venv_new):
  # боевой: захват зоны участника на экране
  venv_new/bin/python combo_live_daemon.py --source screen --zone 0,0,960,540
  # тест/разбор: прогон по видео-файлу (зацикленно)
  venv_new/bin/python combo_live_daemon.py --source video.mp4 --duration 15

Кросс-venv: эмоции (emo_venv) пишут combo/live/emo_state.json — демон подхватывает,
если файл есть. Это последовательно-кооперативно (через файлы), а не 3 тяжёлых
realtime-процесса разом — то, что раньше вешало систему, исключено по дизайну.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import tempfile
import threading
import time
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from analyze_video_rppg import (pos_signal, bandpass, dominant_freq_bpm, estimate_pitch,
                                HR_MIN_HZ, HR_MAX_HZ, HR_SNR_MIN, AUDIO_BLOCK)
import combo_config as cfg
from combo_neiry import compute_neiry, summary_cards, compute_resilience

_CASCADE = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
LIVE_DIR = cfg.LIVE_DIR
EMO_STATE = LIVE_DIR / "emo_state.json"
OUT_JSON = cfg.WEBAPP_DIR / "combo_data.json"

C = {"ok": "#00d4aa", "good": "#a8e063", "watch": "#ff8c42", "signal": "#ffd93d", "crit": "#ff4757"}

# окно стресс-индекса для стрессоустойчивости/тренда (live, ~30 с при интервале 1.5 с)
_STRESS_HIST = deque(maxlen=20)


def pick_participant(boxes, frame_h, locked=None, top_frac=0.55, max_jump=0.25):
    """Выбор плитки участника. Granatum: участник закреплён сверху, но порядок
    плиток может сдвигаться (ушёл асессор). Поэтому: если уже зафиксировали лицо —
    следуем за БЛИЖАЙШИМ к нему (пространственная непрерывность, не «верхний по списку»);
    если потеряли — переакквизиция: крупнейшее лицо в верхней зоне, иначе крупнейшее.
    boxes: список (x,y,w,h). Возвращает выбранный box или locked, если лиц нет.
    """
    if not boxes:
        return locked
    def cx(b): return b[0] + b[2] / 2.0
    def cy(b): return b[1] + b[3] / 2.0
    def area(b): return b[2] * b[3]
    if locked is not None:
        lcx, lcy = cx(locked), cy(locked)
        near = min(boxes, key=lambda b: (cx(b) - lcx) ** 2 + (cy(b) - lcy) ** 2)
        if ((cx(near) - lcx) ** 2 + (cy(near) - lcy) ** 2) ** 0.5 <= max_jump * frame_h:
            return near
    top = [b for b in boxes if cy(b) <= top_frac * frame_h]
    return max(top or boxes, key=area)


# ---------------- аудио ----------------
class AudioState:
    """Скользящие речевые метрики за последние ~30с."""
    def __init__(self, sr=16000, win_sec=30):
        self.sr = sr
        self.lock = threading.Lock()
        maxlen = int(win_sec * sr / AUDIO_BLOCK)
        self.voiced = deque(maxlen=maxlen)
        self.rms_db = deque(maxlen=maxlen)
        self.noise = 0.002
        self.buf = np.zeros(0, dtype=np.float32)
        self.pitches = deque(maxlen=maxlen)
        self.onsets = deque(maxlen=maxlen)
        self._prev_voiced = False
        self._last_pitch_t = 0.0

    def update(self, block: np.ndarray):
        rms = float(np.sqrt(np.mean(block * block)) + 1e-12)
        thr = max(self.noise * 3.5, 0.004)
        v = rms > thr
        if not v:
            self.noise = 0.95 * self.noise + 0.05 * rms
        with self.lock:
            self.voiced.append(1 if v else 0)
            self.onsets.append(1 if (v and not self._prev_voiced) else 0)
            if v:
                self.rms_db.append(20 * np.log10(rms + 1e-9))
                self.buf = np.concatenate([self.buf, block])[-int(self.sr * 0.7):]
                now = time.monotonic()
                if len(self.buf) >= int(self.sr * 0.3) and now - self._last_pitch_t > 0.25:
                    p = estimate_pitch(self.buf, self.sr)
                    self._last_pitch_t = now
                    if p:
                        self.pitches.append(p)
            self._prev_voiced = v

    def snapshot(self):
        with self.lock:
            n = len(self.voiced)
            if n < 5:
                return None
            speech_ratio = sum(self.voiced) / n
            secs = n * AUDIO_BLOCK / self.sr
            tempo = sum(self.onsets) / (secs / 60.0) if secs > 0 else 0
            pmean = float(np.mean(self.pitches)) if self.pitches else 0.0
            db = np.array(self.rms_db) if self.rms_db else np.array([0.0])
            loud_iqr = float(np.percentile(db, 75) - np.percentile(db, 25)) if len(db) > 3 else 0.0
            pstd = float(np.std(self.pitches)) if len(self.pitches) > 1 else 0.0
        ei = round(loud_iqr / 6.0 + pstd / 25.0, 2)
        volume_db = round(float(np.mean(db)), 1) if len(db) else 0.0
        return {"speech_ratio": round(speech_ratio, 2), "tempo": round(tempo, 1),
                "pitch": round(pmean, 0), "ei": ei,
                "volume_db": volume_db, "loud_iqr": round(loud_iqr, 1),
                "pitch_std": round(pstd, 0), "pause_pct": round((1 - speech_ratio) * 100)}


def mic_thread(astate: AudioState, stop):
    import sounddevice as sd
    def cb(indata, frames, t, status):
        x = indata[:, 0] if indata.ndim > 1 else indata
        astate.update(np.asarray(x, dtype=np.float32))
    # COMBO_AUDIO_MONITOR = монитор системного звука (чистый звук звонка, ровный уровень).
    # Сначала пробуем его, иначе обычный микрофон. Так речь не зависит от громкости колонок.
    mon = os.environ.get("COMBO_AUDIO_MONITOR", "").strip()
    for dev in ([mon] if mon else []) + [None]:
        try:
            with sd.InputStream(channels=1, samplerate=astate.sr, blocksize=AUDIO_BLOCK,
                                callback=cb, device=dev):
                print(f"[audio] источник: {dev or 'микрофон по умолчанию'}")
                while not stop.is_set():
                    time.sleep(0.1)
            return
        except Exception as e:
            print(f"[audio] источник {dev} недоступен: {e}")
    print("[audio] звук недоступен")


def wav_stream_thread(astate: AudioState, wav_path: str, stop):
    """Для видео-источника: проигрывает аудиодорожку в реальном времени в AudioState."""
    try:
        with wave.open(wav_path, "rb") as wf:
            sr = wf.getframerate()
            astate.sr = sr
            while not stop.is_set():
                raw = wf.readframes(AUDIO_BLOCK)
                if not raw:
                    wf.rewind(); continue
                x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                astate.update(x)
                time.sleep(AUDIO_BLOCK / sr)
    except Exception as e:
        print(f"[audio] wav-стрим недоступен: {e}")


def extract_wav(video: str):
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False); tmp.close()
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video, "-ac", "1", "-ar", "16000", "-vn", tmp.name]
    try:
        subprocess.run(cmd, capture_output=True, timeout=120)
        if Path(tmp.name).stat().st_size > 1000:
            return tmp.name
    except Exception:
        pass
    return None


# ---------------- видео-каналы (поза + rPPG) ----------------
class VisionState:
    def __init__(self):
        self.lock = threading.Lock()
        self.rgb = deque(maxlen=200)        # (t, r,g,b) лоб
        self.hand = deque(maxlen=300)        # 0/1 рука у лица
        self.fidget = 0.0
        self.head_tilt = 0.0
        self.pose_ok = 0
        self.eff_fps = 10.0
        self.hr = None
        self.snr = 0.0

    def hr_now(self):
        with self.lock:
            if len(self.rgb) < self.eff_fps * 5:
                return None
            arr = np.array([c for _, c in self.rgb], dtype=float)
            fps = self.eff_fps
        if np.isnan(arr).any():
            return None
        sig = bandpass(pos_signal(arr, fps), fps, HR_MIN_HZ, HR_MAX_HZ)
        hr, snr = dominant_freq_bpm(sig, fps, HR_MIN_HZ, HR_MAX_HZ)
        self.snr = round(float(snr), 1)
        if hr and snr >= HR_SNR_MIN and 40 <= hr <= 180:
            return round(hr, 0)
        return None

    def resp_now(self):
        """Частота дыхания (вдохов/мин) — низкочастотная модуляция rPPG-сигнала (0.13–0.5 Гц)."""
        with self.lock:
            if len(self.rgb) < self.eff_fps * 12:
                return None
            arr = np.array([c for _, c in self.rgb], dtype=float)
            fps = self.eff_fps
        if np.isnan(arr).any():
            return None
        try:
            sig = bandpass(pos_signal(arr, fps), fps, 0.13, 0.5)
            rpm, snr = dominant_freq_bpm(sig, fps, 0.13, 0.5)
            if rpm and snr >= 1.5 and 6 <= rpm <= 30:
                return round(rpm, 0)
        except Exception:
            pass
        return None


def vision_thread(vstate: VisionState, source, zone, stop, target_fps=10, participant_top_frac=0.55):
    import mss
    try:
        import mediapipe as mp
        from mediapipe.tasks import python as mpp
        from mediapipe.tasks.python import vision
        model = cfg.BASE_DIR / "pose_landmarker_lite.task"
        landmarker = None
        if model.exists():
            opts = vision.PoseLandmarkerOptions(
                base_options=mpp.BaseOptions(model_asset_path=str(model)),
                running_mode=vision.RunningMode.VIDEO, num_poses=1)
            landmarker = vision.PoseLandmarker.create_from_options(opts)
    except Exception as e:
        print(f"[pose] MediaPipe недоступен: {e}")
        landmarker = None

    cascade = cv2.CascadeClassifier(_CASCADE)
    is_video = source != "screen"
    cap = cv2.VideoCapture(source) if is_video else None
    sct = mss.mss() if not is_video else None
    if not is_video:
        mon = sct.monitors[1]
        if zone:
            zx, zy, zw, zh = zone
            grab = {"top": mon["top"] + zy, "left": mon["left"] + zx, "width": zw, "height": zh}
        else:
            grab = mon
    src_fps = (cap.get(cv2.CAP_PROP_FPS) or 25.0) if is_video else target_fps
    step = max(1, int(round(src_fps / target_fps))) if is_video else 1

    prev_nose = prev_sh = None
    fidget_ema = 0.0
    box = None
    fi = -1
    ts0 = time.monotonic()
    t_last = 0.0
    last_save = 0.0
    while not stop.is_set():
        if is_video:
            ok, frame = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0); continue
            fi += 1
            if fi % step != 0:
                continue
        else:
            frame = np.ascontiguousarray(np.array(sct.grab(grab))[:, :, :3])
        t = time.monotonic() - ts0
        H, W = frame.shape[:2]

        # лицо → ROI лба для rPPG. Авто-выбор участника (верхняя зона + фиксация).
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        fs = cascade.detectMultiScale(gray, 1.15, 5, minSize=(60, 60))
        if len(fs):
            box = pick_participant([tuple(int(v) for v in b) for b in fs], H, box,
                                   top_frac=participant_top_frac)
        # box (Haar) оставляем только для превью-кадра frame.jpg; ROI лба для rPPG
        # берём из ПОЗЫ (MediaPipe) ниже — Haar по плитке с экрана лицо не находит, а поза находит.

        # кадр участника в панель — поток ~6-7 fps (живое видео, не фото раз в секунду)
        if time.monotonic() - last_save > 0.15:
            last_save = time.monotonic()
            try:
                crop = frame  # показываем всю зону участника целиком (не режем по Haar — макушка не обрежется)
                if crop.size:
                    cw = 360
                    ch = max(1, int(crop.shape[0] * cw / crop.shape[1]))
                    small = cv2.resize(crop, (cw, ch))
                    tmpf = cfg.WEBAPP_DIR / "frame.tmp.jpg"
                    cv2.imwrite(str(tmpf), small, [cv2.IMWRITE_JPEG_QUALITY, 72])
                    tmpf.replace(cfg.WEBAPP_DIR / "frame.jpg")
            except Exception:
                pass

        # поза
        if landmarker is not None:
            try:
                rgb_img = np.ascontiguousarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                import mediapipe as mp
                res = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_img),
                                                  int(t * 1000))
            except Exception:
                res = None
            if res and res.pose_landmarks:
                lm = res.pose_landmarks[0]
                def P(i): return (lm[i].x, lm[i].y)
                nose, lsh, rsh, lwr, rwr = P(0), P(11), P(12), P(15), P(16)
                sw = (((lsh[0]-rsh[0])**2+(lsh[1]-rsh[1])**2)**.5) + 1e-6
                d = min(((lwr[0]-nose[0])**2+(lwr[1]-nose[1])**2)**.5,
                        ((rwr[0]-nose[0])**2+(rwr[1]-nose[1])**2)**.5)
                hand = 1 if d < 0.55 * sw else 0
                lear, rear = P(7), P(8)
                tilt = float(np.degrees(np.arctan2(rear[1]-lear[1], rear[0]-lear[0])))
                # уши в кадре могут идти R→L (зеркало/ракурс) → база ~±180°; сводим к отклонению от горизонтали (0 = прямо)
                if tilt > 90:
                    tilt -= 180
                elif tilt < -90:
                    tilt += 180
                sh_mid = ((lsh[0]+rsh[0])/2, (lsh[1]+rsh[1])/2)
                if prev_nose is not None:
                    mv = (((nose[0]-prev_nose[0])**2+(nose[1]-prev_nose[1])**2)**.5 +
                          ((sh_mid[0]-prev_sh[0])**2+(sh_mid[1]-prev_sh[1])**2)**.5)
                    fidget_ema = 0.8*fidget_ema + 0.2*mv
                prev_nose, prev_sh = nose, sh_mid
                # лоб из позы → rPPG (надёжнее Haar на плитке с экрана)
                leye, reye = P(2), P(5)
                ed = ((leye[0]-reye[0])**2 + (leye[1]-reye[1])**2)**.5
                if ed > 0.004:
                    ecx, ecy = (leye[0]+reye[0])/2, (leye[1]+reye[1])/2
                    fx0 = int(max(0.0, ecx-ed*0.6)*W); fx1 = int(min(1.0, ecx+ed*0.6)*W)
                    fy0 = int(max(0.0, ecy-ed*1.05)*H); fy1 = int(max(0.0, ecy-ed*0.5)*H)
                    if fx1 > fx0 and fy1 > fy0:
                        froi = frame[fy0:fy1, fx0:fx1]
                        if froi.size:
                            bb, gg, rr = [float(froi[:, :, c].mean()) for c in range(3)]
                            with vstate.lock:
                                vstate.rgb.append((t, [rr, gg, bb]))
                    # рамка лица (нормализованная) → companion эмоций читает её вместо Haar
                    try:
                        ew = abs(lear[0]-rear[0]) + 1e-6
                        fb = {"x": max(0.0, min(lear[0], rear[0]) - 0.1*ew), "y": max(0.0, ecy - 0.6*ew),
                              "w": min(1.0, ew*1.2), "h": min(1.0, ew*1.8), "t": time.time()}
                        fp = cfg.LIVE_DIR / "face_box.json"
                        fp.with_suffix(".tmp").write_text(json.dumps(fb))
                        fp.with_suffix(".tmp").replace(fp)
                    except Exception:
                        pass
                with vstate.lock:
                    vstate.pose_ok = 1; vstate.hand.append(hand)
                    vstate.fidget = round(fidget_ema, 4); vstate.head_tilt = round(tilt, 1)
            else:
                with vstate.lock:
                    vstate.pose_ok = 0
                prev_nose = prev_sh = None

        # держим целевой fps для screen
        if not is_video:
            dt = 1.0/target_fps - (time.monotonic()-ts0 - t)
            if dt > 0:
                time.sleep(dt)
    if cap: cap.release()


# ---------------- сборка combo_data.json ----------------
def _status(value, lo_ok, hi_ok, crit_hi=None):
    if crit_hi is not None and value >= crit_hi:
        return "CRIT"
    return "NORMA" if lo_ok <= value <= hi_ok else "NABL"


def build_payload(vstate, astate, elapsed):
    channels, alerts = [], []
    # речь
    sp = astate.snapshot() if astate else None
    if sp:
        # темп через микрофон-колонки завышается ложными онсетами → высокий порог, без «критично»
        temp_st = "NABL" if sp["tempo"] > 150 else "NORMA"
        channels.append({"title": "Речь", "icon": "💬", "rows": [
            {"nm": "Питч F0", "val": f"{int(sp['pitch'])} гц", "min": 70, "max": 350, "value": sp["pitch"],
             "zones": [[120, C["signal"]], [260, C["ok"]], [350, C["signal"]]], "st": "NORMA",
             "desc": "Средняя высота голоса (частота основного тона). Резкий рост питча часто сопровождает стресс/волнение."},
            {"nm": "Темп", "val": f"{sp['tempo']}/мин", "min": 0, "max": 60, "value": sp["tempo"],
             "zones": [[40, C["ok"]], [50, C["signal"]], [60, C["crit"]]], "st": temp_st},
            {"nm": "Доля речи", "val": f"{round(sp['speech_ratio']*100)} %", "min": 0, "max": 100,
             "value": sp["speech_ratio"]*100, "zones": [[30, C["signal"]], [80, C["ok"]], [100, C["signal"]]], "st": "NORMA"},
            {"nm": "Громкость (средняя)", "val": f"{sp.get('volume_db',0)} дБ", "min": -50, "max": 0,
             "value": sp.get("volume_db", -40), "zones": [[-40, C["signal"]], [-12, C["ok"]], [0, C["signal"]]], "st": "NORMA"},
            {"nm": "Громкость (вариативность)", "val": f"{sp.get('loud_iqr',0)} дБ", "min": 0, "max": 12,
             "value": sp.get("loud_iqr", 0), "zones": [[3, C["signal"]], [8, C["ok"]], [12, C["signal"]]],
             "lo": "Монотонно", "hi": "Выразительно", "st": "NORMA",
             "desc": "Разброс громкости (межквартильный, дБ). Низкий → монотонная подача; высокий → выразительная, акцентированная."},
            {"nm": "Вариативность интонации", "val": f"{int(sp.get('pitch_std',0))} гц", "min": 0, "max": 60,
             "value": sp.get("pitch_std", 0), "zones": [[12, C["signal"]], [45, C["ok"]], [60, C["signal"]]],
             "lo": "Плоско", "hi": "Живо", "st": "NORMA",
             "desc": "Разброс высоты голоса (СКО питча). Низкий → плоско, устало, отстранённо; высокий → живая интонация, вовлечённость."},
            {"nm": "Доля пауз", "val": f"{sp.get('pause_pct',0)} %", "min": 0, "max": 100,
             "value": sp.get("pause_pct", 0), "zones": [[20, C["ok"]], [50, C["signal"]], [100, C["watch"]]], "st": "NORMA"},
        ]})
        if temp_st == "CRIT":
            alerts.append({"t": "Темп речи — ускорен", "v": f"{sp['tempo']}/мин", "sub": "Темп выше нормы", "st": "CRIT"})
    # эмоции (companion)
    emo = None
    if EMO_STATE.exists():
        try:
            emo = json.loads(EMO_STATE.read_text(encoding="utf-8"))
        except Exception:
            emo = None
    if emo and emo.get("face_ok"):
        _RU = {"Anger": "Злость", "Contempt": "Презрение", "Disgust": "Отвращение", "Fear": "Страх",
               "Happiness": "Радость", "Neutral": "Нейтральность", "Sadness": "Грусть", "Surprise": "Удивление"}
        erows = [
            {"nm": "Валентность", "val": f"{emo.get('valence',0):+.2f}", "min": -1, "max": 1, "value": emo.get("valence", 0),
             "zones": [[-.2, C["watch"]], [1, C["ok"]]], "lo": "Негативная", "hi": "Позитивная", "st": "NORMA",
             "desc": "Эмоциональный тон лица: от негативного (−1) к позитивному (+1)."},
            {"nm": "Возбуждение", "val": f"{emo.get('arousal',0):.2f}", "min": 0, "max": 1, "value": emo.get("arousal", 0),
             "zones": [[.5, C["ok"]], [.75, C["signal"]], [1, C["watch"]]], "lo": "Спокойствие", "hi": "Возбуждение", "st": "NORMA",
             "desc": "Эмоциональная активация: от спокойствия (0) к возбуждению (1)."},
            {"nm": "Доминирующая эмоция", "val": _RU.get(emo.get("dominant"), emo.get("dominant", "—")),
             "min": 0, "max": 1, "value": .6, "zones": [[1, C["ok"]]], "st": "NORMA"},
            {"nm": "Доверие (как считывается)", "val": f"{emo.get('perceived_trust',0):.2f}", "min": 0, "max": 1,
             "value": emo.get("perceived_trust", 0), "zones": [[.35, C["watch"]], [1, C["ok"]]],
             "lo": "Настороженно", "hi": "Открыто", "st": "NORMA",
             "desc": "Воспринимаемое доверие (Тодоров) — как лицо может считываться окружающими, не характер."},
            {"nm": "Доминантность (как считывается)", "val": f"{emo.get('perceived_dominance',0):.2f}", "min": 0, "max": 1,
             "value": emo.get("perceived_dominance", 0), "zones": [[.4, C["ok"]], [.7, C["signal"]], [1, C["watch"]]],
             "lo": "Мягко", "hi": "Жёстко", "st": "NORMA",
             "desc": "Воспринимаемая доминантность (Тодоров) — первое впечатление собранности/жёсткости."},
            {"nm": "Лабильность (переключений/мин)", "val": f"{emo.get('switches_per_min',0)}", "min": 0, "max": 15,
             "value": emo.get("switches_per_min", 0), "zones": [[3, C["signal"]], [8, C["ok"]], [15, C["watch"]]], "st": "NORMA",
             "desc": "Как часто меняется доминирующая эмоция. Низко → ровная мимика; высоко → лабильная."},
            {"nm": "Стабильность мимики", "val": f"{emo.get('emotional_stability_pct',0)} %", "min": 0, "max": 100,
             "value": emo.get("emotional_stability_pct", 0), "zones": [[40, C["watch"]], [100, C["ok"]]], "st": "NORMA"},
        ]
        _em = emo.get("emotions") or {}
        for _k in ["Happiness", "Neutral", "Surprise", "Sadness", "Anger", "Fear", "Disgust", "Contempt"]:
            _v = _em.get(_k)
            if _v is not None:
                erows.append({"nm": _RU[_k], "val": f"{round(_v*100)} %", "min": 0, "max": 100,
                              "value": _v*100, "zones": [[100, C["ok"]]], "st": "NORMA"})
        channels.append({"title": "Эмоции", "icon": "🙂", "rows": erows})
    # поза
    with vstate.lock:
        pose_ok = vstate.pose_ok
        hand_rate = (sum(vstate.hand)/len(vstate.hand)) if vstate.hand else 0
        fidget = vstate.fidget; tilt = vstate.head_tilt
    snr = round(float(getattr(vstate, "snr", 0.0)), 1)
    if pose_ok:
        channels.append({"title": "Поза", "icon": "🧍", "rows": [
            {"nm": "Рука у лица", "val": f"{round(hand_rate*100)} %", "min": 0, "max": 60, "value": hand_rate*100,
             "zones": [[10, C["ok"]], [25, C["signal"]], [60, C["watch"]]], "st": _status(hand_rate*100, 0, 10, 25)},
            {"nm": "Ёрзанье", "val": f"{fidget}", "min": 0, "max": .05, "value": fidget,
             "zones": [[.01, C["ok"]], [.03, C["signal"]], [.05, C["watch"]]], "st": "NORMA"},
            {"nm": "Наклон головы", "val": f"{tilt:+.0f}°", "min": -30, "max": 30, "value": tilt,
             "zones": [[-10, C["signal"]], [10, C["ok"]], [30, C["signal"]]], "lo": "Влево", "hi": "Вправо", "st": "NORMA"},
        ]})
    # Экстраверсия — мультимодально: вокальная энергия + выразительность мимики + подвижность
    ext_parts = []
    if sp:
        ext_parts.append(min(1.0, sp.get("ei", 0) / 2.0))
    if emo and emo.get("face_ok"):
        ext_parts.append(min(1.0, emo.get("arousal", 0)))
        if emo.get("expressivity") is not None:
            ext_parts.append(min(1.0, emo["expressivity"] / 14.0))
    ext_parts.append(min(1.0, fidget / 0.025))
    if ext_parts:
        extr = sum(ext_parts) / len(ext_parts)
        for _ch in channels:
            if _ch["title"] == "Речь":
                _ch["rows"].insert(0, {"nm": "Экстраверсия ↔ Интроверсия", "val": f"{extr:.2f}",
                                       "min": 0, "max": 1, "value": extr,
                                       "zones": [[.35, C["signal"]], [.7, C["ok"]], [1, C["good"]]],
                                       "lo": "Интроверсия", "hi": "Экстраверсия", "st": "NORMA",
                                       "desc": "Мультимодально: вокальная энергия + выразительность мимики + подвижность. Это полюс, не «плохо/хорошо»."})
                break

    # пульс
    hr = vstate.hr_now()
    if hr:
        channels.append({"title": "Пульс по видео", "icon": "❤️", "rows": [
            {"nm": "ЧСС", "val": f"{int(hr)} уд/мин", "min": 40, "max": 160, "value": hr,
             "zones": [[60, C["signal"]], [100, C["ok"]], [160, C["crit"]]], "st": _status(hr, 60, 100, 130)},
            {"nm": "Качество сигнала (SNR)", "val": f"{snr}", "min": 0, "max": 12, "value": snr,
             "zones": [[2, C["crit"]], [4, C["signal"]], [12, C["ok"]]], "st": _status(snr, 4, 12),
             "desc": "Соотношение сигнал/шум пульса. Низкое → лицо плохо видно, тень или движение; ЧСС в это время менее надёжна."},
        ]})

    # дыхание + нервная система (Павлов: возбуждение↔торможение) + гибкость — live-композиты
    resp = vstate.resp_now()
    ns_rows = []
    if resp:
        ns_rows.append({"nm": "Дыхание", "val": f"{int(resp)} вд/мин", "min": 6, "max": 30, "value": resp,
                        "zones": [[10, C["signal"]], [20, C["ok"]], [30, C["watch"]]], "st": _status(resp, 10, 20, 26),
                        "desc": "Частота дыхания по низкочастотной модуляции rPPG-сигнала. Учащение → напряжение/волнение."})
    exc = []
    if emo and emo.get("face_ok"):
        exc.append(min(1.0, max(0.0, emo.get("arousal", 0))))
    if hr:
        exc.append(min(1.0, max(0.0, (hr-60)/50.0)))
    exc.append(min(1.0, fidget/0.03))
    if sp:
        exc.append(min(1.0, sp["tempo"]/55.0))
        exc.append(min(1.0, max(0.0, (sp["speech_ratio"]-0.2)/0.6)))
    if exc:
        pav = sum(exc)/len(exc)
        ns_rows.append({"nm": "Возбуждение ↔ Торможение (Павлов)", "val": f"{pav:.2f}", "min": 0, "max": 1, "value": pav,
                        "zones": [[.35, C["ok"]], [.65, C["signal"]], [1, C["watch"]]],
                        "lo": "Торможение", "hi": "Возбуждение", "st": "NORMA",
                        "desc": "Баланс нервных процессов (live-прокси): активация лица + пульс + подвижность + темп речи. ↑ → преобладает возбуждение."})
    flx = []
    if emo and emo.get("switches_per_min") is not None:
        flx.append(min(1.0, emo["switches_per_min"]/10.0))
    if sp:
        flx.append(min(1.0, sp.get("pitch_std", 0)/40.0))
    flx.append(min(1.0, fidget/0.025))
    if flx:
        flex = sum(flx)/len(flx)
        ns_rows.append({"nm": "Гибкость (подвижность реакций)", "val": f"{flex:.2f}", "min": 0, "max": 1, "value": flex,
                        "zones": [[.25, C["watch"]], [.7, C["ok"]], [1, C["signal"]]],
                        "lo": "Ригидно", "hi": "Гибко", "st": "NORMA",
                        "desc": "Подвижность реакций (live-прокси): переключения эмоций + вариативность интонации + микродвижения."})
    if ns_rows:
        channels.append({"title": "Нервная система (live)", "icon": "🧠", "rows": ns_rows})

    # --- Neiry-блок: композитные индексы состояния (стресс/утомление/вовлечённость) + вывод ---
    _emap = (emo.get("emotions") if (emo and emo.get("face_ok")) else None) or {}
    ni = compute_neiry(
        hr=hr, resp=resp,
        valence=emo.get("valence") if emo else None,
        arousal=emo.get("arousal") if emo else None,
        e_anger=_emap.get("Anger"), e_fear=_emap.get("Fear"),
        emo_stability=emo.get("emotional_stability_pct") if emo else None,
        tempo=sp["tempo"] if sp else None,
        pause_pct=sp.get("pause_pct") if sp else None,
        pitch_std=sp.get("pitch_std") if sp else None,
        loud_iqr=sp.get("loud_iqr") if sp else None,
        speech_ratio=sp.get("speech_ratio") if sp else None,
        fidget=fidget, head_tilt=tilt,
        face_present=emo.get("face_ok") if emo else None,
    )
    # стрессоустойчивость + тренд по окну стресс-индекса (live-динамика, не одномоментно)
    if ni["stress"] is not None:
        _STRESS_HIST.append(ni["stress"])
    resilience = compute_resilience(list(_STRESS_HIST))
    stress_trend = (_STRESS_HIST[-1] - _STRESS_HIST[0]) if len(_STRESS_HIST) >= 4 else None
    ni_rows = []
    if ni["stress"] is not None:
        ni_rows.append({"nm": "Стресс-индекс", "val": f"{ni['stress']}", "min": 0, "max": 100,
                        "value": ni["stress"], "zones": [[30, C["ok"]], [60, C["signal"]], [100, C["watch"]]],
                        "st": _status(ni["stress"], 0, 60, 80),
                        "desc": "Композит (live-прокси): возбуждение + пульс + дыхание + негативная валентность + напряжение мимики. Не медизмерение."})
    if ni["fatigue"] is not None:
        ni_rows.append({"nm": "Утомление", "val": f"{ni['fatigue']}", "min": 0, "max": 100,
                        "value": ni["fatigue"], "zones": [[30, C["ok"]], [60, C["signal"]], [100, C["watch"]]],
                        "st": _status(ni["fatigue"], 0, 60, 80),
                        "desc": "Композит (live-прокси): снижение активации + монотонная интонация + замедление речи + паузы + оседание позы. Моргания (PERCLOS) добавятся позже."})
    if ni.get("engagement") is not None:
        ni_rows.append({"nm": "Вовлечённость/фокус", "val": f"{ni['engagement']}", "min": 0, "max": 100,
                        "value": ni["engagement"], "zones": [[40, C["watch"]], [60, C["signal"]], [100, C["ok"]]],
                        "st": _status(ni["engagement"], 60, 100),
                        "desc": "Поведенческий прокси (только видео): неподвижность + ориентация головы + лицо в кадре. Взгляд (gaze) добавится с FaceLandmarker."})
    if resilience is not None:
        ni_rows.append({"nm": "Стрессоустойчивость (live)", "val": f"{resilience}", "min": 0, "max": 100,
                        "value": resilience, "zones": [[40, C["watch"]], [60, C["signal"]], [100, C["ok"]]],
                        "st": _status(resilience, 60, 100),
                        "desc": "Live-прокси саморегуляции: восстановление стресса после пика + низкая волатильность "
                                "по окну ~30 с. НЕ путать с осью SR паутинки (та из HRV-датчика). Копинг-стратегии — из речи отдельно."})
    if ni_rows:
        ni_rows.insert(0, {"nm": "Вывод", "val": ni["verdict"], "min": 0, "max": 1, "value": 1,
                           "zones": [[1, C["ok"]]], "st": "NORMA",
                           "desc": "Краткое резюме состояния по совокупности каналов."})
        channels.insert(0, {"title": "Состояние (Neiry)", "icon": "📊", "rows": ni_rows})

    # --- Карточки-«выводы» (раздел B плана Neiry): человекочитаемые итоги САМЫМ верхом ---
    cards = summary_cards(
        stress=ni["stress"], fatigue=ni["fatigue"], engagement=ni.get("engagement"),
        trust=emo.get("perceived_trust") if (emo and emo.get("face_ok")) else None,
        dominance=emo.get("perceived_dominance") if (emo and emo.get("face_ok")) else None,
        pitch_std=sp.get("pitch_std") if sp else None,
        pause_pct=sp.get("pause_pct") if sp else None,
        tempo=sp["tempo"] if sp else None,
        speech_ratio=sp.get("speech_ratio") if sp else None,
        stress_trend=stress_trend,
    )
    if cards:
        card_rows = [{"nm": c["label"], "val": c["text"], "min": 0, "max": 1, "value": 1,
                      "zones": [[1, C["ok"]]], "st": "NORMA", "desc": ""} for c in cards]
        channels.insert(0, {"title": "Итоги", "icon": "📝", "rows": card_rows})

    audio_only = bool(sp) and not pose_ok and not hr and not (emo and emo.get("face_ok"))
    return {"audio_only": audio_only, "elapsed": elapsed, "alerts": alerts,
            "channels": channels or [{"title": "Ожидание сигнала…", "icon": "•", "rows": []}]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="screen", help="screen | путь к видео")
    ap.add_argument("--zone", default=None, help="x,y,w,h (для screen)")
    ap.add_argument("--interval", type=float, default=1.5)
    ap.add_argument("--duration", type=float, default=0, help="0 = до Ctrl+C")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--top-frac", type=float, default=0.55,
                    help="доля высоты сверху, где искать участника (Granatum закрепляет сверху)")
    ap.add_argument("--exercise", "-e", default="", help="метка упражнения в имени файла истории")
    a = ap.parse_args()
    zone = tuple(int(v) for v in a.zone.split(",")) if a.zone else None

    stop = threading.Event()
    vstate = VisionState()
    astate = AudioState()
    threads = [threading.Thread(target=vision_thread,
                                args=(vstate, a.source, zone, stop, 10, a.top_frac), daemon=True)]
    if not a.no_audio:
        if a.source == "screen":
            threads.append(threading.Thread(target=mic_thread, args=(astate, stop), daemon=True))
        else:
            wav = extract_wav(a.source)
            if wav:
                threads.append(threading.Thread(target=wav_stream_thread, args=(astate, wav, stop), daemon=True))
    for t in threads:
        t.start()

    # --- автосейв истории на диск: построчно, реальное время → зависание теряет ≤1 интервал ---
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ex = f"_{a.exercise}" if a.exercise else ""
    try:
        cfg.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    csv_path = cfg.LOGS_DIR / f"combo_session_{stamp}{ex}.csv"
    csv_cols = ["ts_iso", "elapsed_s", "hr_bpm",
                "tempo_per_min", "pitch_hz", "pitch_std", "volume_db", "loud_iqr",
                "speech_ratio_pct", "pause_pct", "ei",
                "valence", "arousal", "dominant",
                "hand_to_face_pct", "fidget", "head_tilt_deg", "pose_ok", "face_ok",
                "resp_per_min", "perceived_trust", "perceived_dominance",
                "emo_switches_per_min", "emo_stability_pct",
                "e_happiness", "e_neutral", "e_surprise", "e_sadness",
                "e_anger", "e_fear", "e_disgust", "e_contempt",
                "stress_idx", "fatigue_idx", "engagement_idx"]
    csv_f = open(csv_path, "w", newline="", encoding="utf-8")
    csv_w = csv.writer(csv_f)
    csv_w.writerow(csv_cols)
    csv_f.flush()

    print(f"live-демон: source={a.source}, пишу {OUT_JSON}")
    print(f"автосейв истории (реальное время): {csv_path}")
    t0 = time.monotonic()
    try:
        while not stop.is_set():
            el = time.monotonic() - t0
            vstate.hr = vstate.hr_now()
            payload = build_payload(vstate, None if a.no_audio else astate,
                                    time.strftime("%H:%M:%S", time.gmtime(el)))
            tmp = OUT_JSON.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(OUT_JSON)

            # --- строка истории на диск (с реальным временем) ---
            sp = None if a.no_audio else (astate.snapshot() if astate else None)
            emo = None
            if EMO_STATE.exists():
                try:
                    emo = json.loads(EMO_STATE.read_text(encoding="utf-8"))
                except Exception:
                    emo = None
            with vstate.lock:
                pose_ok = vstate.pose_ok
                hand_rate = (sum(vstate.hand) / len(vstate.hand)) if vstate.hand else 0
                fidget = vstate.fidget
                tilt = vstate.head_tilt
            hr = vstate.hr_now()
            resp = vstate.resp_now()
            _emap = (emo.get("emotions") if emo else None) or {}
            _ni = compute_neiry(
                hr=hr, resp=resp,
                valence=emo.get("valence") if emo else None,
                arousal=emo.get("arousal") if emo else None,
                e_anger=_emap.get("Anger"), e_fear=_emap.get("Fear"),
                emo_stability=emo.get("emotional_stability_pct") if emo else None,
                tempo=sp["tempo"] if sp else None,
                pause_pct=sp.get("pause_pct") if sp else None,
                pitch_std=sp.get("pitch_std") if sp else None,
                loud_iqr=sp.get("loud_iqr") if sp else None,
                speech_ratio=sp.get("speech_ratio") if sp else None,
                fidget=fidget, head_tilt=tilt,
                face_present=emo.get("face_ok") if emo else None)
            csv_w.writerow([
                datetime.now().isoformat(timespec="seconds"), round(el, 1),
                int(hr) if hr else "",
                sp["tempo"] if sp else "", round(sp["pitch"]) if sp else "",
                sp["pitch_std"] if sp else "", sp["volume_db"] if sp else "",
                sp["loud_iqr"] if sp else "",
                round(sp["speech_ratio"] * 100) if sp else "", sp["pause_pct"] if sp else "",
                sp["ei"] if sp else "",
                round(emo.get("valence", 0), 3) if emo else "",
                round(emo.get("arousal", 0), 3) if emo else "",
                emo.get("dominant", "") if emo else "",
                round(hand_rate * 100) if pose_ok else "", round(fidget, 4) if pose_ok else "",
                round(tilt, 1) if pose_ok else "", int(pose_ok),
                int(bool(emo and emo.get("face_ok"))),
                int(resp) if resp else "",
                emo.get("perceived_trust", "") if emo else "",
                emo.get("perceived_dominance", "") if emo else "",
                emo.get("switches_per_min", "") if emo else "",
                emo.get("emotional_stability_pct", "") if emo else "",
                *[round(_emap.get(k, 0), 3) if _emap else "" for k in
                  ("Happiness", "Neutral", "Surprise", "Sadness", "Anger", "Fear", "Disgust", "Contempt")],
                _ni["stress"] if _ni["stress"] is not None else "",
                _ni["fatigue"] if _ni["fatigue"] is not None else "",
                _ni.get("engagement") if _ni.get("engagement") is not None else "",
            ])
            csv_f.flush()

            if a.duration and el >= a.duration:
                break
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        try:
            csv_f.close()
        except Exception:
            pass
        time.sleep(0.3)
        print(f"live-демон остановлен. История сессии: {csv_path}")


if __name__ == "__main__":
    main()
