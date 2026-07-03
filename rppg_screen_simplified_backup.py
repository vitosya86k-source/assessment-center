"""
rPPG-инструмент: живой пульс/дыхание/HRV-тренд по лицу из экрана.

Зачем: во время ассессмента в браузере (Гранатум) видеть пульс участника в моменте.
Не трогает браузер — только читает пиксели экрана (mss), работе не мешает.

Запуск:
    cd "<.../NEW VERSION>"
    venv_new/bin/python rppg_screen.py

Логика выбора лица:
- Захват только ВЕРХНИЕ 55% экрана (в Гранатуме там плитка участника).
- При старте — самое крупное лицо в этой области.
- Нажми 's' — обвести мышью плитку участника, если автовыбор не угадал.
- Нажми 'n' — следующее лицо. Буферы сбрасываются.

Клавиши (фокус на окне HRV):
    s — выбрать плитку мышью
    n — следующий участник
    b — baseline покоя (~60 сек спокойного сидения)
    c — снять плитку
    q — выход
"""

import os
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

TARGET_FPS = 15
BUFFER_SECONDS = 18
HR_MIN_HZ, HR_MAX_HZ = 0.7, 3.5
RESP_MIN_HZ, RESP_MAX_HZ = 0.1, 0.5
DETECT_WIDTH = 800
DETECT_EVERY = 4
LOG_PATH = Path(__file__).parent / "data" / "rppg_live_log.csv"
_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"

AUDIO_SR = 16000
AUDIO_BLOCK = 1024
AUDIO_ENV_KEEP_SEC = 60
PITCH_MIN_HZ, PITCH_MAX_HZ = 70, 350

_FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_CACHE = {}

GREEN = (0, 220, 0)
YELLOW = (235, 220, 70)
ORANGE = (255, 150, 0)
RED = (255, 70, 70)
GREY = (170, 170, 170)
DGREY = (60, 60, 60)
WHITE = (240, 240, 240)


def _font(size, bold=False):
    key = (size, bold)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = ImageFont.truetype(_FONT_BLD if bold else _FONT_REG, size)
    return _FONT_CACHE[key]


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


def find_pulse_monitor_source():
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
    def __init__(self):
        self.sr = AUDIO_SR
        self.block = AUDIO_BLOCK
        self.lock = threading.Lock()
        env_maxlen = int(AUDIO_ENV_KEEP_SEC * self.sr / self.block)
        self.rms_env = deque(maxlen=env_maxlen)
        self.voiced_hist = deque(maxlen=env_maxlen)
        self.pitch_hist = deque(maxlen=env_maxlen)
        self.voiced_buf = np.zeros(0, dtype=np.float32)
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
        thr = max(self.noise_floor * 3.5, 0.004)
        voiced = rms > thr
        if not voiced:
            self.noise_floor = 0.95 * self.noise_floor + 0.05 * rms
        if voiced:
            self.voiced_buf = np.concatenate([self.voiced_buf, x])
            keep = int(self.sr * 0.7)
            if len(self.voiced_buf) > keep:
                self.voiced_buf = self.voiced_buf[-keep:]
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
        monitor = find_pulse_monitor_source()
        try:
            if monitor:
                os.environ["PULSE_SOURCE"] = monitor
                self.source_label = "PulseAudio monitor"
                device = "pulse"
            else:
                self.source_label = "микрофон"
                device = None
            self.stream = sd.InputStream(
                samplerate=self.sr, blocksize=self.block, channels=1,
                dtype="float32", device=device, callback=self._callback,
            )
            self.stream.start()
        except Exception as e:
            self.error = str(e)
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
                self.error = f"{e}; {e2}"
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
        pitch_med = float(np.median(pitch_arr[-40:])) if len(pitch_arr) >= 4 else pitch
        tempo = pauses_per_min = avg_pause_sec = None
        if len(v_hist) > 50:
            v = np.array(v_hist, dtype=np.int8)
            risings = int(np.sum((v[1:] == 1) & (v[:-1] == 0)))
            secs = len(v) * self.block / self.sr
            if secs > 5:
                tempo = risings * 60.0 / secs
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
            if secs > 5:
                pauses_per_min = len(pause_lens) * 60.0 / secs
                if pause_lens:
                    avg_pause_sec = float(np.mean(pause_lens))
        return {
            "rms_db": rms_db, "voiced": voiced, "pitch_hz": pitch,
            "pitch_med": pitch_med, "speech_ratio": speech_ratio,
            "tempo_per_min": tempo, "pauses_per_min": pauses_per_min,
            "avg_pause_sec": avg_pause_sec, "source": self.source_label,
            "error": self.error,
        }


def detect_faces(frame_bgr, cascade, scale):
    small = cv2.resize(frame_bgr, None, fx=scale, fy=scale)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(50, 50))
    if len(faces) == 0:
        return []
    boxes = [(int(fx / scale), int(fy / scale), int(fw / scale), int(fh / scale))
             for (fx, fy, fw, fh) in faces]
    boxes.sort(key=lambda b: b[2] * b[3], reverse=True)
    return boxes


def find_tracked_face(boxes, t_center, t_size):
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
        if d > (t_size or size) * 0.8:
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


def default_participant_roi(frame_shape, zone=None):
    """Центр верхней плитки — rPPG работает даже когда Haar не видит лицо."""
    H, W = frame_shape[:2]
    if zone is not None:
        zx, zy, zw, zh = zone
        cx = zx + zw // 2
        cy = zy + int(zh * 0.22)
        hw, hh = max(6, int(zw * 0.18)), max(6, int(zh * 0.12))
    else:
        left_margin = int(W * 0.07)
        usable_w = W - left_margin
        cx = left_margin + usable_w // 2
        cy = int(H * 0.22)
        hw, hh = max(6, int(usable_w * 0.18)), max(6, int(H * 0.12))
    x_lo, x_hi = max(0, cx - hw), min(W, cx + hw)
    y_lo, y_hi = max(0, cy - hh), min(H, cy + hh)
    if x_hi - x_lo < 6 or y_hi - y_lo < 6:
        return None
    return (x_lo, y_lo, x_hi, y_hi)


def forehead_mean_rgb(frame_bgr, roi):
    if roi is None:
        return None
    x_lo, y_lo, x_hi, y_hi = roi
    patch = frame_bgr[y_lo:y_hi, x_lo:x_hi]
    mean_bgr = patch.reshape(-1, 3).mean(axis=0)
    return np.array([mean_bgr[2], mean_bgr[1], mean_bgr[0]])


def interp_hr(hr):
    if hr is None:
        return "...", GREY
    if hr > 130:
        return "АРТЕФАКТ?", RED
    if hr > 100:
        return "ВЫСОКИЙ", ORANGE
    if hr >= 85:
        return "слегка повышен", YELLOW
    if 60 <= hr < 85:
        return "норма покоя", GREEN
    return "НИЗКИЙ", ORANGE


def interp_resp(r):
    if r is None:
        return "...", GREY
    if r > 22:
        return "часто", ORANGE
    if r < 10:
        return "редко", ORANGE
    return "норма 12-20", GREEN


def interp_signal(snr):
    if snr is None or snr < 2:
        return "шумно", ORANGE
    if snr < 4:
        return "средне", YELLOW
    return "чисто", GREEN


def interp_rmssd(r):
    return ("тренд", GREY) if r else ("...", GREY)


def interp_pitch(pitch, baseline_pitch):
    if pitch is None:
        return "тишина", GREY
    return f"{pitch:.0f} Гц", WHITE


def interp_speech_ratio(r):
    if r is None:
        return "...", GREY
    pct = r * 100
    if pct < 5:
        return "молчит", GREY
    if pct < 25:
        return "мало", YELLOW
    if pct < 65:
        return "активно", GREEN
    return "доминирует", ORANGE


def interp_tempo(tempo):
    if tempo is None:
        return "...", GREY
    if tempo < 100:
        return "медленная", YELLOW
    if tempo < 150:
        return "норма", GREEN
    if tempo < 200:
        return "быстрая", ORANGE
    return "стресс", RED


def interp_pauses(pp):
    if pp is None:
        return "...", GREY
    if pp < 2:
        return "уверенная", GREEN
    if pp < 5:
        return "обдумывает", YELLOW
    return "затрудняется", ORANGE


def render_dashboard(face_preview_bgr, hr, snr, rmssd, resp, baseline, buf_sec,
                     face_idx, faces_total, hr_history, log_path, log_rows,
                     tracking_lost, audio, zone, using_fallback=False):
    W, Hh = 560, 920
    img = Image.new("RGB", (W, Hh), (0, 0, 0))
    d = ImageDraw.Draw(img)

    if zone is not None:
        hdr, hdr_col = "плитка зафиксирована", GREEN
    elif faces_total > 0 and not using_fallback:
        hdr = f"лицо {face_idx + 1} из {faces_total}"
        hdr_col = GREY if tracking_lost else WHITE
    elif using_fallback:
        hdr, hdr_col = "авто-лоб (центр плитки)", YELLOW
    else:
        hdr, hdr_col = "лицо не найдено", ORANGE
    d.text((14, 10), hdr, font=_font(24, bold=True), fill=hdr_col)
    d.text((14, 44), "N лицо  S плитка  B покой  Q выход",
           font=_font(15, bold=True), fill=YELLOW)

    pv_x, pv_y, pv_w, pv_h = 14, 80, 170, 130
    d.rectangle((pv_x, pv_y, pv_x + pv_w, pv_y + pv_h), outline=DGREY, width=2)
    if face_preview_bgr is not None:
        ph, pw = face_preview_bgr.shape[:2]
        if ph > 0 and pw > 0:
            scale = min(pv_w / pw, pv_h / ph)
            nw, nh = max(1, int(pw * scale)), max(1, int(ph * scale))
            face_rgb = cv2.cvtColor(face_preview_bgr, cv2.COLOR_BGR2RGB)
            face_im = Image.fromarray(cv2.resize(face_rgb, (nw, nh)))
            ox, oy = pv_x + (pv_w - nw) // 2, pv_y + (pv_h - nh) // 2
            img.paste(face_im, (ox, oy))
            d.rectangle((ox + int(0.30 * nw), oy + int(0.08 * nh),
                         ox + int(0.70 * nw), oy + int(0.28 * nh)),
                        outline=YELLOW, width=2)
    else:
        d.text((pv_x + 8, pv_y + 60), "жду лицо", font=_font(14), fill=GREY)

    mx = 200
    d.text((mx, 80), "ПУЛЬС", font=_font(15, bold=True), fill=GREY)
    if hr:
        hr_int, hr_col = interp_hr(hr)
        d.text((mx, 100), f"{hr:.0f}", font=_font(54, bold=True), fill=hr_col)
        d.text((mx + 140, 132), "уд/мин", font=_font(18, bold=True), fill=hr_col)
        d.text((mx, 168), hr_int, font=_font(15, bold=True), fill=hr_col)
    else:
        d.text((mx, 108), f"копим {buf_sec:.0f}с", font=_font(20), fill=GREY)

    d.text((14, 224), "ДЫХАНИЕ", font=_font(15, bold=True), fill=GREY)
    if resp:
        r_int, r_col = interp_resp(resp)
        d.text((110, 220), f"{resp:.0f}/мин", font=_font(28, bold=True), fill=r_col)
        d.text((280, 226), r_int, font=_font(15, bold=True), fill=r_col)
    else:
        d.text((110, 220), "...", font=_font(28), fill=GREY)

    d.text((14, 262), "сигнал", font=_font(14, bold=True), fill=GREY)
    s_int, s_col = interp_signal(snr)
    d.text((100, 260), f"SNR {snr:.1f}" if snr else "—", font=_font(17, bold=True), fill=s_col)
    d.text((200, 262), s_int, font=_font(14, bold=True), fill=s_col)

    ay = 300
    d.text((14, ay), "ГОЛОС", font=_font(18, bold=True), fill=WHITE)
    if audio and not audio.get("error"):
        rms_db = audio["rms_db"]
        vx, vy, vw, vh = 14, ay + 28, 360, 18
        d.rectangle((vx, vy, vx + vw, vy + vh), outline=DGREY, width=1)
        lvl = max(0.0, min(1.0, (rms_db + 60) / 60.0))
        bar_col = GREEN if audio["voiced"] else GREY
        d.rectangle((vx + 2, vy + 2, vx + 2 + int((vw - 4) * lvl), vy + vh - 2), fill=bar_col)
        d.text((vx + vw + 10, vy - 2), f"{rms_db:5.1f} dB", font=_font(14, bold=True), fill=bar_col)
        ty = ay + 58
        for label, val, interp_fn, args in [
            ("питч", audio.get("pitch_med"), interp_pitch, (audio.get("pitch_med"), None)),
            ("доля речи", audio.get("speech_ratio"), interp_speech_ratio, (audio.get("speech_ratio"),)),
            ("темп", audio.get("tempo_per_min"), interp_tempo, (audio.get("tempo_per_min"),)),
            ("паузы", audio.get("pauses_per_min"), interp_pauses, (audio.get("pauses_per_min"),)),
        ]:
            d.text((14, ty), label, font=_font(14), fill=GREY)
            if val is not None and (not isinstance(val, float) or val == val):
                if isinstance(val, float):
                    txt = f"{val:.0f}" if label != "доля речи" else f"{val * 100:.0f}%"
                else:
                    txt = str(val)
                t_int, t_col = interp_fn(*args)
                d.text((120, ty - 2), txt, font=_font(20, bold=True), fill=t_col)
                d.text((260, ty), t_int, font=_font(14), fill=t_col)
            ty += 30

    gx, gy, gw, gh = 14, 580, 510, 130
    d.text((gx, gy - 22), "график пульса", font=_font(15, bold=True), fill=GREY)
    d.rectangle((gx, gy, gx + gw, gy + gh), outline=DGREY, width=1)
    arr = [v for v in hr_history if v is not None and 40 < v < 180]
    if len(arr) >= 2:
        lo, hi = max(40, min(arr) - 5), min(180, max(arr) + 5)
        pts = []
        for i, v in enumerate(arr):
            xv = gx + int(i * gw / max(1, len(arr) - 1))
            yv = gy + gh - int((v - lo) / max(hi - lo, 1) * gh)
            pts.append((xv, yv))
        d.line(pts, fill=GREEN, width=2)

    d.text((14, 730), f"лог: {log_path.name}  строк: {log_rows}",
           font=_font(14, bold=True), fill=GREEN)
    d.text((14, 760), "Захват: верхние 55% = плитка участника",
           font=_font(13, bold=True), fill=YELLOW)
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def reset_buffers(*buffers):
    for b in buffers:
        b.clear()


def select_zone_with_mouse(frame_bgr):
    H, W = frame_bgr.shape[:2]
    max_w = 1400
    scale = min(1.0, max_w / W)
    preview = cv2.resize(frame_bgr, (int(W * scale), int(H * scale))) if scale < 1 else frame_bgr.copy()
    title = "Обведите плитку участника (Enter, Esc отмена)"
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
    sct = mss.MSS()
    full = sct.monitors[1]
    HRV_WIN_W = 560
    HRV_RESERVED = HRV_WIN_W + 30
    capture_w = max(640, full["width"] - HRV_RESERVED)
    capture_h = int(full["height"] * 0.55)
    monitor = {"top": 0, "left": 0, "width": capture_w, "height": capture_h}
    cascade = cv2.CascadeClassifier(_CASCADE_PATH)
    if cascade.empty():
        print("Не найден Haar-каскад.")
        return
    scale = DETECT_WIDTH / float(monitor["width"])

    last_box = None
    track_center = track_size = None
    face_idx = faces_total = 0
    switch_requested = select_zone_requested = clear_zone_requested = False
    miss_count = tracking_lost = 0
    zone = None

    rgb_buf = deque(maxlen=TARGET_FPS * BUFFER_SECONDS)
    green_buf = deque(maxlen=TARGET_FPS * BUFFER_SECONDS)
    t_buf = deque(maxlen=TARGET_FPS * BUFFER_SECONDS)
    hr_history = deque(maxlen=120)
    baseline_hr = None
    log_rows = 0

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not LOG_PATH.exists() or LOG_PATH.stat().st_size == 0
    log_f = open(LOG_PATH, "a", newline="")
    logger = csv.writer(log_f)
    if new_file:
        logger.writerow(["timestamp", "hr_bpm", "snr", "rmssd_ms_trend", "resp_per_min",
                           "face_idx", "voice_db", "voice_pitch_hz", "voice_speech_ratio",
                           "voice_tempo_per_min", "voice_pauses_per_min", "voice_avg_pause_sec"])
        log_f.flush()

    audio = AudioMonitor()
    audio.start()
    print(f"аудио: {audio.source_label}" + (f" ({audio.error})" if audio.error else ""))

    cv2.namedWindow("HRV", cv2.WINDOW_AUTOSIZE)
    cv2.moveWindow("HRV", full["width"] - HRV_WIN_W - 10, 0)
    cv2.setWindowProperty("HRV", cv2.WND_PROP_TOPMOST, 1)
    print(f"захват: верхние 55% ({capture_w}x{capture_h}) — там участник")
    print(f"лог: {LOG_PATH}")
    print("клавиши в окне HRV: S плитка, N лицо, B покой, Q выход\n")

    frame_interval = 1.0 / TARGET_FPS
    last_calc = frame_i = 0
    hr = rmssd = resp = None
    snr = 0.0
    face_preview = None

    try:
        while True:
            t0 = time.time()
            frame = np.ascontiguousarray(np.array(sct.grab(monitor))[:, :, :3])

            if select_zone_requested:
                zone = select_zone_with_mouse(frame)
                if zone:
                    last_box = track_center = track_size = None
                    reset_buffers(rgb_buf, green_buf, t_buf, hr_history)
                    hr = rmssd = resp = None
                    snr = miss_count = 0
                    tracking_lost = False
                    print(f"плитка: {zone}")
                select_zone_requested = False

            if clear_zone_requested:
                zone = None
                last_box = track_center = track_size = None
                reset_buffers(rgb_buf, green_buf, t_buf, hr_history)
                hr = rmssd = resp = None
                clear_zone_requested = False

            if frame_i % DETECT_EVERY == 0 or switch_requested:
                if zone is not None:
                    zx, zy, zw, zh = zone
                    sub = frame[zy:zy + zh, zx:zx + zw]
                    boxes_sub = detect_faces(sub, cascade, min(1.0, DETECT_WIDTH / max(1, zw)))
                    boxes = [(b[0] + zx, b[1] + zy, b[2], b[3]) for b in boxes_sub]
                else:
                    left_margin = int(frame.shape[1] * 0.07)
                    detect_frame = frame[:, left_margin:]
                    boxes = detect_faces(detect_frame, cascade, scale)
                    boxes = [(b[0] + left_margin, b[1], b[2], b[3]) for b in boxes]
                faces_total = len(boxes)

                if switch_requested and boxes:
                    cur = -1
                    if last_box:
                        cx0 = last_box[0] + last_box[2] / 2
                        cy0 = last_box[1] + last_box[3] / 2
                        for i, b in enumerate(boxes):
                            if abs(b[0] + b[2] / 2 - cx0) < 60 and abs(b[1] + b[3] / 2 - cy0) < 60:
                                cur = i
                                break
                    face_idx = (cur + 1) % faces_total
                    last_box = boxes[face_idx]
                    track_center = (last_box[0] + last_box[2] / 2, last_box[1] + last_box[3] / 2)
                    track_size = (last_box[2] + last_box[3]) / 2
                    reset_buffers(rgb_buf, green_buf, t_buf, hr_history)
                    hr = rmssd = resp = None
                    switch_requested = False
                elif track_center is None and boxes:
                    pick = boxes[0]
                    last_box = pick
                    track_center = (pick[0] + pick[2] / 2, pick[1] + pick[3] / 2)
                    track_size = (pick[2] + pick[3]) / 2
                    face_idx = 0
                elif track_center is not None:
                    tracked = find_tracked_face(boxes, track_center, track_size)
                    if tracked:
                        last_box = tracked
                        cx = tracked[0] + tracked[2] / 2
                        cy = tracked[1] + tracked[3] / 2
                        track_center = (0.7 * track_center[0] + 0.3 * cx,
                                        0.7 * track_center[1] + 0.3 * cy)
                        track_size = 0.7 * track_size + 0.3 * (tracked[2] + tracked[3]) / 2
                        miss_count = tracking_lost = 0
                    else:
                        miss_count += 1
                        if miss_count > 30:
                            tracking_lost = True
                if switch_requested:
                    switch_requested = False
            frame_i += 1

            using_fallback = last_box is None or tracking_lost
            if last_box is not None and not tracking_lost:
                roi = forehead_roi(last_box, frame.shape)
            else:
                roi = default_participant_roi(frame.shape, zone)
            rgb_mean = forehead_mean_rgb(frame, roi)
            if rgb_mean is not None:
                rgb_buf.append(rgb_mean)
                green_buf.append(rgb_mean[1])
                t_buf.append(t0)

            if roi is not None:
                x_lo, y_lo, x_hi, y_hi = roi
                face_preview = frame[y_lo:y_hi, x_lo:x_hi].copy()
            else:
                face_preview = None

            if time.time() - last_calc > 0.8 and len(rgb_buf) > TARGET_FPS * 5:
                last_calc = time.time()
                span = t_buf[-1] - t_buf[0]
                fps = len(t_buf) / span if span > 0 else TARGET_FPS
                pulse = pos_signal(np.array(rgb_buf), fps)
                pulse_bp = bandpass(pulse, fps, HR_MIN_HZ, HR_MAX_HZ)
                hr, snr = dominant_freq_bpm(pulse_bp, fps, HR_MIN_HZ, HR_MAX_HZ)
                rmssd = rmssd_trend(pulse_bp, fps)
                green_bp = bandpass(np.array(green_buf) - np.mean(green_buf), fps, RESP_MIN_HZ, RESP_MAX_HZ)
                resp, _ = dominant_freq_bpm(green_bp, fps, RESP_MIN_HZ, RESP_MAX_HZ)
                if hr:
                    hr_history.append(hr)
                asnap = audio.snapshot() if audio else None
                if hr or (asnap and not asnap.get("error")):
                    logger.writerow([
                        datetime.now().isoformat(timespec="seconds"),
                        f"{hr:.1f}" if hr else "", f"{snr:.1f}" if hr else "",
                        f"{rmssd:.1f}" if rmssd else "", f"{resp:.1f}" if resp else "",
                        face_idx,
                        f"{asnap['rms_db']:.1f}" if asnap and not asnap.get("error") else "",
                        f"{asnap['pitch_med']:.0f}" if asnap and asnap.get("pitch_med") else "",
                        f"{asnap['speech_ratio']:.3f}" if asnap and not asnap.get("error") else "",
                        f"{asnap['tempo_per_min']:.1f}" if asnap and asnap.get("tempo_per_min") else "",
                        f"{asnap['pauses_per_min']:.2f}" if asnap and asnap.get("pauses_per_min") is not None else "",
                        f"{asnap['avg_pause_sec']:.2f}" if asnap and asnap.get("avg_pause_sec") else "",
                    ])
                    log_f.flush()
                    log_rows += 1

            buf_sec = (t_buf[-1] - t_buf[0]) if len(t_buf) > 1 else 0.0
            panel = render_dashboard(face_preview, hr, snr, rmssd, resp, baseline_hr,
                                     buf_sec, face_idx, faces_total, hr_history,
                                     LOG_PATH, log_rows, tracking_lost,
                                     audio.snapshot() if audio else None, zone,
                                     using_fallback)
            cv2.imshow("HRV", panel)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("b") and hr:
                baseline_hr = hr
                print(f"baseline: {hr:.0f}")
            if key == ord("n"):
                switch_requested = True
            if key == ord("s"):
                select_zone_requested = True
            if key == ord("c"):
                clear_zone_requested = True
            dt = time.time() - t0
            if dt < frame_interval:
                time.sleep(frame_interval - dt)
    finally:
        audio.stop()
        log_f.close()
        cv2.destroyAllWindows()
        print(f"готово. строк: {log_rows}. {LOG_PATH}")


if __name__ == "__main__":
    main()
