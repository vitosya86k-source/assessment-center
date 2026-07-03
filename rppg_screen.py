"""
rPPG-инструмент: живой пульс/дыхание/HRV-тренд по лицу из экрана.

Зачем: во время ассессмента в браузере (Гранатум) видеть пульс участника в моменте.
Не трогает браузер — только читает пиксели экрана (mss), работе не мешает.

Запуск:
    cd "<.../NEW VERSION>"
    ./start_ac_measurement.sh
    # или: venv_new/bin/python rppg_screen.py --exercise чай

Логика выбора лица:
- При старте берётся самое крупное лицо на экране.
- Дальше держится ИМЕННО этот участник (фиксируется по центру и размеру),
  даже если в кадре есть другие лица или Haar на мгновение его теряет.
- Нажми 'n' — переключиться на следующего участника. Буферы сбрасываются.

Клавиши (фокус на окне HRV):
    n — следующий участник
    b — зафиксировать baseline покоя (нажми после ~60 сек спокойного сидения)
    q — выход

Честность: пульс при спокойно сидящем человеке надёжен (~2-5 уд/мин).
HR > 130 в покое — почти наверняка артефакт видеосжатия, не сердце.
HRV/дыхание — ОРИЕНТИРОВОЧНЫЙ тренд по видео, не диагноз датчика.
"""

import argparse
import os
import select
import signal
import sys
import termios
import tty
import time
import csv
import subprocess
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.signal import butter, filtfilt, find_peaks, welch

import cv2
import mss
import sounddevice as sd
from PIL import Image, ImageDraw, ImageFont

# --- параметры ---
TARGET_FPS = 15
BUFFER_SECONDS = 18
HR_MIN_HZ, HR_MAX_HZ = 0.7, 3.5
RESP_MIN_HZ, RESP_MAX_HZ = 0.1, 0.5
DETECT_WIDTH = 800
DETECT_EVERY = 4
HR_SNR_MIN = 3.0          # ниже — не показывать/не логировать пульс (шум)
PARTICIPANT_TOP_FRAC = 0.52
GRANATUM_TOP_TILE_FRAC = 0.52   # верхняя плитка в сетке видео
DATA_DIR = Path(__file__).parent / "data"
AC_AUDIO_DIR = Path.home() / "Dropbox" / "AC_Audio"
_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"

# --- аудио ---
AUDIO_SR = 16000
AUDIO_BLOCK = 1024              # ~64 мс
AUDIO_ENV_KEEP_SEC = 60
PITCH_MIN_HZ, PITCH_MAX_HZ = 70, 350

_FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_CACHE = {}


def _font(size, bold=False):
    key = (size, bold)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = ImageFont.truetype(_FONT_BLD if bold else _FONT_REG, size)
    return _FONT_CACHE[key]


# --- цвета (RGB для PIL) ---
GREEN = (0, 220, 0)
YELLOW = (235, 220, 70)
ORANGE = (255, 150, 0)
RED = (255, 70, 70)
GREY = (170, 170, 170)
DGREY = (60, 60, 60)
WHITE = (240, 240, 240)


# ================= обработка сигнала =================

def bandpass(sig, fps, lo, hi):
    nyq = fps / 2.0
    lo_n, hi_n = max(lo / nyq, 1e-3), min(hi / nyq, 0.99)
    if hi_n <= lo_n:
        return sig
    b, a = butter(3, [lo_n, hi_n], btype="band")
    return filtfilt(b, a, sig)


def pos_signal(rgb, fps):
    """POS (Wang 2017): пульсовая волна из RGB-ряда (N,3)."""
    eps = 1e-9
    n = rgb.shape[0]
    H = np.zeros(n)
    win = max(int(1.6 * fps), 8)
    proj = np.array([[0.0, 1.0, -1.0], [-2.0, 1.0, 1.0]])
    for s in range(0, n - win):
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


def rmssd_trend(pulse, fps):
    if len(pulse) < fps * 6:
        return None
    peaks, _ = find_peaks(pulse, distance=int(fps * 0.4))
    if len(peaks) < 4:
        return None
    rr = np.diff(peaks) / fps * 1000.0
    rr = rr[(rr > 350) & (rr < 1500)]
    if len(rr) < 3:
        return None
    return float(np.sqrt(np.mean(np.diff(rr) ** 2)))


# ================= аудио =================

def find_pulse_monitor_source():
    """Имя PulseAudio/PipeWire monitor-источника default-sink (это поток, идущий
    в наушники = голос участника). Возвращает None если PulseAudio недоступен."""
    try:
        sink = subprocess.check_output(["pactl", "get-default-sink"],
                                       text=True, timeout=1).strip()
        if not sink:
            return None
        monitor = sink + ".monitor"
        out = subprocess.check_output(["pactl", "list", "short", "sources"],
                                      text=True, timeout=1)
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1] == monitor:
                return monitor
    except Exception:
        return None
    return None


def estimate_pitch(x, sr, fmin=PITCH_MIN_HZ, fmax=PITCH_MAX_HZ):
    """F0 голоса через автокорреляцию. None если речь не уверенно периодична."""
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


class AudioMonitor:
    """Считывает аудио в отдельном потоке: RMS, голос/тишина, питч, доля речи."""

    def __init__(self):
        self.sr = AUDIO_SR
        self.block = AUDIO_BLOCK
        self.lock = threading.Lock()
        env_maxlen = int(AUDIO_ENV_KEEP_SEC * self.sr / self.block)
        self.rms_env = deque(maxlen=env_maxlen)
        self.voiced_hist = deque(maxlen=env_maxlen)
        self.pitch_hist = deque(maxlen=env_maxlen)
        self.voiced_buf = np.zeros(0, dtype=np.float32)  # аудио активных блоков для питча
        self.noise_floor = 0.002
        self.last_rms = 0.0
        self.last_voiced = False
        self.last_pitch = None
        self.last_pitch_t = 0.0
        self.stream = None
        self.source_label = "—"
        self.error = None

    def _callback(self, indata, frames, time_info, status):
        x = indata[:, 0] if indata.ndim > 1 else indata
        x = np.asarray(x, dtype=np.float32)
        rms = float(np.sqrt(np.mean(x * x)) + 1e-12)
        # Адаптивный шумовой порог: только в тишине обновляем floor.
        thr = max(self.noise_floor * 3.5, 0.004)
        voiced = rms > thr
        if not voiced:
            self.noise_floor = 0.95 * self.noise_floor + 0.05 * rms
        # Накапливаем активные кадры для питча
        if voiced:
            self.voiced_buf = np.concatenate([self.voiced_buf, x])
            # ограничим ~0.7 сек
            keep = int(self.sr * 0.7)
            if len(self.voiced_buf) > keep:
                self.voiced_buf = self.voiced_buf[-keep:]
        # Считаем питч не чаще чем раз в 0.25 сек
        pitch = None
        now = time.time()
        if voiced and len(self.voiced_buf) >= int(self.sr * 0.3) and now - self.last_pitch_t > 0.25:
            pitch = estimate_pitch(self.voiced_buf, self.sr)
            self.last_pitch_t = now
        with self.lock:
            self.last_rms = rms
            self.last_voiced = voiced
            if pitch is not None:
                self.last_pitch = pitch
                self.pitch_hist.append(pitch)
            self.rms_env.append(rms)
            self.voiced_hist.append(1 if voiced else 0)

    def start(self):
        # Попытка: PulseAudio monitor (звук, идущий в динамики = голос собеседника).
        monitor = find_pulse_monitor_source()
        try:
            if monitor:
                os.environ["PULSE_SOURCE"] = monitor
                self.source_label = "PulseAudio monitor (звук собеседника)"
                device = "pulse"
            else:
                self.source_label = "микрофон по умолчанию"
                device = None
            self.stream = sd.InputStream(
                samplerate=self.sr,
                blocksize=self.block,
                channels=1,
                dtype="float32",
                device=device,
                callback=self._callback,
            )
            self.stream.start()
        except Exception as e:
            # Fallback: пробуем дефолтный мик без PULSE_SOURCE
            self.error = f"{type(e).__name__}: {e}"
            try:
                os.environ.pop("PULSE_SOURCE", None)
                self.stream = sd.InputStream(
                    samplerate=self.sr, blocksize=self.block,
                    channels=1, dtype="float32", callback=self._callback,
                )
                self.stream.start()
                self.source_label = "микрофон (fallback)"
                self.error = None
            except Exception as e2:
                self.error = f"{type(e).__name__}: {e}; fallback: {type(e2).__name__}: {e2}"
                self.stream = None

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def snapshot(self):
        with self.lock:
            rms = self.last_rms
            voiced = self.last_voiced
            pitch = self.last_pitch
            speech_ratio = float(np.mean(self.voiced_hist)) if self.voiced_hist else 0.0
            pitch_arr = list(self.pitch_hist)
            v_hist = list(self.voiced_hist)
        rms_db = 20.0 * np.log10(rms + 1e-9)
        pitch_med = float(np.median(pitch_arr[-40:])) if len(pitch_arr) >= 4 else (pitch if pitch else None)
        # Метрики из voiced_hist
        tempo = pauses_per_min = avg_pause_sec = None
        if len(v_hist) > 50:
            v = np.array(v_hist, dtype=np.int8)
            risings = int(np.sum((v[1:] == 1) & (v[:-1] == 0)))
            secs = len(v) * self.block / self.sr
            if secs > 5:
                tempo = risings * 60.0 / secs
            # Паузы: подсчёт непрерывных серий нулей длиной > 1 сек
            frame_sec = self.block / self.sr
            min_pause_frames = int(1.0 / frame_sec)
            run_len = 0
            pause_lens = []
            for k in range(len(v)):
                if v[k] == 0:
                    run_len += 1
                else:
                    if run_len >= min_pause_frames:
                        pause_lens.append(run_len * frame_sec)
                    run_len = 0
            # хвостовой run не считаем (возможно ещё продолжается)
            if secs > 5:
                pauses_per_min = len(pause_lens) * 60.0 / secs
                if pause_lens:
                    avg_pause_sec = float(np.mean(pause_lens))
        return {
            "rms_db": rms_db,
            "voiced": voiced,
            "pitch_hz": pitch,
            "pitch_med": pitch_med,
            "speech_ratio": speech_ratio,
            "tempo_per_min": tempo,
            "pauses_per_min": pauses_per_min,
            "avg_pause_sec": avg_pause_sec,
            "source": self.source_label,
            "error": self.error,
        }


class AudioFileRecorder:
    """Пишет полный звук сессии в AC_Audio (m4a через ffmpeg + pulse monitor)."""

    def __init__(self, out_path: Path):
        self.out_path = out_path
        self.proc = None
        self.error = None

    def start(self):
        monitor = find_pulse_monitor_source()
        if not monitor:
            self.error = "PulseAudio monitor не найден"
            return
        AC_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "pulse", "-i", monitor,
            "-ac", "1", "-ar", "44100", "-c:a", "aac", "-b:a", "96k",
            str(self.out_path),
        ]
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self.proc = None

    def stop(self):
        if self.proc is None:
            return
        try:
            self.proc.send_signal(signal.SIGINT)
            self.proc.wait(timeout=8)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None


def session_paths(exercise: str | None = None):
    """Имена файлов сессии: YYYYMMDD_HHMMSS[_exercise]."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{exercise}" if exercise else ""
    base = f"{stamp}{suffix}"
    log_path = DATA_DIR / f"{base}.csv"
    pause_path = DATA_DIR / f"{base}_pauses.csv"
    audio_path = AC_AUDIO_DIR / f"{base}.m4a"
    return stamp, log_path, pause_path, audio_path


# ================= детекция и трекинг лица =================

def detect_faces(frame_bgr, cascade, scale):
    """Список всех лиц в координатах полного кадра, отсортирован по размеру убыв."""
    small = cv2.resize(frame_bgr, None, fx=scale, fy=scale)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=7, minSize=(60, 60))
    if len(faces) == 0:
        return []
    boxes = [(int(fx / scale), int(fy / scale), int(fw / scale), int(fh / scale))
             for (fx, fy, fw, fh) in faces]
    boxes.sort(key=lambda b: b[2] * b[3], reverse=True)
    return boxes


def detect_granatum_video_origin(frame_bgr):
    """Левый/верхний край области видео (минуя сайдбар и пустую шапку Granatum)."""
    H, W = frame_bgr.shape[:2]
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    left = 0
    for x in range(min(140, W // 3)):
        col = gray[int(H * 0.15):int(H * 0.85), x]
        if len(col) and col.mean() > 32 and col.std() > 8:
            left = max(0, x - 1)
            break
    top = 0
    x0 = left + max(20, (W - left) // 10)
    x1 = left + (W - left) * 9 // 10
    for y in range(min(180, H // 3)):
        row = gray[y, x0:x1]
        if len(row) and row.mean() > 28 and row.std() > 12:
            top = max(0, y - 1)
            break
    return left, top


def auto_participant_zone(frame_shape, frame_bgr=None):
    """Участник — верхняя плитка в области видео Granatum (без сайдбара слева)."""
    H, W = frame_shape[:2]
    if frame_bgr is not None:
        left, top = detect_granatum_video_origin(frame_bgr)
        cw, ch = W - left, H - top
        if cw > 200 and ch > 150:
            zh = max(80, int(ch * GRANATUM_TOP_TILE_FRAC))
            return (left, top, cw, zh)
    return (0, 0, W, max(80, int(H * PARTICIPANT_TOP_FRAC)))


def pick_participant_face(boxes, frame_h, top_frac=0.50):
    """Крупнейшее лицо только в верхней части кадра (участник, не асессор снизу)."""
    if not boxes:
        return None
    y_max = frame_h * top_frac
    top = [b for b in boxes if (b[1] + b[3] / 2.0) < y_max]
    if not top:
        # fallback: самое верхнее лицо
        return min(boxes, key=lambda b: b[1] + b[3] / 2.0)
    top.sort(key=lambda b: b[2] * b[3], reverse=True)
    return top[0]


def find_tracked_face(boxes, t_center, t_size):
    """Ищет среди boxes лицо, ближайшее к t_center с похожим размером.
    Возвращает None если ничего подходящего рядом нет — лучше держать старый last_box,
    чем перепрыгнуть на соседа."""
    if not boxes or t_center is None:
        return None
    tx, ty = t_center
    best, best_d = None, None
    for b in boxes:
        x, y, w, h = b
        cx, cy = x + w / 2.0, y + h / 2.0
        size = (w + h) / 2.0
        if t_size and (size < 0.55 * t_size or size > 1.8 * t_size):
            continue
        d = ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5
        max_d = (t_size or size) * 0.8
        if d > max_d:
            continue
        if best is None or d < best_d:
            best, best_d = b, d
    return best


def forehead_roi(box, frame_shape):
    if box is None:
        return None
    x, y, w, h = box
    H, W = frame_shape[:2]
    x_lo, x_hi = x + int(0.30 * w), x + int(0.70 * w)
    y_lo, y_hi = y + int(0.08 * h), y + int(0.28 * h)
    x_lo, x_hi = max(x_lo, 0), min(x_hi, W)
    y_lo, y_hi = max(y_lo, 0), min(y_hi, H)
    if x_hi - x_lo < 6 or y_hi - y_lo < 6:
        return None
    return (x_lo, y_lo, x_hi, y_hi)


def zone_forehead_roi(zone):
    """ROI лба в верхней плитке: голова по центру, лоб ниже края плитки."""
    zx, zy, zw, zh = zone
    return (zx + int(0.22 * zw), zy + int(0.14 * zh),
            zx + int(0.78 * zw), zy + int(0.42 * zh))


def zone_is_dark(frame_bgr, zone, thresh=28.0):
    """Чёрная плитка = камера выключена."""
    zx, zy, zw, zh = zone
    H, W = frame_bgr.shape[:2]
    x1, y1 = max(zx, 0), max(zy, 0)
    x2, y2 = min(zx + zw, W), min(zy + zh, H)
    if x2 - x1 < 10 or y2 - y1 < 10:
        return True
    patch = frame_bgr[y1:y2, x1:x2]
    return float(patch.mean()) < thresh


def forehead_mean_rgb(frame_bgr, roi):
    if roi is None:
        return None
    x_lo, y_lo, x_hi, y_hi = roi
    patch = frame_bgr[y_lo:y_hi, x_lo:x_hi]
    mean_bgr = patch.reshape(-1, 3).mean(axis=0)
    return np.array([mean_bgr[2], mean_bgr[1], mean_bgr[0]])


# ================= интерпретации =================

def interp_hr(hr):
    if hr is None:
        return "...", GREY
    if hr > 130:
        return "АРТЕФАКТ? помехи видео", RED
    if hr > 100:
        return "ВЫСОКИЙ (стресс/нагрузка)", ORANGE
    if hr >= 85:
        return "слегка повышен", YELLOW
    if 60 <= hr < 85:
        return "норма покоя", GREEN
    if 50 <= hr < 60:
        return "глубокий покой", GREEN
    return "НИЗКИЙ", ORANGE


def interp_resp(r):
    if r is None:
        return "...", GREY
    if r > 22:
        return "часто (волнение)", ORANGE
    if r < 10:
        return "редко", ORANGE
    return "норма 12-20", GREEN


def interp_signal(snr):
    if snr is None or snr < 2:
        return "шумно — нужно света/ближе", ORANGE
    if snr < 4:
        return "средне", YELLOW
    return "чисто", GREEN


def interp_rmssd(r):
    if r is None:
        return "...", GREY
    return "только тренд по видео", GREY


def interp_pitch(pitch, baseline_pitch):
    if pitch is None:
        return "тишина / нет голоса", GREY
    if baseline_pitch:
        diff = pitch - baseline_pitch
        rel = diff / baseline_pitch
        if rel > 0.15:
            return f"выше базы +{diff:.0f} Гц (волнение?)", ORANGE
        if rel < -0.10:
            return f"ниже базы {diff:.0f} Гц (спад)", YELLOW
        return f"≈ база {baseline_pitch:.0f} Гц", GREEN
    typ = "муж. диапазон" if pitch < 165 else ("жен. диапазон" if pitch > 175 else "промежуточный")
    return typ, WHITE


def interp_speech_ratio(r):
    if r is None:
        return "...", GREY
    pct = r * 100
    if pct < 5:
        return "молчит", GREY
    if pct < 25:
        return "говорит мало", YELLOW
    if pct < 65:
        return "активный разговор", GREEN
    return "доминирует в речи", ORANGE


def interp_tempo(tempo):
    """Темп речи (всплески/мин ≈ слогов/мин). По схеме assessment_profiler_bot:
    <100 — медленная (обдумывание), 100-150 — норма, 150-200 — быстрая (волнение),
    >200 — стресс."""
    if tempo is None:
        return "...", GREY
    if tempo < 100:
        return "медленная (обдумывает)", YELLOW
    if tempo < 150:
        return "норма", GREEN
    if tempo < 200:
        return "быстрая (энергия/волнение)", ORANGE
    return "очень быстрая (стресс)", RED


def interp_pauses(pp):
    if pp is None:
        return "...", GREY
    if pp < 2:
        return "уверенная речь", GREEN
    if pp < 5:
        return "обдумывает", YELLOW
    return "затрудняется", ORANGE


# ================= рендер UI =================

# ===== кликабельные кнопки внизу панели (мышь работает в окне HRV) =====
PANEL_W, PANEL_H = 560, 980
_BTN_Y1, _BTN_Y2 = 926, 972
# клик мыши кладёт сюда код клавиши; главный цикл его забирает
_PENDING_KEY = {"k": None}


def panel_buttons(paused=False):
    """[(label, key_ord, x1, y1, x2, y2)] — общий источник для отрисовки и хит-теста."""
    labels = [
        ("ПРОДОЛЖИТЬ" if paused else "ПАУЗА", ord("p")),
        ("ПЛИТКА", ord("s")),
        ("ПОКОЙ", ord("b")),
        ("СТОП", ord("q")),
    ]
    m, g = 14, 8
    n = len(labels)
    bw = (PANEL_W - 2 * m - (n - 1) * g) // n
    btns, x = [], m
    for lbl, key in labels:
        btns.append((lbl, key, x, _BTN_Y1, x + bw, _BTN_Y2))
        x += bw + g
    return btns


def on_mouse_panel(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:
        for (_lbl, key, x1, y1, x2, y2) in panel_buttons():
            if x1 <= x <= x2 and y1 <= y <= y2:
                _PENDING_KEY["k"] = key
                break


def draw_buttons(d, paused=False):
    for (lbl, key, x1, y1, x2, y2) in panel_buttons(paused):
        if key == ord("q"):
            bg, brd, fg = (60, 20, 20), (180, 70, 70), (255, 200, 200)
        elif key == ord("p") and paused:
            bg, brd, fg = (20, 70, 20), (90, 200, 90), (200, 255, 200)
        else:
            bg, brd, fg = (45, 45, 45), (120, 120, 120), (235, 235, 235)
        d.rectangle((x1, y1, x2, y2), fill=bg, outline=brd, width=2)
        f = _font(18, bold=True)
        try:
            tw = d.textlength(lbl, font=f)
        except Exception:
            tw = len(lbl) * 9
        d.text((x1 + (x2 - x1 - tw) / 2, y1 + (y2 - y1) / 2 - 12), lbl, font=f, fill=fg)


def render_dashboard(face_preview_bgr, hr, snr, rmssd, resp, baseline, buf_sec,
                     face_idx, faces_total, hr_history, log_path, log_rows,
                     tracking_lost, audio, zone, video_ok=True,
                     camera_off=False, zone_only=False, paused=False):
    W, Hh = PANEL_W, PANEL_H
    img = Image.new("RGB", (W, Hh), (0, 0, 0))
    d = ImageDraw.Draw(img)

    # шапка
    if camera_off and zone is not None:
        hdr = "камера выкл — только голос"
        hdr_col = ORANGE
    elif zone is not None:
        hdr = "участник: верх" + (" — ROI лба" if zone_only else "")
        hdr_col = GREEN
    elif faces_total > 0:
        hdr = f"лицо {face_idx + 1} из {faces_total}"
        hdr_col = GREY if tracking_lost else WHITE
    else:
        hdr = "лицо не найдено"
        hdr_col = ORANGE
    d.text((14, 10), hdr, font=_font(24, bold=True), fill=hdr_col)
    if not video_ok:
        d.text((14, 44), "видео: окно свернуто — только звук",
               font=_font(16, bold=True), fill=ORANGE)
    elif tracking_lost:
        d.text((14, 44), "(участник вышел из кадра)",
               font=_font(16), fill=ORANGE)
    else:
        d.text((14, 44),
               "N — другое лицо  ◀▶  S — выбрать мышью",
               font=_font(17, bold=True), fill=YELLOW)
        d.text((14, 70),
               "C — снять плитку    B — покой    Q — выход",
               font=_font(14), fill=GREY)

    # превью лица (узкое сверху)
    pv_x, pv_y, pv_w, pv_h = 14, 100, 170, 130
    d.rectangle((pv_x, pv_y, pv_x + pv_w, pv_y + pv_h), outline=DGREY, width=2)
    if face_preview_bgr is not None:
        ph, pw = face_preview_bgr.shape[:2]
        if ph > 0 and pw > 0:
            scale = min(pv_w / pw, pv_h / ph)
            nw, nh = max(1, int(pw * scale)), max(1, int(ph * scale))
            face_rgb = cv2.cvtColor(face_preview_bgr, cv2.COLOR_BGR2RGB)
            face_resized = cv2.resize(face_rgb, (nw, nh))
            face_im = Image.fromarray(face_resized)
            ox = pv_x + (pv_w - nw) // 2
            oy = pv_y + (pv_h - nh) // 2
            img.paste(face_im, (ox, oy))
            rx1 = ox + int(0.22 * nw)
            rx2 = ox + int(0.78 * nw)
            ry1 = oy + int(0.14 * nh)
            ry2 = oy + int(0.42 * nh)
            d.rectangle((rx1, ry1, rx2, ry2), outline=YELLOW, width=2)
    else:
        if zone_only:
            d.text((pv_x + 8, pv_y + pv_h // 2 - 8),
                   "ROI лба", font=_font(14), fill=YELLOW)
        else:
            d.text((pv_x + 8, pv_y + pv_h // 2 - 8),
                   "жду лицо", font=_font(14), fill=GREY)

    snr_ok = snr is not None and snr >= HR_SNR_MIN
    hr_show = hr if video_ok and not camera_off and snr_ok else None
    mx = 200
    d.text((mx, 100), "ПУЛЬС", font=_font(15, bold=True), fill=GREY)
    if hr_show:
        hr_int, hr_col = interp_hr(hr_show)
        d.text((mx, 120), f"{hr_show:.0f}", font=_font(54, bold=True), fill=hr_col)
        d.text((mx + 140, 152), "уд/мин", font=_font(18, bold=True), fill=hr_col)
        d.text((mx, 188), hr_int, font=_font(15, bold=True), fill=hr_col)
    elif camera_off:
        d.text((mx, 128), "камера выкл", font=_font(20), fill=ORANGE)
    elif not video_ok:
        d.text((mx, 128), "видео недоступно", font=_font(18), fill=ORANGE)
    elif hr and not snr_ok:
        d.text((mx, 128), f"копим {buf_sec:.0f}с (SNR {snr:.1f})",
               font=_font(16), fill=YELLOW)
    else:
        d.text((mx, 128), f"копим {buf_sec:.0f}с / 8с",
               font=_font(20), fill=GREY)

    if baseline and hr_show:
        delta = hr - baseline
        d_col = GREEN if abs(delta) < 6 else (ORANGE if delta > 0 else YELLOW)
        d.text((430, 110), "vs покоя", font=_font(14), fill=GREY)
        d.text((430, 130), f"{delta:+.0f}",
               font=_font(34, bold=True), fill=d_col)

    # ДЫХАНИЕ
    d.text((14, 244), "ДЫХАНИЕ", font=_font(15, bold=True), fill=GREY)
    if resp:
        r_int, r_col = interp_resp(resp)
        d.text((110, 240), f"{resp:.0f}/мин", font=_font(28, bold=True), fill=r_col)
        d.text((280, 246), r_int, font=_font(15, bold=True), fill=r_col)
    else:
        d.text((110, 240), "...", font=_font(28), fill=GREY)

    # сигнал + RMSSD одной строкой
    d.text((14, 282), "сигнал", font=_font(14, bold=True), fill=GREY)
    s_int, s_col = interp_signal(snr)
    d.text((100, 280), f"SNR {snr:.1f}" if snr else "—",
           font=_font(17, bold=True), fill=s_col)
    d.text((200, 282), s_int, font=_font(14, bold=True), fill=s_col)

    d.text((14, 308), "RMSSD", font=_font(14, bold=True), fill=GREY)
    rm_int, rm_col = interp_rmssd(rmssd)
    d.text((100, 306), f"{rmssd:.0f} мс" if rmssd else "...",
           font=_font(17, bold=True), fill=WHITE if rmssd else GREY)
    d.text((200, 308), rm_int, font=_font(14), fill=rm_col)

    # ===== блок ГОЛОС =====
    ay = 344
    d.text((14, ay), "ГОЛОС / ЗВУК", font=_font(20, bold=True), fill=WHITE)
    if audio is None:
        d.text((14, ay + 28), "(аудио не запущено)", font=_font(15), fill=GREY)
    elif audio.get("error"):
        d.text((14, ay + 28), f"ошибка: {audio['error'][:50]}",
               font=_font(13), fill=ORANGE)
    else:
        rms_db = audio["rms_db"]
        voiced = audio["voiced"]
        pitch_med = audio["pitch_med"]
        sr_ratio = audio["speech_ratio"]
        tempo = audio["tempo_per_min"]
        pp = audio.get("pauses_per_min")
        avp = audio.get("avg_pause_sec")

        # VU-meter -60..0 dB
        vx, vy, vw, vh = 14, ay + 30, 360, 18
        d.rectangle((vx, vy, vx + vw, vy + vh), outline=DGREY, width=1)
        lvl = max(0.0, min(1.0, (rms_db + 60) / 60.0))
        bar_col = GREEN if voiced else GREY
        d.rectangle((vx + 2, vy + 2, vx + 2 + int((vw - 4) * lvl), vy + vh - 2), fill=bar_col)
        d.text((vx + vw + 10, vy - 4), f"{rms_db:5.1f} dB",
               font=_font(15, bold=True), fill=bar_col)
        d.text((vx + vw + 10, vy + 18),
               "ГОВОРИТ" if voiced else "тишина",
               font=_font(14, bold=True), fill=GREEN if voiced else GREY)

        ty = ay + 60
        row_h = 32

        d.text((14, ty + 2), "питч F0", font=_font(15), fill=GREY)
        if pitch_med:
            p_int, p_col = interp_pitch(pitch_med, None)
            d.text((130, ty - 2), f"{pitch_med:.0f} Гц",
                   font=_font(23, bold=True), fill=p_col)
            d.text((260, ty + 2), p_int, font=_font(15, bold=True), fill=p_col)
        else:
            d.text((130, ty - 2), "—", font=_font(23), fill=GREY)
            d.text((260, ty + 2), "нужна речь", font=_font(15), fill=GREY)
        ty += row_h

        d.text((14, ty + 2), "доля речи", font=_font(15), fill=GREY)
        sr_int, sr_col = interp_speech_ratio(sr_ratio)
        d.text((130, ty - 2), f"{sr_ratio * 100:.0f}%",
               font=_font(23, bold=True), fill=sr_col)
        d.text((260, ty + 2), sr_int, font=_font(15, bold=True), fill=sr_col)
        ty += row_h

        d.text((14, ty + 2), "темп", font=_font(15), fill=GREY)
        t_int, t_col = interp_tempo(tempo)
        d.text((130, ty - 2), f"{tempo:.0f}/мин" if tempo else "—",
               font=_font(23, bold=True), fill=t_col)
        d.text((260, ty + 2), t_int, font=_font(15, bold=True), fill=t_col)
        ty += row_h

        d.text((14, ty + 2), "паузы", font=_font(15), fill=GREY)
        pp_int, pp_col = interp_pauses(pp)
        pp_txt = f"{pp:.1f}/мин" if pp is not None else "—"
        d.text((130, ty - 2), pp_txt, font=_font(23, bold=True), fill=pp_col)
        d.text((260, ty + 2), pp_int, font=_font(15, bold=True), fill=pp_col)
        if avp:
            d.text((430, ty + 4), f"средн {avp:.1f}с",
                   font=_font(13), fill=GREY)

    # график пульса
    gx, gy, gw, gh = 14, 620, 510, 130
    d.text((gx, gy - 22), "график пульса (~60с)",
           font=_font(15, bold=True), fill=GREY)
    d.rectangle((gx, gy, gx + gw, gy + gh), outline=DGREY, width=1)
    arr = [v for v in hr_history if v is not None and 40 < v < 180]
    if len(arr) >= 2:
        lo = max(40, min(arr) - 5)
        hi = min(180, max(arr) + 5)
        if hi - lo < 5:
            lo, hi = max(40, lo - 5), min(180, hi + 5)
        d.text((gx + gw + 6, gy - 6), f"{hi:.0f}", font=_font(13, bold=True), fill=GREY)
        d.text((gx + gw + 6, gy + gh - 14), f"{lo:.0f}", font=_font(13, bold=True), fill=GREY)
        n = len(arr)
        pts = []
        for i, v in enumerate(arr):
            xv = gx + int(i * gw / max(1, n - 1))
            yv = gy + gh - int((v - lo) / (hi - lo) * gh)
            pts.append((xv, yv))
        if len(pts) >= 2:
            d.line(pts, fill=GREEN, width=2)
        if baseline and lo < baseline < hi:
            by = gy + gh - int((baseline - lo) / (hi - lo) * gh)
            d.line([(gx, by), (gx + gw, by)], fill=(120, 120, 50), width=1)
            d.text((gx + 4, by - 16), f"покой {baseline:.0f}",
                   font=_font(12, bold=True), fill=(220, 220, 100))

    # лог-статус
    log_col = GREEN if log_rows > 0 else GREY
    d.text((14, 762), f"лог: {log_path.name}   строк: {log_rows}",
           font=_font(14, bold=True), fill=log_col)

    # шпаргалка по нормам (компактно)
    d.text((14, 790), "Нормы покоя: пульс 60-80  |  дых. 12-20  |  питч муж 85-180, жен 165-255",
           font=_font(12), fill=GREY)
    d.text((14, 810),
           "Темп: <100 обдумывает, 100-150 норма, 150-200 быстр., >200 стресс",
           font=_font(12), fill=GREY)
    d.text((14, 830),
           "Паузы >1с: <2 уверен., 2-5 обдумывает, >5 затруднения",
           font=_font(12), fill=GREY)
    d.text((14, 850), "Пульс >130 или скачет = видеопомехи. RMSSD — только тренд",
           font=_font(12), fill=GREY)
    d.text((14, 880),
           "ВАЖНО: окно справа, левая часть экрана = Гранатум для замера",
           font=_font(13, bold=True), fill=YELLOW)

    # кнопки мышью
    draw_buttons(d, paused)

    # баннер паузы поверх верхней части
    if paused:
        d.rectangle((0, 0, PANEL_W, 92), fill=(70, 55, 0), outline=YELLOW, width=3)
        d.text((14, 24), "|| ПАУЗА — замер остановлен",
               font=_font(28, bold=True), fill=YELLOW)
        d.text((14, 62), "звук m4a пишется. Нажми ПРОДОЛЖИТЬ",
               font=_font(16), fill=(220, 220, 120))

    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


# ================= захват окна браузера (X11) =================

BROWSER_TITLE_HINTS = ("granat", "гранат", "granatum", "meet", "jitsi", "conference", "ассесс", "solutions")
BROWSER_SKIP_TITLE = ("clipboard", "devtools", "developer tools")


def find_browser_window_monitor():
    """Ищет видимое окно Гранатума/браузера. None если свернуто или не найдено."""
    try:
        tree = subprocess.check_output(["xwininfo", "-root", "-tree"], text=True, timeout=2)
    except Exception:
        return None, None
    candidates = []
    chrome_fallback = []
    for line in tree.splitlines():
        if '"' not in line or not line.strip().startswith("0x"):
            continue
        title = line.split('"')[1]
        low = title.lower()
        if any(s in low for s in BROWSER_SKIP_TITLE):
            continue
        is_chrome = "google-chrome" in low or "chromium" in line.lower()
        if not any(h in low for h in BROWSER_TITLE_HINTS) and not is_chrome:
            continue
        wid = line.split()[0]
        try:
            info = subprocess.check_output(["xwininfo", "-id", wid], text=True, timeout=1)
        except Exception:
            continue
        if "Map State: IsUnMapped" in info or "IsUnMapped" in info:
            continue
        geom = {}
        for il in info.splitlines():
            il = il.strip()
            if il.startswith("Absolute upper-left X:"):
                geom["left"] = int(il.split(":")[1])
            elif il.startswith("Absolute upper-left Y:"):
                geom["top"] = int(il.split(":")[1])
            elif il.startswith("Width:"):
                geom["width"] = int(il.split(":")[1])
            elif il.startswith("Height:"):
                geom["height"] = int(il.split(":")[1])
        if geom.get("width", 0) < 200 or geom.get("height", 0) < 200:
            continue
        entry = (title, geom)
        if any(h in low for h in BROWSER_TITLE_HINTS):
            candidates.append(entry)
        elif is_chrome and geom.get("width", 0) > 600:
            chrome_fallback.append(entry)
    if not candidates:
        candidates = chrome_fallback
    if not candidates:
        return None, None
    for title, geom in candidates:
        if "granat" in title.lower() or "гранат" in title.lower():
            mon = {"top": geom["top"], "left": geom["left"],
                   "width": geom["width"], "height": geom["height"]}
            return mon, title
    title, geom = max(candidates, key=lambda x: x[1]["width"] * x[1]["height"])
    mon = {"top": geom["top"], "left": geom["left"],
           "width": geom["width"], "height": geom["height"]}
    return mon, title


# ================= main =================

def reset_buffers(*buffers):
    for b in buffers:
        b.clear()


def select_zone_with_mouse(frame_bgr):
    """Открыть окно с уменьшенной копией экрана, дать пользователю мышью обвести плитку.
    Возвращает (x, y, w, h) в координатах оригинального полного экрана, либо None."""
    H, W = frame_bgr.shape[:2]
    # Уменьшаем для удобства выделения
    max_w = 1400
    if W > max_w:
        scale = max_w / W
        preview = cv2.resize(frame_bgr, (int(W * scale), int(H * scale)))
    else:
        scale = 1.0
        preview = frame_bgr.copy()
    title = "Обведите мышью плитку участника, затем Enter (Esc отмена)"
    cv2.namedWindow(title, cv2.WINDOW_AUTOSIZE)
    cv2.setWindowProperty(title, cv2.WND_PROP_TOPMOST, 1)
    try:
        r = cv2.selectROI(title, preview, showCrosshair=False, fromCenter=False)
    except Exception:
        r = (0, 0, 0, 0)
    cv2.destroyWindow(title)
    x, y, w, h = r
    if w < 20 or h < 20:
        return None
    return (int(x / scale), int(y / scale), int(w / scale), int(h / scale))


def main():
    parser = argparse.ArgumentParser(description="rPPG + голос live-замер для АЦ")
    parser.add_argument("--exercise", "-e", default=None,
                        help="метка упражнения (чай, письма, ...) — в имени файлов")
    parser.add_argument("--select-zone", "-s", action="store_true",
                        help="сразу выбрать плитку участника мышью")
    parser.add_argument("--follow-window", action="store_true", default=True,
                        help="захват окна Гранатума (по умолчанию вкл.)")
    parser.add_argument("--no-follow-window", dest="follow_window", action="store_false",
                        help="захват левой части экрана вместо окна браузера")
    parser.add_argument("--headless", action="store_true",
                        help="без окна HRV — звук и лог в фоне (Ctrl+C стоп)")
    parser.add_argument("--participant-top", action="store_true", default=True,
                        help="участник в верхней плитке Гранатума (по умолчанию)")
    parser.add_argument("--all-faces", dest="participant_top", action="store_false",
                        help="не ограничивать верхней плиткой")
    args = parser.parse_args()

    # graceful shutdown: SIGTERM/SIGHUP (kill, закрытие терминала, остановка из RC)
    # поднимают KeyboardInterrupt -> отрабатывает finally -> ffmpeg штатно закрывает m4a.
    def _graceful_stop(signum, _frame):
        raise KeyboardInterrupt
    for _sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(_sig, _graceful_stop)
        except Exception:
            pass

    stamp, log_path, pause_path, audio_path = session_paths(args.exercise)
    LOG_PATH = log_path

    sct = mss.mss()
    full = sct.monitors[1]
    HRV_WIN_W, HRV_WIN_H = 560, 920
    HRV_RESERVED = HRV_WIN_W + 30
    capture_w = max(640, full["width"] - HRV_RESERVED)
    capture_h = full["height"]
    fallback_monitor = {"top": 0, "left": 0, "width": capture_w, "height": capture_h}
    monitor = dict(fallback_monitor)
    browser_title = None
    if args.follow_window:
        win_mon, browser_title = find_browser_window_monitor()
        if win_mon:
            monitor = win_mon
            print(f"захват окна: {browser_title}")
        else:
            print("окно Гранатума не найдено — захват левой части экрана")
    else:
        print(f"захват экрана: {capture_w}x{capture_h}")
    cascade = cv2.CascadeClassifier(_CASCADE_PATH)
    if cascade.empty():
        print("Не найден Haar-каскад OpenCV.")
        return
    scale = DETECT_WIDTH / float(monitor["width"])

    last_box = None
    track_center = None       # фиксированная точка участника
    track_size = None         # размер его лица
    face_idx = 0              # порядковый номер в текущей сортировке (для подписи)
    faces_total = 0
    switch_requested = False
    miss_count = 0            # сколько детекций подряд участник не найден
    zone = None               # (zx, zy, zw, zh) - плитка участника в координатах экрана
    select_zone_requested = False
    clear_zone_requested = False

    maxlen = TARGET_FPS * BUFFER_SECONDS
    rgb_buf = deque(maxlen=maxlen)
    green_buf = deque(maxlen=maxlen)
    t_buf = deque(maxlen=maxlen)
    hr_history = deque(maxlen=120)
    baseline_hr = None
    log_rows = 0

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not LOG_PATH.exists() or LOG_PATH.stat().st_size == 0
    log_f = open(LOG_PATH, "a", newline="")
    logger = csv.writer(log_f)
    if new_file:
        logger.writerow(["timestamp", "hr_bpm", "snr", "rmssd_ms_trend", "resp_per_min",
                         "face_idx", "video_ok", "voice_db", "voice_voiced", "voice_pitch_hz",
                         "voice_speech_ratio", "voice_tempo_per_min",
                         "voice_pauses_per_min", "voice_avg_pause_sec"])
        log_f.flush()

    pause_f = open(pause_path, "a", newline="")
    pause_logger = csv.writer(pause_f)
    new_pause_file = not pause_path.exists() or pause_path.stat().st_size == 0
    if new_pause_file:
        pause_logger.writerow(["pause_start", "pause_end", "duration_sec"])
        pause_f.flush()

    audio = AudioMonitor()
    audio.start()
    if audio.error:
        print(f"внимание: аудио-метрики не запустились — {audio.error}")
    else:
        print(f"аудио-метрики: {audio.source_label}")

    audio_rec = AudioFileRecorder(audio_path)
    audio_rec.start()
    if audio_rec.error:
        print(f"внимание: запись m4a не стартовала — {audio_rec.error}")
    else:
        print(f"запись звука: {audio_path}")

    # отслеживание пауз >1с для отдельного лога
    pause_start_t = None
    last_voiced_state = False

    if not args.headless:
        cv2.namedWindow("HRV", cv2.WINDOW_AUTOSIZE)
        cv2.moveWindow("HRV", full["width"] - HRV_WIN_W - 10, 0)
        cv2.setWindowProperty("HRV", cv2.WND_PROP_TOPMOST, 1)
        cv2.setMouseCallback("HRV", on_mouse_panel)
    print("Роман = верхняя плитка, нижние лица игнорируются")
    print("Клавиши в ЭТОМ терминале (не в окне HRV): n s b q")
    print("Свернуть Гранатум нельзя. Звук + m4a пишутся всегда.")

    print("\n=== идёт замер ===")
    print(f"сессия: {stamp}")
    print(f"лог rPPG+голос: {LOG_PATH}")
    print(f"лог пауз:       {pause_path}")
    print(f"аудио m4a:      {audio_path}")
    print("клавиши (в этом терминале, латиница):")
    print("  n = другое лицо в верхней плитке")
    print("  s = обвести плитку мышью")
    print("  b = baseline покоя")
    print("  q = выход\n")

    zone_auto = args.participant_top
    zone_initialized = False

    stdin_tty_old = None
    if sys.stdin.isatty():
        try:
            stdin_tty_old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except Exception:
            stdin_tty_old = None

    if args.select_zone:
        select_zone_requested = True

    frame_interval = 1.0 / TARGET_FPS
    last_calc = 0.0
    frame_i = 0
    hr = rmssd = resp = None
    snr = 0.0
    face_preview = None
    tracking_lost = False
    last_rgb_t = 0.0
    video_ok = True
    window_visible = True
    camera_off = False
    zone_only = False
    paused = False

    try:
        while True:
            t0 = time.time()
            if args.follow_window and frame_i % 45 == 0:
                win_mon, bt = find_browser_window_monitor()
                if win_mon:
                    monitor = win_mon
                    browser_title = bt
                elif frame_i > 0:
                    monitor = dict(fallback_monitor)
                window_visible = True
            frame = np.ascontiguousarray(np.array(sct.grab(monitor))[:, :, :3])
            if zone_auto and not zone_initialized:
                zone = auto_participant_zone(frame.shape, frame)
                zone_initialized = True
                last_box = None
                track_center = None
                track_size = None
                reset_buffers(rgb_buf, green_buf, t_buf, hr_history)
                print(f"авто-зона участника (верх): {zone}")
            camera_off = zone is not None and zone_is_dark(frame, zone)

            if select_zone_requested:
                zone = select_zone_with_mouse(frame)
                if zone is not None:
                    # Ручной выбор плитки фиксируем — авто-пересчёт больше не трогает зону
                    zone_auto = False
                    # Сбрасываем трекинг — лицо будет искаться внутри новой плитки
                    last_box = None
                    track_center = None
                    track_size = None
                    reset_buffers(rgb_buf, green_buf, t_buf, hr_history)
                    hr = rmssd = resp = None
                    snr = 0.0
                    miss_count = 0
                    tracking_lost = False
                    print(f"плитка участника: {zone}")
                select_zone_requested = False

            if clear_zone_requested:
                zone = None
                last_box = None
                track_center = None
                track_size = None
                reset_buffers(rgb_buf, green_buf, t_buf, hr_history)
                hr = rmssd = resp = None
                snr = 0.0
                clear_zone_requested = False
                print("плитка снята — меряем по всему экрану")

            if (frame_i % DETECT_EVERY == 0 or switch_requested) and not paused:
                if zone is not None:
                    zx, zy, zw, zh = zone
                    sub_frame = frame[zy:zy + zh, zx:zx + zw]
                    boxes_sub = detect_faces(sub_frame, cascade,
                                             min(1.0, DETECT_WIDTH / max(1, zw)))
                    boxes = [(b[0] + zx, b[1] + zy, b[2], b[3]) for b in boxes_sub]
                else:
                    boxes = detect_faces(frame, cascade, scale)
                # пересчёт зоны раз в ~12 с — если сдвинули окно
                if zone_auto and frame_i > 0 and frame_i % 180 == 0:
                    zone = auto_participant_zone(frame.shape, frame)
                faces_total = len(boxes)

                if switch_requested:
                    if boxes:
                        # перейти на следующее по размеру относительно текущего
                        cur_idx = -1
                        if last_box is not None:
                            cx0 = last_box[0] + last_box[2] / 2
                            cy0 = last_box[1] + last_box[3] / 2
                            for i, b in enumerate(boxes):
                                bcx, bcy = b[0] + b[2] / 2, b[1] + b[3] / 2
                                if abs(bcx - cx0) < 60 and abs(bcy - cy0) < 60:
                                    cur_idx = i
                                    break
                        face_idx = (cur_idx + 1) % faces_total if cur_idx >= 0 else 0
                        last_box = boxes[face_idx]
                        track_center = (last_box[0] + last_box[2] / 2.0,
                                        last_box[1] + last_box[3] / 2.0)
                        track_size = (last_box[2] + last_box[3]) / 2.0
                        reset_buffers(rgb_buf, green_buf, t_buf, hr_history)
                        hr = rmssd = resp = None
                        snr = 0.0
                        miss_count = 0
                        tracking_lost = False
                        print(f"переключение -> участник {face_idx + 1}/{faces_total}")
                    switch_requested = False
                else:
                    if track_center is None:
                        if boxes:
                            fh = frame.shape[0]
                            if zone is not None:
                                pick = pick_participant_face(boxes, fh)
                            elif args.participant_top:
                                pick = pick_participant_face(boxes, fh)
                            else:
                                pick = boxes[0]
                            if pick is not None:
                                last_box = pick
                                track_center = (pick[0] + pick[2] / 2.0,
                                                pick[1] + pick[3] / 2.0)
                                track_size = (pick[2] + pick[3]) / 2.0
                                face_idx = 0
                                miss_count = 0
                                print("захват: верхнее лицо (участник)")
                    else:
                        tracked = find_tracked_face(boxes, track_center, track_size)
                        if tracked is not None:
                            last_box = tracked
                            cx = tracked[0] + tracked[2] / 2.0
                            cy = tracked[1] + tracked[3] / 2.0
                            sz = (tracked[2] + tracked[3]) / 2.0
                            track_center = (0.7 * track_center[0] + 0.3 * cx,
                                            0.7 * track_center[1] + 0.3 * cy)
                            track_size = 0.7 * track_size + 0.3 * sz
                            if tracked in boxes:
                                face_idx = boxes.index(tracked)
                            miss_count = 0
                            tracking_lost = False
                        else:
                            miss_count += 1
                            if miss_count > 30 and zone is None:
                                tracking_lost = True
            frame_i += 1

            zone_only = False
            if zone is not None and not camera_off:
                roi = zone_forehead_roi(zone)
                zone_only = last_box is None or tracking_lost
                if last_box is not None and not tracking_lost:
                    roi = forehead_roi(last_box, frame.shape)
                    zone_only = False
            elif not tracking_lost and last_box is not None:
                roi = forehead_roi(last_box, frame.shape)
            else:
                roi = None
            rgb_mean = forehead_mean_rgb(frame, roi)
            if rgb_mean is not None and window_visible and not camera_off and not paused:
                rgb_buf.append(rgb_mean)
                green_buf.append(rgb_mean[1])
                t_buf.append(t0)
                last_rgb_t = t0

            video_ok = (window_visible and not camera_off
                        and time.time() - last_rgb_t < 2.5 and len(rgb_buf) > TARGET_FPS * 5)

            # превью: плитка участника или лицо
            face_preview = None
            if zone is not None and not camera_off:
                zx, zy, zw, zh = zone
                H, W = frame.shape[:2]
                x1, y1 = max(zx, 0), max(zy, 0)
                x2, y2 = min(zx + zw, W), min(zy + zh, H)
                if x2 - x1 > 10 and y2 - y1 > 10:
                    face_preview = frame[y1:y2, x1:x2].copy()
            elif last_box is not None and not tracking_lost:
                x, y, w, h = last_box
                H, W = frame.shape[:2]
                x1, y1 = max(x, 0), max(y, 0)
                x2, y2 = min(x + w, W), min(y + h, H)
                if x2 - x1 > 10 and y2 - y1 > 10:
                    face_preview = frame[y1:y2, x1:x2].copy()

            if time.time() - last_calc > 0.8 and len(rgb_buf) > TARGET_FPS * 5 and not paused:
                last_calc = time.time()
                span = t_buf[-1] - t_buf[0]
                fps = len(t_buf) / span if span > 0 else TARGET_FPS
                rgb_arr = np.array(rgb_buf)
                pulse = pos_signal(rgb_arr, fps)
                pulse_bp = bandpass(pulse, fps, HR_MIN_HZ, HR_MAX_HZ)
                hr_calc, snr_calc = dominant_freq_bpm(pulse_bp, fps, HR_MIN_HZ, HR_MAX_HZ)
                rmssd_calc = rmssd_trend(pulse_bp, fps)
                green = np.array(green_buf)
                green_bp = bandpass(green - green.mean(), fps, RESP_MIN_HZ, RESP_MAX_HZ)
                resp_calc, _ = dominant_freq_bpm(green_bp, fps, RESP_MIN_HZ, RESP_MAX_HZ)
                if video_ok and hr_calc and snr_calc >= HR_SNR_MIN:
                    hr, snr, rmssd, resp = hr_calc, snr_calc, rmssd_calc, resp_calc
                    hr_history.append(hr)
                elif hr_calc is not None:
                    snr = snr_calc
                asnap = audio.snapshot() if audio else None
                if asnap and not asnap.get("error"):
                    voiced_now = asnap["voiced"]
                    if not voiced_now:
                        if pause_start_t is None and last_voiced_state:
                            pause_start_t = time.time()
                    elif pause_start_t is not None:
                        dur = time.time() - pause_start_t
                        if dur >= 1.0:
                            end_t = time.time()
                            pause_logger.writerow([
                                datetime.fromtimestamp(pause_start_t).isoformat(timespec="seconds"),
                                datetime.fromtimestamp(end_t).isoformat(timespec="seconds"),
                                f"{dur:.2f}",
                            ])
                            pause_f.flush()
                        pause_start_t = None
                    last_voiced_state = voiced_now
                snr_log = snr if (video_ok and hr and snr >= HR_SNR_MIN) else ""
                if hr or (asnap and not asnap.get("error")):
                    logger.writerow([
                        datetime.now().isoformat(timespec="seconds"),
                        f"{hr:.1f}" if (video_ok and hr and snr >= HR_SNR_MIN) else "",
                        f"{snr:.1f}" if snr_log else "",
                        f"{rmssd:.1f}" if (video_ok and rmssd) else "",
                        f"{resp:.1f}" if (video_ok and resp) else "",
                        face_idx,
                        "1" if video_ok else "0",
                        f"{asnap['rms_db']:.1f}" if asnap and not asnap.get("error") else "",
                        "1" if asnap and asnap.get("voiced") else ("0" if asnap else ""),
                        f"{asnap['pitch_med']:.0f}" if asnap and asnap.get("pitch_med") else "",
                        f"{asnap['speech_ratio']:.3f}" if asnap and not asnap.get("error") else "",
                        f"{asnap['tempo_per_min']:.1f}" if asnap and asnap.get("tempo_per_min") else "",
                        f"{asnap['pauses_per_min']:.2f}" if asnap and asnap.get("pauses_per_min") is not None else "",
                        f"{asnap['avg_pause_sec']:.2f}" if asnap and asnap.get("avg_pause_sec") else "",
                    ])
                    log_f.flush()
                    log_rows += 1

            buf_sec = (t_buf[-1] - t_buf[0]) if len(t_buf) > 1 else 0.0
            audio_snap = audio.snapshot() if audio else None
            panel = render_dashboard(face_preview, hr, snr, rmssd, resp,
                                     baseline_hr, buf_sec, face_idx, faces_total,
                                     hr_history, LOG_PATH, log_rows, tracking_lost,
                                     audio_snap, zone, video_ok, camera_off, zone_only,
                                     paused)
            if not args.headless:
                cv2.imshow("HRV", panel)
            key = cv2.waitKey(1) & 0xFF if not args.headless else 0xFF
            # клик по кнопке в окне HRV
            if _PENDING_KEY["k"] is not None:
                key = _PENDING_KEY["k"]
                _PENDING_KEY["k"] = None
            # горячие клавиши из терминала (дублируют кнопки)
            if select.select([sys.stdin], [], [], 0)[0]:
                try:
                    ch = sys.stdin.read(1).lower()
                    if ch in "nsbcpq":
                        key = ord(ch)
                except Exception:
                    pass
            if key == ord("q"):
                break
            if key == ord("p"):
                paused = not paused
                if not paused:
                    # возобновление: сброс буферов, чтобы пауза не испортила fps/HR
                    reset_buffers(rgb_buf, green_buf, t_buf)
                    last_rgb_t = time.time()
                    last_calc = time.time()
                print("ПАУЗА" if paused else "продолжаем замер")
            if key == ord("b") and hr and video_ok and not paused:
                baseline_hr = hr
                print(f"baseline покоя зафиксирован: {hr:.0f} уд/мин")
            if key == ord("n") and not paused:
                switch_requested = True
            if key == ord("s") and not paused:
                select_zone_requested = True
            if key == ord("c") and not paused:
                clear_zone_requested = True

            dt = time.time() - t0
            if dt < frame_interval:
                time.sleep(frame_interval - dt)
    finally:
        if stdin_tty_old is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, stdin_tty_old)
            except Exception:
                pass
        try:
            audio.stop()
        except Exception:
            pass
        try:
            audio_rec.stop()
        except Exception:
            pass
        log_f.close()
        pause_f.close()
        cv2.destroyAllWindows()
        print(f"\nготово. строк в логе: {log_rows}")
        print(f"  rPPG+голос: {LOG_PATH}")
        print(f"  паузы:      {pause_path}")
        print(f"  аудио:      {audio_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
