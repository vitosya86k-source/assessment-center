"""
hrv_calibration.py — применение калибровки SDNN под Kubios в едином месте.

Раньше калибровка (data/calibration.json, найденная calibrate.py) применялась
ТОЛЬКО в auto_collector.py, а HRVCalculator бота считал SDNN сырым np.std —
поэтому замеры из @HRV_monitor_bot не совпадали с Kubios.

Этот модуль выносит ту же логику, что в auto_collector._compute_sdnn_calibrated,
чтобы и бот, и фоновый сборщик считали SDNN одинаково. Логика 1:1 с
calibrate.py::compute_sdnn (resample → neurokit signal_detrend → std).

Калибровка от 29.05.2026: polynomial order=7 resample_hz=8.0, расхождение с
Kubios <1% (SDNN 40.59 vs 40.26). См. data/calibration.json.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

try:
    import neurokit2 as _nk
    _NK_OK = True
except Exception:  # neurokit2 может отсутствовать в лёгком окружении
    _nk = None
    _NK_OK = False

_CALIB_PATH = Path(__file__).resolve().parent / "data" / "calibration.json"
_DEFAULT_CALIB = {"sdnn_method": "polynomial", "sdnn_params": {"order": 4, "resample_hz": 4.0}}

# Кэш с перезагрузкой по mtime — чтобы перекалибровка подхватывалась без рестарта
_CALIB_CACHE = {"data": None, "mtime": 0.0}


def load_calibration() -> dict:
    """Читает calibration.json. При отсутствии/ошибке — безопасный дефолт."""
    if _CALIB_PATH.exists():
        try:
            return json.loads(_CALIB_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return dict(_DEFAULT_CALIB)


def get_calibration() -> dict:
    """Калибровка с кэшем, перечитывается при изменении файла."""
    try:
        m = _CALIB_PATH.stat().st_mtime if _CALIB_PATH.exists() else 0.0
    except Exception:
        m = 0.0
    if _CALIB_CACHE["data"] is None or m != _CALIB_CACHE["mtime"]:
        _CALIB_CACHE["data"] = load_calibration()
        _CALIB_CACHE["mtime"] = m
    return _CALIB_CACHE["data"]


def compute_sdnn_calibrated(rr_v: np.ndarray) -> float:
    """SDNN с откалиброванным детрендингом (как в auto_collector.py).

    Args:
        rr_v: массив RR-интервалов в мс (уже очищенный от артефактов).

    Returns:
        SDNN в мс. При недоступности neurokit2, коротком окне (<60 RR) или
        нелепом результате — честный fallback на сырой np.std.
    """
    rr_v = np.asarray(rr_v, dtype=float)
    naive = float(np.std(rr_v, ddof=1)) if len(rr_v) > 1 else 0.0
    if not _NK_OK or len(rr_v) < 60:
        return naive

    calib = get_calibration()
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
            d = _nk.signal_detrend(sig, method="tarvainen2002",
                                   regularization=int(params.get("regularization", 500)))
        elif method == "loess":
            d = _nk.signal_detrend(sig, method="loess", alpha=float(params.get("alpha", 0.3)))
        else:
            return naive
        sdnn = float(np.std(d, ddof=1))
        # sanity-guard: калибровка не должна менять SDNN в разы
        if naive > 0 and (sdnn > naive * 2 or sdnn < naive * 0.3):
            return naive
        return sdnn
    except Exception:
        return naive


def calibration_info() -> str:
    """Короткая строка о текущей калибровке — для /status и логов."""
    c = get_calibration()
    method = c.get("sdnn_method", "?")
    params = c.get("sdnn_params", {})
    when = c.get("calibrated_at", "—")
    diff = c.get("diff_pct", {}).get("sdnn")
    diff_s = f", ΔSDNN {diff:+.2f}%" if isinstance(diff, (int, float)) else ""
    nk_s = "" if _NK_OK else " [neurokit2 НЕ установлен → сырой SDNN]"
    return f"SDNN-калибровка: {method} {params} (от {when}{diff_s}){nk_s}"
