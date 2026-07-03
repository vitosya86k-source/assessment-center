"""
auto_collector.py — единственный entry point, который надо запускать.

Что делает:
  1. Сидит в фоне и каждые 5 сек сканирует BLE на наличие Polar Verity Sense / H10.
  2. Как только видит устройство — автоматически подключается через PMD и стартует PPI-стрим.
  3. Пишет RR-интервалы в data/sessions/YYYYMMDD/segment_HHMMSS.csv (новый сегмент = новое подключение).
  4. Параллельно держит мини-сайт на http://localhost:8765 — показывает live HR, RMSSD, SDNN, % артефактов.
  5. Когда BLE-связь рвётся (отошла от ноута) — фиксирует сегмент, продолжает сканировать в фоне.
  6. Когда устройство снова появляется — открывает новый сегмент автоматически. Без её участия.
  7. По команде POST /finish_day склеивает все сегодняшние сегменты и зовёт analyze_session.py — выдаёт дневной HTML-отчёт.

Запуск:
    ./venv_new/bin/python auto_collector.py
    # потом в браузере: http://localhost:8765

Остановить:
    Ctrl-C, либо POST на /shutdown
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import signal
import subprocess
import sys
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import uvicorn
from bleak import BleakScanner, BleakClient
from bleakheart import PolarMeasurementData
from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from hrv_calculator import HRVCalculator
import session_history as hist
import kubios_indices as ki
from hrv_interpret import (
    hypotheses_for_change, biofeedback_recommendations, overall_picture,
    ALL as INTERP_SPECS, PRIMARY_METRICS,
)
try:
    import neurokit2 as _nk
    _NK_OK = True
except Exception:
    _NK_OK = False


_CALIB_PATH = Path(__file__).resolve().parent / "data" / "calibration.json"


def _load_calibration() -> dict:
    """Загружает калибровочные параметры. Если их нет — возвращает дефолт."""
    if _CALIB_PATH.exists():
        try:
            import json as _j
            return _j.loads(_CALIB_PATH.read_text())
        except Exception:
            pass
    return {"sdnn_method": "polynomial", "sdnn_params": {"order": 4, "resample_hz": 4.0}}


# Глобальный объект калибровки (перезагружается при каждом запросе метрик)
_CALIB_CACHE = {"data": None, "mtime": 0.0}


def _get_calibration() -> dict:
    try:
        m = _CALIB_PATH.stat().st_mtime if _CALIB_PATH.exists() else 0
    except Exception:
        m = 0
    if _CALIB_CACHE["data"] is None or m != _CALIB_CACHE["mtime"]:
        _CALIB_CACHE["data"] = _load_calibration()
        _CALIB_CACHE["mtime"] = m
    return _CALIB_CACHE["data"]


def _compute_sdnn_calibrated(rr_v: np.ndarray) -> float:
    """SDNN с учётом сохранённой калибровки (calibrate.py)."""
    naive = float(np.std(rr_v, ddof=1))
    if not _NK_OK or len(rr_v) < 60:
        return naive
    calib = _get_calibration()
    method = calib.get("sdnn_method", "polynomial")
    params = calib.get("sdnn_params", {})
    try:
        if method == "raw":
            return naive
        t_rr = np.cumsum(rr_v) / 1000.0
        hz = float(params.get("resample_hz", 4.0))
        if hz > 0:
            step = 1.0 / hz
            t_u = np.arange(t_rr[0], t_rr[-1], step)
            sig = np.interp(t_u, t_rr, rr_v)
        else:
            sig = rr_v.copy()
        if method == "polynomial":
            d = _nk.signal_detrend(sig, method="polynomial", order=int(params.get("order", 4)))
        elif method == "tarvainen":
            d = _nk.signal_detrend(sig, method="tarvainen2002", regularization=int(params.get("regularization", 500)))
        elif method == "loess":
            d = _nk.signal_detrend(sig, method="loess", alpha=float(params.get("alpha", 0.3)))
        else:
            return naive
        sdnn = float(np.std(d, ddof=1))
        # sanity: если калибровка дала нелепый результат — fallback
        if sdnn > naive * 2 or sdnn < naive * 0.3:
            return naive
        return sdnn
    except Exception:
        return naive

AXIS_LABELS = {
    "RD": "Готовность",
    "SR": "Стрессоустойчивость",
    "AD": "Адаптивность",
    "FL": "Гибкость",
    "RC": "Восстановление",
    "EN": "Выносливость",
    "BL": "Баланс",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("auto_collector")

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data" / "sessions"
DATA_ROOT.mkdir(parents=True, exist_ok=True)

DEVICE_NAME_MARKERS = ("Polar", "Verity", "H10")
SCAN_INTERVAL_SEC = 5.0
SCAN_TIMEOUT_SEC = 6.0
RECONNECT_BACKOFF_SEC = 3.0
RR_MIN_MS = 300
RR_MAX_MS = 2000
LIVE_BUFFER_SIZE = 600  # хранить последние 10 минут RR (≈1 RR/сек) для live-метрик


class Segment:
    """Один непрерывный кусок BLE-сессии — отдельный CSV-файл."""

    def __init__(self, day_dir: Path):
        ts = datetime.now()
        self.path = day_dir / f"segment_{ts.strftime('%H%M%S')}.csv"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.start_time = ts
        self.fh = self.path.open("w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.fh)
        self.writer.writerow(["Heart_Rate_bpm", "Timestamp_ISO", "RR_Interval_ms", "Second", "RR_Source", "Duration_Seconds"])
        self.n = 0
        log.info("Открыт сегмент: %s", self.path)

    def write(self, hr: float, ts_iso: str, rr_ms: float, duration_s: float) -> None:
        self.writer.writerow([f"{hr:.1f}", ts_iso, f"{rr_ms:.1f}", self.n, "pmd", f"{duration_s:.3f}"])
        self.n += 1
        if self.n % 50 == 0:
            self.fh.flush()

    def close(self) -> None:
        try:
            self.fh.flush()
            self.fh.close()
            log.info("Закрыт сегмент: %s (%d RR-интервалов)", self.path.name, self.n)
        except Exception:
            log.exception("Ошибка при закрытии сегмента")


class CollectorState:
    """Общее состояние между BLE-таском и веб-сервером."""

    def __init__(self):
        self.live_rr: deque[tuple[float, float]] = deque(maxlen=LIVE_BUFFER_SIZE)  # (timestamp_sec, rr_ms)
        # Таймсерия: каждую минуту считаем RMSSD/SDNN/HR за последние 5 мин, храним 30 точек (полчаса)
        self.timeseries: deque[dict] = deque(maxlen=180)
        self.last_ts_update_sec: float = 0.0
        self.current_segment: Optional[Segment] = None
        self.connected: bool = False
        self.device_name: Optional[str] = None
        self.device_address: Optional[str] = None
        self.last_seen: Optional[datetime] = None
        self.last_status: str = "ждём появления датчика..."
        self.shutdown_event: asyncio.Event = asyncio.Event()
        self.user_label: str = "я"  # переключается с веба для случая «дала браслет»
        self.db = hist.connect()

    def day_dir(self) -> Path:
        d = DATA_ROOT / datetime.now().strftime("%Y%m%d")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def on_rr(self, rr_ms: float, hr: float) -> None:
        if not (RR_MIN_MS <= rr_ms <= RR_MAX_MS):
            return
        now = datetime.now()
        t_sec = now.timestamp()
        self.live_rr.append((t_sec, rr_ms))
        if self.current_segment is None:
            self.current_segment = Segment(self.day_dir())
        seg = self.current_segment
        duration = (now - seg.start_time).total_seconds()
        seg.write(hr=hr, ts_iso=now.strftime("%Y-%m-%d %H:%M:%S"), rr_ms=rr_ms, duration_s=duration)

    def close_segment(self) -> None:
        if self.current_segment is None:
            return
        seg = self.current_segment
        seg.close()
        # Сохраняем сессию в БД истории
        try:
            self._save_segment_to_history(seg)
        except Exception:
            log.exception("Не удалось сохранить сегмент в историю")
        self.current_segment = None

    def _save_segment_to_history(self, seg) -> None:
        import csv as _csv
        rr_list: list[float] = []
        hr_list: list[float] = []
        with seg.path.open("r", encoding="utf-8") as fh:
            reader = _csv.DictReader(fh)
            for row in reader:
                try:
                    rr = float(row["RR_Interval_ms"])
                    hr = float(row["Heart_Rate_bpm"])
                except (KeyError, ValueError):
                    continue
                if RR_MIN_MS <= rr <= RR_MAX_MS:
                    rr_list.append(rr)
                    hr_list.append(hr)
        if len(rr_list) < 30:
            log.info("Сегмент слишком короткий (%d RR) — не записываю в историю", len(rr_list))
            return
        ended = datetime.now()
        duration = (ended - seg.start_time).total_seconds()
        arr = np.array(rr_list)
        artifacts_pct = 0.0  # уже отфильтровано на лету
        try:
            calc = HRVCalculator(hr_data=hr_list, rr_intervals=rr_list)
            metrics = calc.calculate_all_metrics()
            freq_valid = len(rr_list) >= 200 and duration >= 240
            axes = calc.calculate_axis_scores(metrics, freq_valid=freq_valid)
            overall = calc.calculate_overall_score(axes)
            state = calc.get_state_text(overall)
        except Exception:
            log.exception("Не удалось посчитать метрики для сегмента")
            return
        sid = hist.save_session(
            self.db,
            user_label=self.user_label,
            started_at=seg.start_time,
            ended_at=ended,
            source="pmd_ppi",
            csv_path=str(seg.path),
            n_rr_raw=seg.n,
            n_rr_clean=len(rr_list),
            artifacts_pct=artifacts_pct,
            duration_sec=duration,
            project_metrics=metrics,
            axis_scores=axes,
            overall_score=overall,
            state_text=state,
        )
        log.info("Сессия #%d сохранена в БД (%s, %d RR, %.1f мин)", sid, self.user_label, len(rr_list), duration / 60)

    def compute_metrics(self) -> dict:
        empty = {
            "hr": None, "rmssd_5min": None, "sdnn_5min": None, "artifacts_pct": None,
            "n_rr": 0, "window_sec": 0,
            "hr_label": "—", "rmssd_label": "—", "sdnn_label": "—", "art_label": "—",
            "verdict": "ждём данные...",
        }
        if not self.live_rr:
            return empty
        now_sec = self.live_rr[-1][0]
        # 5-мин скользящее окно (как раньше) — основное для непрерывного мониторинга
        window_data = [(t, rr) for (t, rr) in self.live_rr if now_sec - t <= 300.0]
        if len(window_data) < 5:
            window_data = list(self.live_rr)
        # 3-мин окно (Kubios-style) — для прямой сверки с Kubios App
        window_3min = [(t, rr) for (t, rr) in self.live_rr if now_sec - t <= 180.0]
        rr = np.array([r for (_t, r) in window_data], dtype=float)
        valid = (rr >= RR_MIN_MS) & (rr <= RR_MAX_MS)
        rr_v = rr[valid]
        artifacts_pct = float(100.0 * (1.0 - len(rr_v) / len(rr)))

        # Kubios artifact correction
        kubios_artifacts = 0
        if _NK_OK and len(rr_v) >= 30:
            try:
                peaks = np.cumsum(rr_v).astype(int)
                info, _peaks_corr = _nk.signal_fixpeaks(
                    peaks=peaks, sampling_rate=1000, iterative=True, method="kubios"
                )
                rr_clean_sec = info.get("rr")
                if rr_clean_sec is not None and len(rr_clean_sec) >= 10:
                    for k in ("ectopic", "extra", "missed", "longshort"):
                        v = info.get(k)
                        if isinstance(v, (list, np.ndarray)):
                            kubios_artifacts += len(v)
                    rr_v = np.asarray(rr_clean_sec, dtype=float) * 1000.0
            except Exception:
                pass

        # Дополнительный фильтр Berntson — соседние RR не должны различаться >25%
        # (стандарт для optical sensors; убирает остаточные missed/double beats после Kubios)
        if len(rr_v) > 2:
            keep_mask = np.ones(len(rr_v), dtype=bool)
            for i in range(1, len(rr_v)):
                if keep_mask[i - 1]:
                    rel_diff = abs(rr_v[i] - rr_v[i - 1]) / rr_v[i - 1]
                    if rel_diff > 0.25:  # >25% скачок = артефакт
                        keep_mask[i] = False
            removed_berntson = int(np.sum(~keep_mask))
            if removed_berntson > 0:
                rr_v = rr_v[keep_mask]
                kubios_artifacts += removed_berntson

        # Финальный процент артефактов
        if len(rr) > 0:
            total_removed = (len(rr) - len(rr_v))
            artifacts_pct = float(100.0 * total_removed / len(rr))

        # Лейблы по нормам (короткие, для UI)
        def label_artifacts(p):
            if p < 5: return "отлично"
            if p < 15: return "приемлемо"
            if p < 30: return "плохо"
            return "очень плохо"

        if len(rr_v) < 5:
            empty.update({
                "artifacts_pct": round(artifacts_pct, 1),
                "n_rr": int(len(rr)),
                "window_sec": int(now_sec - window_data[0][0]),
                "art_label": label_artifacts(artifacts_pct),
                "verdict": "слишком мало валидных RR в окне — посади датчик плотнее",
            })
            return empty

        hr = float(60000.0 / np.mean(rr_v))
        rmssd = float(np.sqrt(np.mean(np.diff(rr_v) ** 2)))  # RMSSD на разностях — детрендинг не нужен

        # SDNN — с откалиброванным детрендингом (calibration.json от calibrate.py)
        sdnn = _compute_sdnn_calibrated(rr_v)

        # Раз в 60 сек снимаем точку для таймсерии
        if now_sec - self.last_ts_update_sec >= 60.0:
            self.timeseries.append({
                "t": datetime.fromtimestamp(now_sec).strftime("%H:%M"),
                "hr": round(hr, 1),
                "rmssd": round(rmssd, 1),
                "sdnn": round(sdnn, 1),
            })
            self.last_ts_update_sec = now_sec

        def label_hr(v):
            if v < 50: return "низкий"
            if v < 100: return "норма"
            if v < 120: return "повышен"
            return "высокий"

        def label_rmssd(v):
            if v < 15: return "очень низкое"
            if v < 25: return "сниженное"
            if v < 50: return "норма"
            if v < 100: return "хорошее"
            return "очень высокое"

        def label_sdnn(v):
            if v < 20: return "низкое"
            if v < 50: return "сниженное"
            if v < 100: return "норма"
            if v < 200: return "повышенное"
            return "очень высокое"

        # Краткий вердикт одной строкой
        if artifacts_pct > 20:
            verdict = f"⚠ Шумно ({artifacts_pct:.0f}% артефактов) — числа ненадёжны, поправь посадку датчика"
        elif rmssd > 150 or sdnn > 200:
            verdict = "⚠ Подозрительно высокие RMSSD/SDNN — это остаточные артефакты, не реальная парасимпатика"
        else:
            if rmssd < 20:
                verdict = "Парасимпатика снижена — напряжение или усталость"
            elif rmssd < 50:
                verdict = "Норма для бодрствования"
            else:
                verdict = "Хорошее восстановление, высокая парасимпатика"

        # 7 осей и общий балл — только если артефактов не слишком много
        axes = None
        overall = None
        state = None
        picture = None
        recs: list[dict] = []
        project_metrics = None
        primary_metrics_full = []
        pns_idx = sns_idx = None
        pns_lab = sns_lab = None
        if artifacts_pct <= 20 and len(rr_v) >= 30:
            try:
                hr_from_rr = (60000.0 / rr_v).tolist()
                calc = HRVCalculator(hr_data=hr_from_rr, rr_intervals=rr_v.tolist())
                project_metrics = calc.calculate_all_metrics()
                # частотные метрики надёжны от ~5 мин записи
                freq_valid = (now_sec - window_data[0][0]) >= 240 and len(rr_v) >= 200
                axes_raw = calc.calculate_axis_scores(project_metrics, freq_valid=freq_valid)
                overall = calc.calculate_overall_score(axes_raw)
                state = calc.get_state_text(overall)
                axes = {code: (None if axes_raw.get(code) is None else int(axes_raw[code])) for code in AXIS_LABELS}
                picture = overall_picture(project_metrics, axes_raw)
                recs = biofeedback_recommendations(project_metrics, artifacts_pct=artifacts_pct)

                # Kubios-стиль PNS/SNS индексы
                _mr = project_metrics.get("mean_rr") or 0
                _rms = project_metrics.get("rmssd") or 0
                _sd1 = project_metrics.get("sd1") or (_rms / np.sqrt(2) if _rms else 0)
                _sd2 = project_metrics.get("sd2") or 0
                _si = project_metrics.get("stress_index") or 0
                if _mr and _rms and _sd1:
                    pns_idx = ki.compute_pns_index(_mr, _rms, _sd1)
                    pns_lab = ki.interpret_index(pns_idx, "pns")
                if _mr and _si and _sd1 and _sd2:
                    sns_idx = ki.compute_sns_index(_mr, _si, _sd1, _sd2)
                    sns_lab = ki.interpret_index(sns_idx, "sns")

                # Полный набор первичных метрик со шкалами и интерпретацией
                # Маппим имена project_metrics -> ключи INTERP_SPECS
                metric_value_map = {
                    "rmssd": project_metrics.get("rmssd"),
                    "sdnn": project_metrics.get("sdnn"),
                    "pnn50": project_metrics.get("pnn50"),
                    "mean_rr": project_metrics.get("mean_rr"),
                    "sd1": project_metrics.get("sd1"),
                    "sd2": project_metrics.get("sd2"),
                    "sd1_sd2": project_metrics.get("sd1_sd2_ratio"),
                    "stress_index": project_metrics.get("stress_index"),
                    "lf_hf_ratio": project_metrics.get("lf_hf_ratio") if freq_valid else None,
                    "lf_nu": project_metrics.get("lf_nu") if freq_valid else None,
                    "hf_nu": project_metrics.get("hf_nu") if freq_valid else None,
                    "lf_power": project_metrics.get("lf_power") if freq_valid else None,
                    "hf_power": project_metrics.get("hf_power") if freq_valid else None,
                    "total_power": project_metrics.get("total_power") if freq_valid else None,
                    "vlf": project_metrics.get("vlf_power") if freq_valid else None,
                    "csi": None,  # NeuroKit only
                    "cvi": None,
                    "dfa_alpha1": None,
                    "apen": None,
                    "sampen": None,
                    "pnn20": None,
                }
                for key in PRIMARY_METRICS:
                    spec = INTERP_SPECS.get(key)
                    if not spec:
                        continue
                    val = metric_value_map.get(key)
                    label, meaning = (None, None)
                    if val is not None and val == val:
                        label, meaning = spec.interpret(val)
                    primary_metrics_full.append({
                        "key": key,
                        "name": spec.name,
                        "unit": spec.unit,
                        "description": spec.description,
                        "applicability": spec.applicability,
                        "ranges": [{"lo": lo, "hi": hi, "label": lbl, "meaning": mean} for lo, hi, lbl, mean in spec.ranges],
                        "value": (round(float(val), 2) if (val is not None and val == val) else None),
                        "label": label,
                        "meaning": meaning,
                    })
            except Exception:
                log.exception("Ошибка в live-вычислениях")

        return {
            "hr": round(hr, 1),
            "rmssd_5min": round(rmssd, 1),
            "sdnn_5min": round(sdnn, 1),
            "artifacts_pct": round(artifacts_pct, 1),
            "n_rr": int(len(rr)),
            "window_sec": int(now_sec - window_data[0][0]),
            "hr_label": label_hr(hr),
            "rmssd_label": label_rmssd(rmssd),
            "sdnn_label": label_sdnn(sdnn),
            "art_label": label_artifacts(artifacts_pct),
            "verdict": verdict,
            "axes": axes,
            "overall_score": overall,
            "state_text": state,
            "quality_unreliable": artifacts_pct > 20,
            "picture": picture,
            "recommendations": recs,
            "timeseries": list(self.timeseries),
            "primary_full": primary_metrics_full,
            "bio_age": (round(float(project_metrics["biological_age"]), 1) if project_metrics and project_metrics.get("biological_age") else None),
            "pns_index": pns_idx,
            "sns_index": sns_idx,
            "pns_label": pns_lab,
            "sns_label": sns_lab,
            "kubios_style_3min": _compute_kubios_3min(window_3min),
        }


def _compute_kubios_3min(window: list) -> dict:
    """3-мин окно с теми же препроцессами что Kubios App — для прямой сверки."""
    out = {"hr": None, "rmssd": None, "sdnn": None, "sd1": None, "sd2": None,
           "mean_rr": None, "stress_index": None, "n_rr": 0, "window_sec": 0,
           "pns_index": None, "sns_index": None}
    if len(window) < 30:
        return out
    rr = np.array([r for (_t, r) in window], dtype=float)
    rr = rr[(rr >= RR_MIN_MS) & (rr <= RR_MAX_MS)]
    if len(rr) < 30:
        return out
    # Kubios fixpeaks
    if _NK_OK:
        try:
            peaks = np.cumsum(rr).astype(int)
            info, _ = _nk.signal_fixpeaks(peaks=peaks, sampling_rate=1000, iterative=True, method="kubios")
            rr_sec = info.get("rr")
            if rr_sec is not None and len(rr_sec) >= 20:
                rr = np.asarray(rr_sec, dtype=float) * 1000
        except Exception:
            pass
    # Berntson 25%
    if len(rr) > 2:
        keep = np.ones(len(rr), dtype=bool)
        for i in range(1, len(rr)):
            if keep[i - 1] and abs(rr[i] - rr[i - 1]) / rr[i - 1] > 0.25:
                keep[i] = False
        rr = rr[keep]
    if len(rr) < 20:
        return out
    out["n_rr"] = int(len(rr))
    out["window_sec"] = int(window[-1][0] - window[0][0])
    out["mean_rr"] = round(float(np.mean(rr)), 2)
    out["hr"] = round(60000.0 / float(np.mean(rr)), 1)
    out["rmssd"] = round(float(np.sqrt(np.mean(np.diff(rr) ** 2))), 1)
    # SDNN с откалиброванным детрендингом
    sdnn = _compute_sdnn_calibrated(rr)
    out["sdnn"] = round(sdnn, 1)
    # SD1 (≈ RMSSD/√2), SD2
    sd1 = float(np.sqrt(0.5) * np.std(np.diff(rr), ddof=1))
    sd2 = float(np.sqrt(2 * np.var(rr, ddof=1) - 0.5 * np.var(np.diff(rr), ddof=1)))
    out["sd1"] = round(sd1, 1)
    out["sd2"] = round(sd2, 1) if not np.isnan(sd2) else None
    # Stress Index (Baevsky)
    rr_sec_arr = rr / 1000.0
    bin_w = 0.05
    if rr_sec_arr.max() - rr_sec_arr.min() > 0:
        bins = np.arange(rr_sec_arr.min(), rr_sec_arr.max() + bin_w, bin_w)
        if len(bins) >= 2:
            hist, edges = np.histogram(rr_sec_arr, bins=bins)
            mode_idx = int(np.argmax(hist))
            mo = (edges[mode_idx] + edges[mode_idx + 1]) / 2
            amo = hist[mode_idx] / len(rr_sec_arr) * 100.0
            mxdmn = float(rr_sec_arr.max() - rr_sec_arr.min())
            if mo > 0 and mxdmn > 0:
                out["stress_index"] = round(float(amo / (2 * mo * mxdmn)), 2)
    # PNS/SNS индексы
    try:
        if out["sd1"]:
            out["pns_index"] = ki.compute_pns_index(out["mean_rr"], out["rmssd"], out["sd1"])
        if out["sd1"] and out["sd2"] and out["stress_index"]:
            out["sns_index"] = ki.compute_sns_index(out["mean_rr"], out["stress_index"], out["sd1"], out["sd2"])
    except Exception:
        pass
    return out


STATE = CollectorState()


# ---------- BLE-цикл ----------

async def find_polar_device():
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT_SEC)
    for d in devices:
        name = d.name or ""
        if any(marker in name for marker in DEVICE_NAME_MARKERS):
            return d
    return None


def parse_ppi_frame(buf: bytes):
    """Возвращает список (hr, ppi_ms) из PMD PPI-фрейма. Совместимо с pmd_connector.py."""
    if not buf:
        return []
    frame_type = None
    offset = None
    if len(buf) >= 9 and buf[0] in (0x00, 0x01):
        frame_type = buf[0]
        offset = 1 + 8
    elif len(buf) >= 10 and buf[9] in (0x00, 0x01):
        frame_type = buf[9]
        offset = 10
    elif len(buf) % 6 == 0:
        frame_type = 0x00
        offset = 0
    if frame_type != 0x00 or offset is None or offset >= len(buf):
        return []
    out = []
    for i in range(offset, len(buf), 6):
        if i + 5 >= len(buf):
            break
        hr = buf[i]
        ppi_ms = int.from_bytes(buf[i + 1:i + 3], "little", signed=False)
        err_ms = int.from_bytes(buf[i + 3:i + 5], "little", signed=False)
        if err_ms >= 30:
            continue
        if RR_MIN_MS <= ppi_ms <= RR_MAX_MS:
            out.append((float(hr), float(ppi_ms)))
    return out


def make_pmd_handler():
    def _handle(payload):
        try:
            meas, _ts, data = payload
            if meas != "PPI":
                return
            for hr, ppi_ms in parse_ppi_frame(bytes(data)):
                STATE.on_rr(rr_ms=ppi_ms, hr=hr)
        except Exception:
            log.exception("Ошибка в PMD handler")
    return _handle


async def stream_loop():
    """Главный BLE-цикл. Никогда не возвращается, пока не shutdown."""
    while not STATE.shutdown_event.is_set():
        device = None
        try:
            STATE.last_status = "сканирую BLE..."
            log.info("Сканирую BLE на наличие Polar...")
            device = await find_polar_device()
        except Exception:
            log.exception("Ошибка сканирования")

        if device is None:
            STATE.last_status = "датчик не найден, повторяю через 5 сек"
            try:
                await asyncio.wait_for(STATE.shutdown_event.wait(), timeout=SCAN_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
            continue

        log.info("Найден %s (%s) → подключаюсь", device.name, device.address)
        STATE.last_status = f"подключаюсь к {device.name}..."
        client = BleakClient(device.address)
        pmd = None
        try:
            await client.connect()
            STATE.connected = True
            STATE.device_name = device.name
            STATE.device_address = device.address
            STATE.last_seen = datetime.now()
            STATE.last_status = f"подключено: {device.name} — стрим PPI"
            log.info("Подключено. Стартую PPI...")

            pmd = PolarMeasurementData(client, callback=make_pmd_handler())
            await pmd.start_streaming("PPI")

            # Сидим пока соединение живое
            while client.is_connected and not STATE.shutdown_event.is_set():
                try:
                    await asyncio.wait_for(STATE.shutdown_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                STATE.last_seen = datetime.now()
        except Exception as e:
            log.warning("Ошибка подключения/стрима: %s", e)
        finally:
            STATE.connected = False
            STATE.last_status = "соединение потеряно, переподключаюсь..."
            try:
                if pmd is not None:
                    await pmd.stop_streaming("PPI")
            except Exception:
                pass
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass
            STATE.close_segment()

            if STATE.shutdown_event.is_set():
                break
            try:
                await asyncio.wait_for(STATE.shutdown_event.wait(), timeout=RECONNECT_BACKOFF_SEC)
            except asyncio.TimeoutError:
                pass


# ---------- Веб-морда ----------

app = FastAPI()
_STATIC_DIR = ROOT / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)


@app.get("/api/status")
async def api_status():
    metrics = STATE.compute_metrics()
    # Последние 100 RR для tahogram
    recent_rr = [round(rr, 1) for (_t, rr) in list(STATE.live_rr)[-100:]]
    # Мгновенные метрики по последним 20 RR
    instant = compute_instant_metrics(list(STATE.live_rr)[-30:])
    return JSONResponse({
        "user_settings": _load_profile(),
        "connected": STATE.connected,
        "device": {
            "name": STATE.device_name,
            "address": STATE.device_address,
        },
        "status_message": STATE.last_status,
        "last_seen": STATE.last_seen.strftime("%Y-%m-%d %H:%M:%S") if STATE.last_seen else None,
        "current_segment": STATE.current_segment.path.name if STATE.current_segment else None,
        "user_label": STATE.user_label,
        "users_known": hist.list_users(STATE.db),
        "metrics": metrics,
        "recent_rr": recent_rr,
        "instant": instant,
    })


def compute_instant_metrics(window):
    """Мгновенные метрики по последним ~30 RR (примерно 25 секунд). Для биофидбэка."""
    if len(window) < 5:
        return {"hr_instant": None, "rmssd_instant": None}
    rr = np.array([r for (_t, r) in window], dtype=float)
    rr = rr[(rr >= RR_MIN_MS) & (rr <= RR_MAX_MS)]
    if len(rr) < 5:
        return {"hr_instant": None, "rmssd_instant": None}
    hr_inst = float(60000.0 / np.mean(rr[-10:]))  # средний по последним 10 ударам
    rmssd_inst = float(np.sqrt(np.mean(np.diff(rr) ** 2)))
    return {"hr_instant": round(hr_inst, 1), "rmssd_instant": round(rmssd_inst, 1)}


_PROFILE_PATH = ROOT / "data" / "profile.json"


def _load_profile():
    if _PROFILE_PATH.exists():
        import json as _j
        return _j.loads(_PROFILE_PATH.read_text())
    return {}


def _save_profile(d):
    import json as _j
    _PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PROFILE_PATH.write_text(_j.dumps(d, ensure_ascii=False, indent=2))


# Подхватываем сохранённый возраст в env при старте
_p = _load_profile()
if _p.get("age"):
    os.environ["USER_AGE"] = str(_p["age"])


@app.post("/api/set_age")
async def api_set_age(body: dict = Body(...)):
    age = int(body.get("age", 0))
    if age < 15 or age > 95:
        raise HTTPException(400, "age должен быть 15–95")
    p = _load_profile()
    p["age"] = age
    _save_profile(p)
    os.environ["USER_AGE"] = str(age)
    return {"ok": True, "age": age}


@app.post("/api/set_user")
async def api_set_user(body: dict = Body(...)):
    label = (body.get("user_label") or "").strip()
    if not label:
        raise HTTPException(400, "user_label обязателен")
    if STATE.current_segment is not None:
        STATE.close_segment()  # текущий сегмент привязан к старому пользователю
    STATE.user_label = label
    log.info("Пользователь переключён: %s", label)
    return {"ok": True, "user_label": label}


@app.get("/api/history")
async def api_history(user: Optional[str] = None, days: int = 14):
    label = user or STATE.user_label
    sessions = hist.recent_sessions(STATE.db, user_label=label, days=days)
    daily = hist.daily_aggregate(STATE.db, user_label=label, days=days)
    last_prev = hist.last_session(STATE.db, user_label=label)
    return {
        "user_label": label,
        "sessions": sessions,
        "daily": daily,
        "last_session": last_prev,
    }


@app.get("/api/compare_to_last")
async def api_compare_to_last():
    """Сравнить текущие живые метрики с последней завершённой сессией."""
    m = STATE.compute_metrics()
    if m.get("hr") is None:
        return {"ok": False, "reason": "нет данных в текущем окне"}
    prev = hist.last_session(STATE.db, user_label=STATE.user_label)
    if not prev:
        return {"ok": False, "reason": "нет прошлых сессий для сравнения"}
    curr = {
        "rmssd": m.get("rmssd_5min"),
        "sdnn": m.get("sdnn_5min"),
        "mean_rr": (60000.0 / m["hr"]) if m.get("hr") else None,
        "stress_index": None,  # в live не считаем
        "lf_hf_ratio": None,
        "artifacts_pct": m.get("artifacts_pct"),
    }
    prev_short = {
        "rmssd": prev.get("rmssd"),
        "sdnn": prev.get("sdnn"),
        "mean_rr": prev.get("mean_rr"),
        "stress_index": prev.get("stress_index"),
        "lf_hf_ratio": prev.get("lf_hf_ratio"),
        "artifacts_pct": prev.get("artifacts_pct"),
    }
    hyps = hypotheses_for_change(prev=prev_short, curr=curr)
    return {
        "ok": True,
        "prev_started_at": prev.get("started_at"),
        "prev_state": prev.get("state_text"),
        "prev_overall": prev.get("overall_score"),
        "current": curr,
        "hypotheses": hyps,
    }


@app.get("/api/snapshot")
async def api_snapshot():
    """Снимок текущего состояния в стиле Kubios RESULT — для показа клиенту."""
    metrics = STATE.compute_metrics()
    k3 = metrics.get("kubios_style_3min") or {}
    if not k3.get("hr"):
        return HTMLResponse("<html><body style='font-family:sans-serif;padding:40px;text-align:center'><h2>⏳ Собираем данные…</h2><p>3-минутный снимок будет готов через 3 минуты после старта.</p></body></html>")

    # Readiness — берём общий балл если есть
    readiness = metrics.get("overall_score") or 50
    state = metrics.get("state_text") or "—"
    bio = metrics.get("bio_age")
    real_age = _load_profile().get("age")

    # Mood-маппинг по совокупности
    si = k3.get("stress_index") or 0
    pns = k3.get("pns_index") or 0
    sns = k3.get("sns_index") or 0
    if pns > 1.0 and sns < 0: mood = "Глубокое восстановление"; mood_emoji = "😌"
    elif pns > 0.3 and abs(sns) < 0.5: mood = "Сбалансированное состояние"; mood_emoji = "🙂"
    elif sns > 1.0 and si > 200: mood = "Острый стресс"; mood_emoji = "😰"
    elif sns > 0.3: mood = "Мобилизация, лёгкое напряжение"; mood_emoji = "😐"
    elif si < 30: mood = "Глубокий покой, ваготония"; mood_emoji = "😴"
    else: mood = "Норма"; mood_emoji = "🙂"

    # Цвет Readiness
    if readiness >= 80: rcolor = "#2e7d32"; rzone = "Отличная"
    elif readiness >= 65: rcolor = "#43a047"; rzone = "Хорошая"
    elif readiness >= 50: rcolor = "#fb8c00"; rzone = "Норма"
    else: rcolor = "#d32f2f"; rzone = "Низкая"

    age_line = ""
    if bio is not None and real_age:
        delta = bio - real_age
        delta_txt = "как реальный" if delta == 0 else (f"−{abs(int(delta))} лет к реальному" if delta < 0 else f"+{int(delta)} лет к реальному")
        age_line = f"<div class='kv'><div class='k'>Биологический возраст</div><div class='v'><b>{int(bio)}</b> <span class='ages'>({delta_txt}, реальный {real_age})</span></div></div>"

    pns_line = f"<div class='kv'><div class='k'>PNS Index (парасимпатика)</div><div class='v'><b>{pns:+.2f}</b></div></div>" if pns else ""
    sns_line = f"<div class='kv'><div class='k'>SNS Index (симпатика)</div><div class='v'><b>{sns:+.2f}</b></div></div>" if sns else ""

    html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>HRV — снимок</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 480px; margin: 0 auto; padding: 24px 20px; background: white; color: #1a1a1a; }}
  .hero {{ text-align: center; padding: 24px 0 16px; }}
  .readiness-circle {{ position: relative; width: 220px; height: 110px; margin: 0 auto 8px; overflow: hidden; }}
  .readiness-bg {{ width: 220px; height: 220px; border-radius: 50%; background: conic-gradient(from -90deg, {rcolor} {readiness * 1.8}deg, #e8eaed {readiness * 1.8}deg 180deg, transparent 180deg); position: absolute; top: 0; }}
  .readiness-mid {{ position: absolute; top: 14px; left: 14px; width: 192px; height: 192px; background: white; border-radius: 50%; }}
  .readiness-val {{ position: absolute; top: 22px; left: 0; right: 0; text-align: center; font-size: 56px; font-weight: 700; color: {rcolor}; line-height: 1; }}
  .readiness-pct {{ font-size: 30px; opacity: .8; }}
  .readiness-label {{ position: absolute; top: 78px; left: 0; right: 0; text-align: center; font-size: 12px; letter-spacing: .1em; color: #666; }}
  .mood {{ text-align: center; padding: 8px 12px; background: #f5f8ff; border-radius: 10px; margin: 16px 0; font-size: 15px; }}
  .mood-emoji {{ font-size: 28px; display: block; margin-bottom: 4px; }}
  .section-title {{ font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: #888; margin: 20px 0 10px; font-weight: 600; }}
  .kv {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #eef0f3; font-size: 14px; }}
  .kv:last-child {{ border-bottom: none; }}
  .kv .k {{ color: #555; }}
  .kv .v b {{ font-size: 18px; color: #1a1a1a; }}
  .kv .ages {{ color: #888; font-size: 11px; }}
  .footer {{ margin-top: 28px; font-size: 11px; color: #999; text-align: center; border-top: 1px solid #eef0f3; padding-top: 12px; }}
  .save-hint {{ background: #fff8e1; border-radius: 6px; padding: 10px 14px; font-size: 12px; color: #5a4400; margin: 16px 0; text-align: center; }}
</style></head><body>
  <div class="hero">
    <div class="readiness-circle">
      <div class="readiness-bg"></div>
      <div class="readiness-mid"></div>
      <div class="readiness-val">{int(readiness)}<span class="readiness-pct">/100</span></div>
      <div class="readiness-label">READINESS · {rzone.upper()}</div>
    </div>
    <div style="font-size:20px;font-weight:600;margin-top:8px">{state}</div>
  </div>

  <div class="mood">
    <span class="mood-emoji">{mood_emoji}</span>
    {mood}
  </div>

  <div class="section-title">Основные показатели</div>
  <div class="kv"><div class="k">Пульс</div><div class="v"><b>{k3.get('hr', '—')}</b> bpm</div></div>
  <div class="kv"><div class="k">RMSSD (восстановление)</div><div class="v"><b>{k3.get('rmssd', '—')}</b> мс</div></div>
  <div class="kv"><div class="k">SDNN (адаптивность)</div><div class="v"><b>{k3.get('sdnn', '—')}</b> мс</div></div>
  <div class="kv"><div class="k">Stress Index</div><div class="v"><b>{k3.get('stress_index', '—')}</b></div></div>
  {pns_line}{sns_line}
  {age_line}

  <div class="save-hint">📱 Сделай скриншот этой страницы — это снимок твоего состояния прямо сейчас.<br>
  За полную динамику дня — продолжай носить браслет, после сессии получишь детальный отчёт.</div>

  <div class="footer">Замер за последние {k3.get('window_sec', 0)} сек ({k3.get('n_rr', 0)} ударов) · {datetime.now().strftime('%H:%M %d.%m.%Y')}</div>
</body></html>"""
    return HTMLResponse(html)


@app.get("/api/download_current")
async def api_download_current():
    """Скачать CSV текущего сегмента (если идёт запись)."""
    if STATE.current_segment is None:
        raise HTTPException(404, "Сейчас нет активного сегмента")
    p = STATE.current_segment.path
    return FileResponse(p, media_type="text/csv", filename=p.name)


@app.get("/api/download_day")
async def api_download_day():
    """Слепить все сегодняшние сегменты в один CSV и отдать."""
    STATE.close_segment()
    day_dir = STATE.day_dir()
    segments = sorted(day_dir.glob("segment_*.csv"))
    if not segments:
        raise HTTPException(404, "Сегодня нет ни одного сегмента")
    merged_path = day_dir / "merged_day.csv"
    with merged_path.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(["Heart_Rate_bpm", "Timestamp_ISO", "RR_Interval_ms", "Second", "RR_Source", "Duration_Seconds"])
        idx = 0
        for seg in segments:
            with seg.open("r", encoding="utf-8") as src:
                reader = csv.reader(src)
                next(reader, None)
                for row in reader:
                    if len(row) >= 6:
                        row[3] = str(idx)
                        idx += 1
                        writer.writerow(row)
    return FileResponse(merged_path, media_type="text/csv", filename=merged_path.name)


@app.post("/finish_day")
async def finish_day():
    """Склеить все сегодняшние сегменты в один CSV и прогнать analyze_session.py."""
    STATE.close_segment()
    day_dir = STATE.day_dir()
    segments = sorted(day_dir.glob("segment_*.csv"))
    if not segments:
        raise HTTPException(404, "Сегодня ни одного сегмента не записано")
    merged_path = day_dir / "merged_day.csv"
    with merged_path.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(["Heart_Rate_bpm", "Timestamp_ISO", "RR_Interval_ms", "Second", "RR_Source", "Duration_Seconds"])
        idx = 0
        for seg in segments:
            with seg.open("r", encoding="utf-8") as src:
                reader = csv.reader(src)
                next(reader, None)
                for row in reader:
                    if len(row) >= 6:
                        row[3] = str(idx)
                        idx += 1
                        writer.writerow(row)
    log.info("Слеплен дневной CSV: %s (%d RR)", merged_path, idx)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "analyze_session.py"), str(merged_path)],
        cwd=str(ROOT),
    )
    return {"ok": True, "merged_csv": str(merged_path), "n_rr": idx, "analyzer_pid": proc.pid}


@app.post("/shutdown")
async def shutdown():
    STATE.shutdown_event.set()
    return {"ok": True}


INDEX_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>HRV — биофидбэк</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 720px; margin: 24px auto; padding: 0 16px; color: #1a1a1a; }
  h1 { font-weight: 600; margin: 0 0 4px; }
  .sub { color: #777; font-size: 13px; margin-bottom: 24px; }
  .status { padding: 12px 16px; border-radius: 8px; margin-bottom: 24px; font-size: 14px; }
  .ok { background: #e6f7e9; border: 1px solid #b8e3c0; }
  .wait { background: #fff8e6; border: 1px solid #f0d780; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
  .card { background: #f4f6f8; border-radius: 8px; padding: 14px; }
  .card .k { font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: .04em; }
  .card .v { font-size: 28px; font-weight: 600; margin-top: 4px; }
  .card .u { font-size: 12px; color: #888; margin-left: 4px; }
  .card .label { font-size: 12px; color: #444; margin-top: 4px; font-weight: 500; }
  .verdict-box { background: #eef5ff; border-left: 3px solid #1a73e8; padding: 12px 16px; margin: 20px 0; font-size: 14px; line-height: 1.5; border-radius: 6px; }
  .verdict-box.warn { background: #fff3e6; border-left-color: #f57c00; }
  .state-mini { background: #fafbfc; border: 1px solid #e3e6ea; border-radius: 10px; padding: 18px; text-align: center; margin-bottom: 16px; }
  .state-mini-score { font-size: 48px; font-weight: 700; line-height: 1; color: #1a73e8; }
  .state-mini-score .of { font-size: 20px; color: #999; font-weight: 400; }
  .state-mini-text { font-size: 16px; font-weight: 600; margin-top: 6px; }
  .axes-mini { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin-bottom: 16px; }
  .axes-mini .ax { background: #fafbfc; border: 1px solid #e8eaed; border-radius: 6px; padding: 8px 10px; }
  .axes-mini .ax-name { font-size: 11px; color: #555; }
  .axes-mini .ax-val { font-size: 20px; font-weight: 600; margin-top: 2px; }
  .axes-mini .ax-bar { height: 4px; background: #eaecef; border-radius: 3px; margin-top: 4px; overflow: hidden; }
  .axes-mini .ax-bar-fill { height: 100%; }
  .picture-box { background: #fafbfc; border: 1px solid #e3e6ea; border-radius: 8px; padding: 14px 18px; margin-bottom: 12px; font-size: 14px; line-height: 1.55; color: #1a1a1a; }
  .picture-label { font-size: 11px; color: #555; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 8px; font-weight: 600; }
  #recs_block { margin-bottom: 16px; }
  .rec-card { border-radius: 8px; padding: 10px 14px; margin-bottom: 6px; }
  .rec-card .text { font-size: 13px; line-height: 1.4; }
  .rec-card .why { font-size: 11px; color: #666; margin-top: 4px; font-style: italic; }
  .rec-good { background: #cce8d0; }
  .rec-info { background: #e6efff; }
  .rec-action { background: #fff3e0; }
  .rec-alert { background: #f5c6c6; }
  .ts-box { background: #fafbfc; border: 1px solid #e3e6ea; border-radius: 8px; padding: 12px 16px; margin-bottom: 16px; }
  .ts-legend { display: flex; gap: 16px; margin-top: 8px; font-size: 11px; color: #555; }
  .ts-leg-item { display: flex; align-items: center; gap: 6px; }
  .ts-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  /* Видимый overlay ошибок (чтобы видеть баги без открытия DevTools) */
  #err_overlay { position: fixed; bottom: 10px; right: 10px; max-width: 380px; max-height: 200px; overflow: auto; background: #b71c1c; color: white; padding: 10px 14px; border-radius: 8px; font-size: 12px; font-family: monospace; box-shadow: 0 4px 12px rgba(0,0,0,.3); z-index: 9999; display: none; line-height: 1.4; }
  #err_overlay .close { position: absolute; top: 4px; right: 8px; cursor: pointer; font-weight: bold; }
  .chart-fallback { padding: 12px; background: #fff8e1; border: 1px dashed #f9a825; border-radius: 6px; color: #5a4400; font-size: 12px; text-align: center; height: 100%; display: flex; align-items: center; justify-content: center; }

  /* ВКЛАДКИ */
  .tabs { display: flex; gap: 0; border-bottom: 2px solid #e3e6ea; margin: 16px 0 20px; }
  .tab { padding: 12px 22px; cursor: pointer; font-size: 14px; font-weight: 500; color: #666; border-bottom: 3px solid transparent; margin-bottom: -2px; }
  .tab:hover { color: #1a73e8; }
  .tab.active { color: #1a73e8; border-bottom-color: #1a73e8; font-weight: 600; }
  .tab-pane { display: none; }
  .tab-pane.active { display: block; }

  /* БОЛЬШИЕ LIVE-ПЛИТКИ */
  .live-big { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 18px; }
  .live-tile { background: white; border: 2px solid #e3e6ea; border-radius: 12px; padding: 16px 20px; text-align: center; transition: border-color .25s; }
  .live-tile.changed { border-color: #1a73e8; }
  .live-tile .lk { font-size: 11px; color: #555; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 4px; }
  .live-tile .lv { font-size: 48px; font-weight: 700; line-height: 1; color: #1a1a1a; font-variant-numeric: tabular-nums; }
  .live-tile .lu { font-size: 14px; color: #888; margin-left: 4px; font-weight: 400; }
  .live-tile .ld { font-size: 12px; color: #444; margin-top: 6px; }

  .tachogram-box { background: white; border: 1px solid #e3e6ea; border-radius: 10px; padding: 14px 18px; margin-bottom: 18px; }
  .tacho-title { font-size: 13px; color: #444; margin-bottom: 8px; font-weight: 500; }
  .tacho-hint { font-size: 11px; color: #888; margin-top: 6px; line-height: 1.4; }
  /* Жёстко фиксируем размер canvas — иначе Chart.js уходит в бесконечный resize */
  .chart-wrap { position: relative; height: 160px; width: 100%; }
  .chart-wrap canvas { max-height: 160px !important; max-width: 100% !important; }
  .ts-box .chart-wrap { height: 200px; }
  .ts-box .chart-wrap canvas { max-height: 200px !important; }

  .breath-box { background: linear-gradient(135deg, #e3f2fd 0%, #f0f7ff 100%); border: 1px solid #b8d4f4; border-radius: 10px; padding: 16px 20px; margin-bottom: 16px; }
  .breath-title { font-size: 13px; color: #1a4578; font-weight: 600; margin-bottom: 12px; }
  .breath-circle-wrap { display: flex; align-items: center; justify-content: center; height: 140px; }
  .breath-circle { width: 90px; height: 90px; border-radius: 50%; background: rgba(26,115,232,.25); transition: all 4s ease-in-out; display: flex; align-items: center; justify-content: center; color: #1a4578; font-weight: 600; font-size: 14px; }
  .breath-circle.inhale { width: 140px; height: 140px; background: rgba(26,115,232,.5); transition-duration: 4s; }
  .breath-hint { font-size: 12px; color: #1a4578; text-align: center; margin-top: 8px; }

  .caveat-box { background: #fff8e1; border-left: 3px solid #f9a825; border-radius: 4px; padding: 10px 14px; margin: 12px 0; font-size: 12px; color: #5a4400; line-height: 1.5; }

  /* ВКЛАДКИ — более заметные */
  .tabs { background: #f0f3f6; border-radius: 10px; padding: 4px; border-bottom: none; gap: 0; }
  .tab { padding: 12px 22px; border-radius: 8px; border-bottom: none; margin: 0; }
  .tab.active { background: white; color: #1a73e8; box-shadow: 0 1px 3px rgba(0,0,0,.08); border-bottom: none; }

  /* HERO BLOCK для главного показателя HRV */
  .hrv-hero { background: linear-gradient(135deg, #1a73e8 0%, #0a4ea3 100%); color: white; border-radius: 14px; padding: 24px 28px; margin-bottom: 20px; box-shadow: 0 4px 12px rgba(26,115,232,.18); }
  .hrv-hero-label { font-size: 12px; text-transform: uppercase; letter-spacing: .08em; opacity: .8; margin-bottom: 14px; font-weight: 500; }
  .hrv-hero-row { display: grid; grid-template-columns: 1fr 1.4fr; gap: 24px; align-items: center; }
  @media (max-width: 640px) { .hrv-hero-row { grid-template-columns: 1fr; } }
  .hrv-hero-main { text-align: left; }
  .hrv-hero-value { font-size: 72px; font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums; }
  .hrv-hero-unit { font-size: 22px; opacity: .65; margin-left: 6px; font-weight: 400; }
  .hrv-hero-sub { font-size: 12px; opacity: .8; margin-top: 6px; }
  .hrv-hero-row2 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 12px; }
  @media (max-width: 640px) { .hrv-hero-row2 { grid-template-columns: 1fr 1fr; } }
  .hrv-hero-mini-label { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; opacity: .7; margin-bottom: 2px; }
  .hrv-hero-mini { font-size: 20px; font-weight: 600; }
  .hrv-hero-zone { background: rgba(255,255,255,.18); border-radius: 6px; padding: 8px 12px; font-size: 13px; line-height: 1.4; }
  .k3-box { background: #fafbfc; border: 1px solid #d8dde3; border-radius: 10px; padding: 14px 18px; margin-bottom: 16px; }
  .k3-title { font-size: 12px; font-weight: 600; color: #1a4578; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 10px; }
  .k3-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 12px; }
  .k3-k { font-size: 10px; color: #777; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 2px; }
  .k3-v { font-size: 17px; font-weight: 600; color: #1a1a1a; font-variant-numeric: tabular-nums; }
  /* Светофорные цвета фона hero */
  .hrv-hero.zone-good { background: linear-gradient(135deg, #2e7d32 0%, #1b5e20 100%); box-shadow: 0 4px 12px rgba(46,125,50,.25); }
  .hrv-hero.zone-warn { background: linear-gradient(135deg, #f57c00 0%, #e65100 100%); box-shadow: 0 4px 12px rgba(245,124,0,.25); }
  .hrv-hero.zone-alert { background: linear-gradient(135deg, #d32f2f 0%, #b71c1c 100%); box-shadow: 0 4px 12px rgba(211,47,47,.25); }
  .hrv-hero.zone-info { background: linear-gradient(135deg, #1a73e8 0%, #0a4ea3 100%); }

  /* Bullet-charts на live-странице (для всех показателей справочника) */
  .legend-mini { display: flex; gap: 12px; flex-wrap: wrap; padding: 10px 14px; background: #fafbfc; border-radius: 6px; margin-bottom: 14px; font-size: 11px; color: #555; }
  .leg-mini { display: flex; align-items: center; gap: 5px; }
  .leg-mini .sw { width: 16px; height: 10px; border-radius: 2px; display: inline-block; }
  .leg-mini .mark { width: 3px; height: 14px; background: #0a4ea3; box-shadow: 0 0 0 1px white, 0 0 2px rgba(0,0,0,.3); }
  .bm-row { background: white; border: 1px solid #e8eaed; border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; }
  .bm-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px; }
  .bm-name { font-size: 13px; font-weight: 600; color: #1a1a1a; }
  .bm-val { font-size: 16px; font-weight: 700; color: #0a4ea3; font-variant-numeric: tabular-nums; }
  .bm-val .u { font-size: 10px; color: #888; font-weight: 400; margin-left: 2px; }
  .bm-desc { font-size: 11px; color: #555; margin-bottom: 6px; line-height: 1.4; }
  .bm-bar { position: relative; height: 14px; background: #f0f2f4; border-radius: 3px; overflow: visible; margin-bottom: 4px; }
  .bm-bar .seg { position: absolute; top: 0; height: 100%; }
  .bm-bar .mark { position: absolute; top: -3px; bottom: -3px; width: 3px; background: #0a4ea3; border-radius: 2px; box-shadow: 0 0 0 1px white, 0 0 3px rgba(0,0,0,.3); transform: translateX(-1.5px); z-index: 2; }
  .bm-label { font-size: 11px; color: #444; margin-top: 4px; }
  .bm-label b { color: #0a4ea3; }
  .bm-app { font-size: 11px; color: #333; margin-top: 4px; padding: 5px 8px; background: #f5f8ff; border-left: 2px solid #1a73e8; border-radius: 0 3px 3px 0; line-height: 1.4; }
  details summary::-webkit-details-marker { color: #1a73e8; }
  .compare-box { transition: background .3s, border-color .3s; }
  .help { background: #fafbfc; border-radius: 6px; padding: 12px 16px; margin-top: 24px; font-size: 12px; color: #555; line-height: 1.6; }
  .help b { color: #222; }
  button { background: #1a73e8; color: white; border: 0; padding: 10px 16px; border-radius: 6px; font-size: 14px; cursor: pointer; }
  button:hover { background: #155bb5; }
  button.ghost { background: white; color: #1a73e8; border: 1px solid #1a73e8; }
  button.ghost:hover { background: #f0f6ff; }
  .action-row { display: flex; gap: 8px; margin-top: 24px; flex-wrap: wrap; }
  .user-row { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; font-size: 13px; color: #555; }
  .user-row input { padding: 4px 8px; font-size: 13px; border: 1px solid #d0d4d8; border-radius: 4px; width: 120px; }
  .user-row button { padding: 4px 10px; font-size: 12px; }
  .user-row #user_known { color: #999; font-size: 11px; }
  .compare-box { background: #fafbfc; border: 1px solid #e3e6ea; border-radius: 8px; padding: 12px 16px; margin: 16px 0; font-size: 13px; }
  .compare-title { font-weight: 600; margin-bottom: 8px; }
  .compare-box ul { margin: 0; padding-left: 20px; line-height: 1.6; color: #444; }
  .footer { margin-top: 32px; color: #999; font-size: 12px; }
  table.hist { width: 100%; border-collapse: collapse; }
  table.hist th, table.hist td { border-bottom: 1px solid #eef0f3; padding: 5px 8px; text-align: left; }
  table.hist th { background: #f5f7f9; font-weight: 500; color: #666; font-size: 11px; }
</style>
</head>
<body>
  <div id="err_overlay"><span class="close" onclick="this.parentElement.style.display='none'">×</span><div id="err_body"></div></div>
  <script>
    // Перехват всех ошибок и показ на странице (вместо тихого падения)
    function showErr(msg) {
      const ov = document.getElementById('err_overlay');
      const body = document.getElementById('err_body');
      if (!ov || !body) return;
      const t = new Date().toLocaleTimeString();
      body.innerHTML = ('[' + t + '] ' + msg + '<br>') + body.innerHTML;
      ov.style.display = 'block';
    }
    window.addEventListener('error', e => {
      showErr('JS ERROR: ' + (e.message || e.error || 'unknown') + ' @ ' + (e.filename || '?') + ':' + (e.lineno || '?'));
    });
    window.addEventListener('unhandledrejection', e => {
      showErr('PROMISE REJECTED: ' + (e.reason && e.reason.message || e.reason));
    });
  </script>
  <script src="/static/chart.umd.min.js" onerror="showErr('Chart.js НЕ загрузился (/static/chart.umd.min.js)')"></script>
  <script>
    if (typeof Chart === 'undefined') {
      showErr('Chart.js не определён после загрузки — графики работать не будут.');
    }
  </script>
  <h1>HRV — биофидбэк</h1>
  <div class="sub">Слева — что происходит прямо сейчас, справа — что в среднем за сессию</div>

  <div class="user-row">
    <label>Сейчас на руке: <input id="user_input" type="text" value="я" /></label>
    <button onclick="setUser()">Сохранить</button>
    <span id="user_known"></span>
  </div>

  <div id="status" class="status wait">загружаю...</div>

  <div class="tabs">
    <div class="tab active" data-tab="live" onclick="switchTab('live')">⚡ Live — здесь и сейчас</div>
    <div class="tab" data-tab="summary" onclick="switchTab('summary')">📊 Сводка по сессии</div>
  </div>

  <!-- ВКЛАДКА LIVE -->
  <div id="tab-live" class="tab-pane active">
    <div class="caveat-box">
      <b>Это «здесь и сейчас».</b> Числа считаются по последним 15–30 ударам и обновляются каждые 2 секунды.
      Помаши рукой, задержи дыхание, поприседай — увидишь как реагирует.
    </div>

    <div class="live-big">
      <div class="live-tile" id="hr_tile"><div class="lk">Пульс</div><div class="lv"><span id="hr_instant">—</span><span class="lu">bpm</span></div><div class="ld" id="hr_delta"></div></div>
      <div class="live-tile" id="rmssd_tile"><div class="lk">RMSSD мгновенно</div><div class="lv"><span id="rmssd_instant">—</span><span class="lu">мс</span></div><div class="ld" id="rmssd_delta"></div></div>
    </div>

    <div class="tachogram-box">
      <div class="tacho-title">RR-интервалы (последние 100 ударов)</div>
      <div class="chart-wrap"><canvas id="tacho_chart"></canvas></div>
      <div class="tacho-hint">Каждая точка — один удар сердца. Скачок вверх = удлинённый интервал (расслабление). Скачок вниз = укороченный (активация). Зубцы вверх-вниз = хорошая вариабельность.</div>
    </div>

    <div class="breath-box">
      <div class="breath-title">Дыхательный таймер (6 циклов/мин — резонансное дыхание)</div>
      <div class="breath-circle-wrap"><div class="breath-circle" id="breath_circle">дыши</div></div>
      <div class="breath-hint">Вдох на расширение, выдох на сжатие. 4 сек вдох / 6 сек выдох. Через 2–3 минуты увидишь рост RMSSD и снижение LF/HF.</div>
    </div>
  </div>

  <!-- ВКЛАДКА СВОДКА -->
  <div id="tab-summary" class="tab-pane">

    <!-- ГЛАВНЫЙ ПОКАЗАТЕЛЬ — ВАРИАБЕЛЬНОСТЬ -->
    <div class="hrv-hero">
      <div class="hrv-hero-label">Вариабельность сердечного ритма</div>
      <div class="hrv-hero-row">
        <div class="hrv-hero-main">
          <div class="hrv-hero-value"><span id="hero_sdnn">—</span><span class="hrv-hero-unit">мс</span></div>
          <div class="hrv-hero-sub">SDNN — общая вариабельность за 5 минут</div>
        </div>
        <div class="hrv-hero-side">
          <div class="hrv-hero-row2">
            <div><div class="hrv-hero-mini-label">RMSSD (парасимп.)</div><div class="hrv-hero-mini"><span id="hero_rmssd">—</span> мс</div></div>
            <div><div class="hrv-hero-mini-label">Пульс</div><div class="hrv-hero-mini"><span id="hero_hr">—</span> bpm</div></div>
            <div><div class="hrv-hero-mini-label">Биовозраст</div><div class="hrv-hero-mini"><span id="hero_age">—</span> <span style="font-size:11px;opacity:.7">лет</span></div></div>
            <div><div class="hrv-hero-mini-label">Качество</div><div class="hrv-hero-mini"><span id="hero_art">—</span>% арт.</div></div>
          </div>
          <div class="hrv-hero-row2" style="margin-top:8px">
            <div style="grid-column: span 2">
              <div class="hrv-hero-mini-label">PNS Index (парасимпатика, Kubios)</div>
              <div class="hrv-hero-mini"><span id="hero_pns">—</span></div>
              <div style="font-size:11px;opacity:.85;margin-top:2px" id="hero_pns_lab">—</div>
            </div>
            <div style="grid-column: span 2">
              <div class="hrv-hero-mini-label">SNS Index (симпатика, Kubios)</div>
              <div class="hrv-hero-mini"><span id="hero_sns">—</span></div>
              <div style="font-size:11px;opacity:.85;margin-top:2px" id="hero_sns_lab">—</div>
            </div>
          </div>
          <div class="hrv-hero-zone" id="hero_zone">собираем данные…</div>
        </div>
      </div>
    </div>

    <div class="k3-box">
      <div class="k3-title">📊 3-мин окно (Kubios-style — для прямой сверки с Kubios HRV App)</div>
      <div class="k3-grid">
        <div><div class="k3-k">HR</div><div class="k3-v"><span id="k3_hr">—</span> bpm</div></div>
        <div><div class="k3-k">SDNN</div><div class="k3-v"><span id="k3_sdnn">—</span> мс</div></div>
        <div><div class="k3-k">RMSSD</div><div class="k3-v"><span id="k3_rmssd">—</span> мс</div></div>
        <div><div class="k3-k">SD1</div><div class="k3-v"><span id="k3_sd1">—</span> мс</div></div>
        <div><div class="k3-k">SD2</div><div class="k3-v"><span id="k3_sd2">—</span> мс</div></div>
        <div><div class="k3-k">Stress Index</div><div class="k3-v"><span id="k3_si">—</span></div></div>
        <div><div class="k3-k">PNS</div><div class="k3-v"><span id="k3_pns">—</span></div></div>
        <div><div class="k3-k">SNS</div><div class="k3-v"><span id="k3_sns">—</span></div></div>
        <div><div class="k3-k">RR в окне</div><div class="k3-v"><span id="k3_n">—</span></div></div>
      </div>
    </div>

    <div id="compare_block" class="compare-box" style="display:none">
      <div class="compare-title">Сравнение с прошлой сессией</div>
      <div id="compare_body"></div>
    </div>

    <div id="picture_card" class="picture-box" style="display:none">
      <div class="picture-label">Что сейчас по совокупности метрик</div>
      <div id="picture_card_text">—</div>
    </div>

    <div id="recs_card" style="display:none">
      <div class="picture-label">Что можно попробовать сейчас</div>
      <div id="recs_card_body"></div>
    </div>

    <details open style="margin-top:20px">
      <summary style="cursor:pointer;font-weight:600;font-size:15px;color:#1a73e8;padding:10px 0">Все показатели по справочнику — шкалы с зонами норм</summary>
      <div class="legend-mini">
        <span class="leg-mini"><span class="sw" style="background:#a8d5ad"></span>Норма</span>
        <span class="leg-mini"><span class="sw" style="background:#ffd699"></span>Отклонение</span>
        <span class="leg-mini"><span class="sw" style="background:#ffd2a8"></span>Симпатика</span>
        <span class="leg-mini"><span class="sw" style="background:#c4d8f0"></span>Парасимпатика</span>
        <span class="leg-mini"><span class="sw" style="background:#f4a8a8"></span>Критическое</span>
        <span class="leg-mini"><span class="mark"></span>Твоё значение</span>
      </div>
      <div id="full_metrics_list">Собираем данные…</div>
    </details>

  <div id="state_block" class="state-mini" style="display:none">
    <div class="state-mini-score"><span id="overall">—</span><span class="of">/100</span></div>
    <div class="state-mini-text" id="state_text">—</div>
  </div>

  <div id="axes_block" class="axes-mini" style="display:none"></div>

  <div id="picture_block" class="picture-box" style="display:none">
    <div class="picture-label">Что сейчас по совокупности метрик</div>
    <div id="picture_text">—</div>
  </div>

  <div id="recs_block" style="display:none">
    <div class="picture-label">Что можно попробовать сейчас</div>
    <div id="recs_body"></div>
  </div>

  <div id="ts_block" class="ts-box" style="display:none">
    <div class="picture-label">Динамика по минутам (последние 30)</div>
    <div class="chart-wrap"><canvas id="ts_chart"></canvas></div>
    <div class="ts-legend">
      <span class="ts-leg-item"><span class="ts-dot" style="background:#1a73e8"></span>HR (bpm)</span>
      <span class="ts-leg-item"><span class="ts-dot" style="background:#2e7d32"></span>RMSSD (мс)</span>
      <span class="ts-leg-item"><span class="ts-dot" style="background:#e65100"></span>SDNN (мс)</span>
    </div>
  </div>

  <div id="verdict" class="verdict-box">—</div>

  <div class="grid">
    <div class="card">
      <div class="k">HR</div>
      <div class="v"><span id="hr">—</span><span class="u">bpm</span></div>
      <div class="label" id="hr_label">—</div>
    </div>
    <div class="card">
      <div class="k">RMSSD</div>
      <div class="v"><span id="rmssd">—</span><span class="u">мс</span></div>
      <div class="label" id="rmssd_label">—</div>
    </div>
    <div class="card">
      <div class="k">SDNN</div>
      <div class="v"><span id="sdnn">—</span><span class="u">мс</span></div>
      <div class="label" id="sdnn_label">—</div>
    </div>
    <div class="card">
      <div class="k">Артефакты</div>
      <div class="v"><span id="art">—</span><span class="u">%</span></div>
      <div class="label" id="art_label">—</div>
    </div>
    <div class="card">
      <div class="k">RR в окне</div>
      <div class="v"><span id="n">—</span></div>
      <div class="label">за последние 5 мин</div>
    </div>
    <div class="card">
      <div class="k">Сегмент</div>
      <div class="v" style="font-size:14px;"><span id="seg">—</span></div>
      <div class="label">текущая запись</div>
    </div>
  </div>

  <div class="caveat-box" style="background:#fff8e1">
    <b>Это твоё СОСТОЯНИЕ сейчас по последним 5 минутам</b>, а не постоянная характеристика личности.
    Баллы естественно колеблются на ±10–20% от окна к окну — это нормально.
    Личной характеристикой становится <i>среднее за несколько дней</i> (см. историю внизу).
  </div>

  <div class="action-row">
    <button onclick="window.open('/api/snapshot')">📸 Снимок для клиента</button>
    <button onclick="finishDay()" class="ghost">Завершить день → отчёт</button>
    <button onclick="window.open('/api/download_current')" class="ghost">Скачать CSV</button>
    <button onclick="window.open('/api/download_day')" class="ghost">Скачать день</button>
    <button onclick="loadCompare()" class="ghost">Сравнить с прошлой</button>
  </div>

  <details style="margin-top:24px">
    <summary style="cursor:pointer;font-weight:500">История по этому пользователю (14 дней)</summary>
    <div id="history_table" style="margin-top:12px;font-size:12px;color:#444">загружаю...</div>
  </details>

  </div> <!-- /tab-summary -->
  <!-- остаток скрипта остаётся как было -->

  <div class="help">
    <p><b>Что значат метрики:</b></p>
    <p><b>HR</b> — частота пульса. Норма покоя 50–100.<br>
    <b>RMSSD</b> — главный маркер парасимпатики (восстановление). Норма 25–50. Ниже 15 — острый стресс. Выше 100 — либо глубокая релаксация, либо артефакты.<br>
    <b>SDNN</b> — общая вариабельность. Норма 50+ за 5 мин. Чем выше, тем гибче регуляция.<br>
    <b>Артефакты</b> — % выбросов сигнала. До 5% — отлично. Выше 15% — двигаешься или датчик плохо лежит.</p>
    <p><b>Если значения «слишком хорошие»</b> (RMSSD &gt; 150, SDNN &gt; 200) — это не парасимпатика, это <i>остаточный шум после очистки</i>. Поправь посадку датчика.</p>
  </div>

  <div class="footer">localhost:8765 · обновление каждые 2 сек</div>

<script>
let knownUsers = [];
let lastInstantHR = null;
let lastInstantRMSSD = null;

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
}

// Дыхательный таймер 4 сек вдох / 6 сек выдох
(function breathLoop() {
  const c = document.getElementById('breath_circle');
  if (!c) return;
  let phase = 'inhale';
  setInterval(() => {
    phase = phase === 'inhale' ? 'exhale' : 'inhale';
    c.classList.toggle('inhale', phase === 'inhale');
    c.textContent = phase === 'inhale' ? 'вдох' : 'выдох';
  }, 5000);
})();

async function tick() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    knownUsers = d.users_known || [];
    document.getElementById('user_input').value = d.user_label || 'я';
    document.getElementById('user_known').textContent = knownUsers.length > 1 ? ('известны: ' + knownUsers.join(', ')) : '';
    const s = document.getElementById('status');
    if (d.connected) {
      s.className = 'status ok';
      s.textContent = '● подключено: ' + (d.device.name || '?') + ' — ' + d.status_message;
    } else {
      s.className = 'status wait';
      s.textContent = '○ ' + d.status_message;
    }
    const m = d.metrics || {};
    document.getElementById('hr').textContent = m.hr ?? '—';
    document.getElementById('rmssd').textContent = m.rmssd_5min ?? '—';
    document.getElementById('sdnn').textContent = m.sdnn_5min ?? '—';
    document.getElementById('art').textContent = m.artifacts_pct ?? '—';
    document.getElementById('n').textContent = m.n_rr ?? '—';
    document.getElementById('seg').textContent = d.current_segment ?? 'нет';
    document.getElementById('hr_label').textContent = m.hr_label ?? '—';
    document.getElementById('rmssd_label').textContent = m.rmssd_label ?? '—';
    document.getElementById('sdnn_label').textContent = m.sdnn_label ?? '—';
    document.getElementById('art_label').textContent = m.art_label ?? '—';
    const v = document.getElementById('verdict');
    v.textContent = m.verdict ?? '—';
    v.className = 'verdict-box' + ((m.verdict ?? '').startsWith('⚠') ? ' warn' : '');

    const stateBlock = document.getElementById('state_block');
    const axesBlock = document.getElementById('axes_block');
    if (m.axes && m.overall_score !== null && m.overall_score !== undefined) {
      stateBlock.style.display = 'block';
      document.getElementById('overall').textContent = m.overall_score;
      document.getElementById('state_text').textContent = m.state_text ?? '';
      const labels = {RD:'Готовность',SR:'Стрессоустойчивость',AD:'Адаптивность',FL:'Гибкость',RC:'Восстановление',EN:'Выносливость',BL:'Баланс'};
      const codes = ['RD','SR','AD','FL','RC','EN','BL'];
      axesBlock.innerHTML = codes.map(c => {
        const val = m.axes[c];
        const display = (val === null || val === undefined) ? 'н/д' : (val + '%');
        const pct = (val === null || val === undefined) ? 0 : val;
        const color = pct >= 60 ? '#1a73e8' : (pct >= 40 ? '#f57c00' : '#d32f2f');
        return `<div class="ax"><div class="ax-name">${labels[c]}</div><div class="ax-val">${display}</div><div class="ax-bar"><div class="ax-bar-fill" style="width:${pct}%;background:${color}"></div></div></div>`;
      }).join('');
      axesBlock.style.display = 'grid';
    } else {
      stateBlock.style.display = 'none';
      axesBlock.style.display = 'none';
    }

    // Комплексная картина
    const picBlock = document.getElementById('picture_block');
    if (m.picture) {
      document.getElementById('picture_text').textContent = m.picture;
      picBlock.style.display = 'block';
    } else {
      picBlock.style.display = 'none';
    }

    // Рекомендации биофидбэка
    const recsBlock = document.getElementById('recs_block');
    const recsBody = document.getElementById('recs_body');
    if (m.recommendations && m.recommendations.length > 0) {
      const icon = {good:'✓', info:'ℹ', action:'→', alert:'⚠'};
      recsBody.innerHTML = m.recommendations.map(r =>
        `<div class="rec-card rec-${r.level||'info'}"><div class="text"><b>${icon[r.level]||'•'}</b> ${r.text}</div><div class="why">${r.why}</div></div>`
      ).join('');
      recsBlock.style.display = 'block';
    } else {
      recsBlock.style.display = 'none';
    }

    // Таймсерия HR/RMSSD/SDNN (на вкладке сводка)
    if (m.timeseries && m.timeseries.length >= 2) {
      document.getElementById('ts_block').style.display = 'block';
      if (typeof Chart !== 'undefined') {
        drawTimeseries(m.timeseries);
      } else {
        const wrap = document.querySelector('#ts_block .chart-wrap');
        if (wrap && !wrap.querySelector('.chart-fallback')) {
          const last = m.timeseries[m.timeseries.length - 1];
          wrap.innerHTML = '<div class="chart-fallback">⚠ График недоступен. Последняя точка: HR ' + last.hr + ', RMSSD ' + last.rmssd + ', SDNN ' + last.sdnn + '</div>';
        }
      }
    }

    // LIVE-вкладка: instant метрики + тахограмма
    const inst = d.instant || {};
    if (inst.hr_instant !== null && inst.hr_instant !== undefined) {
      const hrEl = document.getElementById('hr_instant');
      const hrTile = document.getElementById('hr_tile');
      const prevHR = lastInstantHR;
      hrEl.textContent = inst.hr_instant;
      if (prevHR !== null && Math.abs(inst.hr_instant - prevHR) >= 1) {
        const delta = inst.hr_instant - prevHR;
        const arrow = delta > 0 ? '↑' : '↓';
        const color = delta > 0 ? '#d32f2f' : '#2e7d32';
        document.getElementById('hr_delta').innerHTML = `<span style="color:${color};font-weight:600">${arrow} ${Math.abs(delta).toFixed(1)}</span> за последние секунды`;
        hrTile.classList.add('changed');
        setTimeout(() => hrTile.classList.remove('changed'), 600);
      }
      lastInstantHR = inst.hr_instant;
    }
    if (inst.rmssd_instant !== null && inst.rmssd_instant !== undefined) {
      const rmssdEl = document.getElementById('rmssd_instant');
      const tile = document.getElementById('rmssd_tile');
      const prev = lastInstantRMSSD;
      rmssdEl.textContent = inst.rmssd_instant;
      if (prev !== null && Math.abs(inst.rmssd_instant - prev) >= 3) {
        const delta = inst.rmssd_instant - prev;
        const arrow = delta > 0 ? '↑' : '↓';
        const color = delta > 0 ? '#2e7d32' : '#d32f2f';
        document.getElementById('rmssd_delta').innerHTML = `<span style="color:${color};font-weight:600">${arrow} ${Math.abs(delta).toFixed(1)} мс</span> — реагирует на состояние`;
        tile.classList.add('changed');
        setTimeout(() => tile.classList.remove('changed'), 600);
      }
      lastInstantRMSSD = inst.rmssd_instant;
    }
    if (d.recent_rr && d.recent_rr.length >= 5) drawTachogram(d.recent_rr);

    // HERO-блок «Вариабельность» во вкладке Сводка
    const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = (v === null || v === undefined) ? '—' : v; };
    setText('hero_sdnn', m.sdnn_5min);
    setText('hero_rmssd', m.rmssd_5min);
    setText('hero_hr', m.hr);
    setText('hero_art', m.artifacts_pct);
    setText('hero_pns', m.pns_index);
    setText('hero_sns', m.sns_index);
    const pnsLab = document.getElementById('hero_pns_lab');
    const snsLab = document.getElementById('hero_sns_lab');
    if (pnsLab) pnsLab.textContent = m.pns_label ? (m.pns_label[0] + ' — ' + m.pns_label[1]) : '—';
    if (snsLab) snsLab.textContent = m.sns_label ? (m.sns_label[0] + ' — ' + m.sns_label[1]) : '—';
    const ageEl = document.getElementById('hero_age');
    if (ageEl) {
      if (m.bio_age === null || m.bio_age === undefined) {
        ageEl.innerHTML = '<a href="#" onclick="setAge(); return false;" style="color:#fff;opacity:.85;font-size:13px;font-weight:400">укажи возраст →</a>';
      } else {
        const realAge = (d.user_settings || {}).age;
        const delta = realAge ? (m.bio_age - realAge) : null;
        let deltaTxt = '';
        if (delta !== null && delta !== undefined) {
          deltaTxt = delta === 0 ? ' (как реальный)' : (delta < 0 ? ` (−${Math.abs(delta)} к реальному)` : ` (+${delta} к реальному)`);
        }
        ageEl.textContent = m.bio_age + deltaTxt;
      }
    }
    const zone = document.getElementById('hero_zone');
    const hero = document.querySelector('.hrv-hero');
    if (zone && hero) {
      const sd = m.sdnn_5min, rm = m.rmssd_5min;
      hero.classList.remove('zone-good', 'zone-warn', 'zone-alert', 'zone-info');
      if (sd === null || sd === undefined) {
        zone.textContent = 'Собираем данные… (нужно 3–5 минут для корректных значений SDNN/RMSSD)';
        hero.classList.add('zone-info');
      } else {
        let label, zoneClass;
        if (sd < 20) { label = 'Очень низкая — критическое снижение адаптации'; zoneClass = 'zone-alert'; }
        else if (sd < 50) { label = 'Низкая — ограниченные адаптационные ресурсы'; zoneClass = 'zone-warn'; }
        else if (sd < 100) { label = 'Норма — хорошая общая вариабельность'; zoneClass = 'zone-good'; }
        else { label = 'Высокая — отличная адаптивность'; zoneClass = 'zone-good'; }
        let rmLab = '';
        if (rm !== null && rm !== undefined) {
          if (rm < 19) rmLab = ' · парасимпатика снижена';
          else if (rm < 35) rmLab = ' · парасимпатика в норме';
          else rmLab = ' · парасимпатика высокая';
        }
        zone.textContent = label + rmLab;
        hero.classList.add(zoneClass);
      }
    }

    // Картина одним абзацем
    const picCard = document.getElementById('picture_card');
    if (m.picture) {
      document.getElementById('picture_card_text').textContent = m.picture;
      picCard.style.display = 'block';
    } else {
      picCard.style.display = 'none';
    }

    // Рекомендации (повторно)
    const recsCard = document.getElementById('recs_card');
    const recsCardBody = document.getElementById('recs_card_body');
    if (m.recommendations && m.recommendations.length > 0) {
      const icon = {good:'✓', info:'ℹ', action:'→', alert:'⚠'};
      recsCardBody.innerHTML = m.recommendations.map(r =>
        `<div class="rec-card rec-${r.level||'info'}"><div class="text"><b>${icon[r.level]||'•'}</b> ${r.text}</div><div class="why">${r.why}</div></div>`
      ).join('');
      recsCard.style.display = 'block';
    } else {
      recsCard.style.display = 'none';
    }

    // Все показатели справочника — bullet charts
    renderFullMetrics(m.primary_full || []);

    // 3-мин Kubios-style блок
    const k3 = m.kubios_style_3min || {};
    setText('k3_hr', k3.hr);
    setText('k3_sdnn', k3.sdnn);
    setText('k3_rmssd', k3.rmssd);
    setText('k3_sd1', k3.sd1);
    setText('k3_sd2', k3.sd2);
    setText('k3_si', k3.stress_index);
    setText('k3_pns', k3.pns_index);
    setText('k3_sns', k3.sns_index);
    setText('k3_n', k3.n_rr);
  } catch (e) {
    console.error(e);
  }
}
function zoneColor(label) {
  const l = (label || '').toLowerCase();
  if (l.includes('норма') || l.includes('баланс') || l.includes('хорош') || l.includes('отлично')) return '#a8d5ad';
  if (l.includes('критич') || l.includes('очень') || l.includes('стресс') || l.includes('перенапря')) return '#f4a8a8';
  if (l.includes('ваготон') || l.includes('пнс') || l.includes('парасимп')) return '#c4d8f0';
  if (l.includes('снс') || l.includes('симпат')) return '#ffd2a8';
  return '#ffd699';
}

function renderFullMetrics(arr) {
  const root = document.getElementById('full_metrics_list');
  if (!root) return;
  if (!arr || arr.length === 0) {
    root.innerHTML = '<div style="color:#888;font-size:13px;padding:14px;text-align:center">Собираем данные… (показатели появятся через 1–5 минут после старта записи)</div>';
    return;
  }
  let h = '';
  for (const m of arr) {
    const ranges = m.ranges || [];
    if (ranges.length === 0) continue;
    const loTotal = ranges[0].lo;
    let hiTotal = ranges[ranges.length - 1].hi;
    if (hiTotal >= 9999) {
      const normHi = Math.max(...ranges.filter(r => /норма|баланс/i.test(r.label)).map(r => r.hi));
      hiTotal = Math.max(normHi * 2.5, ranges[Math.max(0, ranges.length - 2)].hi * 1.4);
    }
    const span = Math.max(hiTotal - loTotal, 1e-9);
    const pos = v => Math.max(0, Math.min(100, 100 * (v - loTotal) / span));
    let segs = '';
    for (const r of ranges) {
      const hi = Math.min(r.hi, hiTotal);
      if (hi <= r.lo) continue;
      const left = pos(r.lo), w = pos(hi) - left;
      const color = zoneColor(r.label);
      segs += `<div class="seg" style="left:${left.toFixed(1)}%;width:${w.toFixed(1)}%;background:${color}" title="${r.label}: ${r.meaning}"></div>`;
    }
    let mark = '';
    let labelLine = '<div class="bm-label" style="color:#888">собираем…</div>';
    let valHtml = '<span style="color:#bbb">—</span>';
    if (m.value !== null && m.value !== undefined) {
      const p = pos(m.value);
      mark = `<div class="mark" style="left:${p.toFixed(2)}%"></div>`;
      labelLine = `<div class="bm-label"><b>${m.label || '—'}</b> — ${m.meaning || ''}</div>`;
      valHtml = `${m.value}<span class="u">${m.unit || ''}</span>`;
    }
    h += `<div class="bm-row">
      <div class="bm-head"><div class="bm-name">${m.name}</div><div class="bm-val">${valHtml}</div></div>
      <div class="bm-desc">${m.description}</div>
      <div class="bm-bar">${segs}${mark}</div>
      ${labelLine}
      <div class="bm-app"><b>В нейроассессменте:</b> ${m.applicability}</div>
    </div>`;
  }
  root.innerHTML = h;
}

let tachoChart = null;
function drawTachogram(rrArr) {
  if (typeof Chart === 'undefined') {
    const wrap = document.querySelector('.tachogram-box .chart-wrap');
    if (wrap && !wrap.querySelector('.chart-fallback')) {
      wrap.innerHTML = '<div class="chart-fallback">⚠ График недоступен (Chart.js не загрузился). Текущие RR: <b>' + rrArr.slice(-10).join(', ') + '</b></div>';
    }
    return;
  }
  const labels = rrArr.map((_, i) => i + 1);
  if (tachoChart) {
    tachoChart.data.labels = labels;
    tachoChart.data.datasets[0].data = rrArr;
    tachoChart.update('none');
    return;
  }
  const ctx = document.getElementById('tacho_chart').getContext('2d');
  tachoChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        label: 'RR (мс)',
        data: rrArr,
        borderColor: '#1a73e8',
        backgroundColor: 'rgba(26,115,232,.12)',
        tension: 0.15,
        borderWidth: 1.5,
        pointRadius: 2,
        pointHoverRadius: 4,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
      plugins: { legend: { display: false } },
      scales: {
        y: { title: { display: true, text: 'RR, мс' }, ticks: { font: { size: 10 } } },
        x: { display: false }
      }
    }
  });
}

let tsChart = null;
function drawTimeseries(data) {
  const labels = data.map(d => d.t);
  const hr = data.map(d => d.hr);
  const rmssd = data.map(d => d.rmssd);
  const sdnn = data.map(d => d.sdnn);
  if (tsChart) {
    tsChart.data.labels = labels;
    tsChart.data.datasets[0].data = hr;
    tsChart.data.datasets[1].data = rmssd;
    tsChart.data.datasets[2].data = sdnn;
    tsChart.update('none');
    return;
  }
  const ctx = document.getElementById('ts_chart').getContext('2d');
  tsChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {label: 'HR', data: hr, borderColor: '#1a73e8', backgroundColor: 'rgba(26,115,232,.1)', yAxisID: 'y', tension: .25, borderWidth: 2, pointRadius: 2},
        {label: 'RMSSD', data: rmssd, borderColor: '#2e7d32', backgroundColor: 'rgba(46,125,50,.1)', yAxisID: 'y1', tension: .25, borderWidth: 2, pointRadius: 2},
        {label: 'SDNN', data: sdnn, borderColor: '#e65100', backgroundColor: 'rgba(230,81,0,.1)', yAxisID: 'y1', tension: .25, borderWidth: 2, pointRadius: 2},
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        y: { type: 'linear', position: 'left', title: { display: true, text: 'HR' }, ticks: { font: { size: 10 } } },
        y1: { type: 'linear', position: 'right', title: { display: true, text: 'мс' }, grid: { drawOnChartArea: false }, ticks: { font: { size: 10 } } },
        x: { ticks: { font: { size: 10 }, autoSkip: true, maxRotation: 0 } },
      }
    }
  });
}

setInterval(tick, 2000);
tick();

async function finishDay() {
  if (!confirm('Завершить день, склеить сегменты и построить отчёт?')) return;
  const r = await fetch('/finish_day', {method: 'POST'});
  const d = await r.json();
  if (d.ok) {
    alert('Отчёт строится: ' + d.merged_csv + ' (' + d.n_rr + ' RR). Откроется в браузере автоматически.');
  } else {
    alert('Ошибка: ' + JSON.stringify(d));
  }
}

async function setUser() {
  const v = document.getElementById('user_input').value.trim();
  if (!v) return;
  const r = await fetch('/api/set_user', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({user_label: v})});
  const d = await r.json();
  if (d.ok) {
    document.getElementById('user_known').textContent = 'теперь записывается на: ' + v;
    loadHistory();
  }
}

async function setAge() {
  const a = prompt('Сколько тебе лет? (нужно для корректного биовозраста)');
  if (!a) return;
  const n = parseInt(a, 10);
  if (isNaN(n) || n < 15 || n > 95) { alert('Введи число от 15 до 95'); return; }
  await fetch('/api/set_age', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({age: n})});
  location.reload();
}

async function loadCompare() {
  try {
    const r = await fetch('/api/compare_to_last');
    const d = await r.json();
    const box = document.getElementById('compare_block');
    const body = document.getElementById('compare_body');
    if (!d.ok) {
      body.innerHTML = '<div style="color:#888;padding:6px 0">⚠ ' + (d.reason || 'нет данных') + '</div>';
    } else {
      const cur = d.current || {};
      const fmt = (v, suffix='') => (v === null || v === undefined) ? '—' : (v + suffix);
      let h = '<div style="font-size:13px;margin-bottom:8px">' +
              '<b>Прошлая сессия:</b> ' + (d.prev_started_at || '?') +
              ' — ' + (d.prev_state || '?') + ' (' + (d.prev_overall ?? '?') + '/100)</div>';
      h += '<div style="font-size:13px;margin-bottom:10px">' +
           '<b>Сейчас:</b> RMSSD ' + fmt(cur.rmssd, ' мс') + ', SDNN ' + fmt(cur.sdnn, ' мс') + ', артефактов ' + fmt(cur.artifacts_pct, '%') + '</div>';
      h += '<div><b>Возможные причины изменений:</b><ul style="margin-top:4px">';
      (d.hypotheses || []).forEach(t => { h += '<li style="margin-bottom:3px">' + t + '</li>'; });
      h += '</ul></div>';
      body.innerHTML = h;
    }
    box.style.display = 'block';
    box.style.background = '#e3f2fd';
    box.style.borderColor = '#1a73e8';
    box.scrollIntoView({behavior: 'smooth', block: 'center'});
    setTimeout(() => { box.style.background = ''; box.style.borderColor = ''; }, 2500);
  } catch (e) {
    alert('Ошибка запроса: ' + e.message);
  }
}

async function loadHistory() {
  const r = await fetch('/api/history');
  const d = await r.json();
  const el = document.getElementById('history_table');
  if (!d.sessions || d.sessions.length === 0) {
    el.innerHTML = '<i>пока нет завершённых сессий — каждая будет сохраняться когда закрывается сегмент (отошла / переключила пользователя)</i>';
    return;
  }
  let h = '<table class="hist"><thead><tr><th>начало</th><th>длит.</th><th>HR</th><th>RMSSD</th><th>SDNN</th><th>SI</th><th>Готов.</th><th>Балл</th><th>Состояние</th></tr></thead><tbody>';
  d.sessions.forEach(s => {
    const fmt = (v, d=0) => (v === null || v === undefined) ? '—' : Number(v).toFixed(d);
    h += '<tr>'
      + '<td>' + (s.started_at || '?').substring(5,16) + '</td>'
      + '<td>' + ((s.duration_sec||0)/60).toFixed(1) + ' мин</td>'
      + '<td>' + fmt(s.hr_mean) + '</td>'
      + '<td>' + fmt(s.rmssd) + '</td>'
      + '<td>' + fmt(s.sdnn) + '</td>'
      + '<td>' + fmt(s.stress_index) + '</td>'
      + '<td>' + fmt(s.rd) + '</td>'
      + '<td>' + fmt(s.overall_score) + '</td>'
      + '<td>' + (s.state_text || '—') + '</td>'
      + '</tr>';
  });
  h += '</tbody></table>';
  el.innerHTML = h;
}
setTimeout(loadHistory, 1000);
</script>
</body>
</html>
"""


# ---------- Запуск ----------

async def main():
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, STATE.shutdown_event.set)

    config = uvicorn.Config(app, host="0.0.0.0", port=8765, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    ble_task = asyncio.create_task(stream_loop())

    # Найдём локальный IP для удобства открытия с телефона
    local_ip = None
    try:
        import socket as _s
        _sock = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        _sock.connect(("8.8.8.8", 80))
        local_ip = _sock.getsockname()[0]
        _sock.close()
    except Exception:
        pass

    log.info("Открой в браузере на ноуте: http://localhost:8765")
    if local_ip:
        log.info("С телефона в той же Wi-Fi: http://%s:8765", local_ip)
    try:
        await STATE.shutdown_event.wait()
    finally:
        log.info("Останавливаюсь...")
        server.should_exit = True
        await asyncio.gather(ble_task, server_task, return_exceptions=True)
        STATE.close_segment()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
