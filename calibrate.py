"""
calibrate.py — калибровка формул под эталонные значения Kubios HRV App.

Берёт CSV-сегмент за тот же интервал, что замерял Kubios, перебирает параметры
детрендинга и артефакт-коррекции, находит те что дают минимальное расхождение
с эталоном по совокупности (SDNN, RMSSD, MeanRR), сохраняет в data/calibration.json.

auto_collector читает этот файл при каждом расчёте и применяет калиброванные параметры.

Использование:
    ./venv_new/bin/python calibrate.py \\
        --csv data/sessions/20260529/segment_111304.csv \\
        --start "11:23:00" --end "11:25:56" \\
        --kubios-sdnn 40.26 --kubios-rmssd 44 --kubios-meanrr 963.62 \\
        [--kubios-sd1 30.94] [--kubios-sd2 47.93] [--kubios-lfhf 1.33]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd
import neurokit2 as nk

ROOT = Path(__file__).resolve().parent
CALIB_PATH = ROOT / "data" / "calibration.json"
CALIB_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_rr_in_window(csv_path: Path, start_iso: str, end_iso: str) -> np.ndarray:
    df = pd.read_csv(csv_path)
    # дополняем дату, если приходит только время
    date = df["Timestamp_ISO"].iloc[0].split(" ")[0]
    if ":" in start_iso and " " not in start_iso:
        start_iso = f"{date} {start_iso}"
    if ":" in end_iso and " " not in end_iso:
        end_iso = f"{date} {end_iso}"
    mask = (df["Timestamp_ISO"] >= start_iso) & (df["Timestamp_ISO"] <= end_iso)
    rr = df[mask]["RR_Interval_ms"].to_numpy(dtype=float)
    rr = rr[(rr >= 300) & (rr <= 2000)]
    return rr


def kubios_clean(rr: np.ndarray) -> np.ndarray:
    peaks = np.cumsum(rr).astype(int)
    info, _ = nk.signal_fixpeaks(peaks=peaks, sampling_rate=1000, iterative=True, method="kubios")
    rr_sec = info.get("rr")
    if rr_sec is None or len(rr_sec) == 0:
        return rr
    return np.asarray(rr_sec, dtype=float) * 1000.0


def compute_sdnn(rr: np.ndarray, method: str, order: int = 4, resample_hz: float = 4.0, regularization: int = 500) -> float:
    if method == "raw":
        return float(np.std(rr, ddof=1))
    t_rr = np.cumsum(rr) / 1000.0
    if resample_hz > 0:
        step = 1.0 / resample_hz
        t_u = np.arange(t_rr[0], t_rr[-1], step)
        sig = np.interp(t_u, t_rr, rr)
    else:
        sig = rr.copy()
    if method == "polynomial":
        d = nk.signal_detrend(sig, method="polynomial", order=order)
    elif method == "tarvainen":
        d = nk.signal_detrend(sig, method="tarvainen2002", regularization=regularization)
    elif method == "loess":
        d = nk.signal_detrend(sig, method="loess", alpha=0.3)
    else:
        return float(np.std(rr, ddof=1))
    return float(np.std(d, ddof=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="Путь к CSV-сегменту")
    ap.add_argument("--start", required=True, help="Начало окна Kubios (HH:MM:SS или ISO)")
    ap.add_argument("--end", required=True, help="Конец окна Kubios")
    ap.add_argument("--kubios-sdnn", type=float, required=True)
    ap.add_argument("--kubios-rmssd", type=float, required=True)
    ap.add_argument("--kubios-meanrr", type=float, required=True)
    ap.add_argument("--kubios-sd1", type=float, default=None)
    ap.add_argument("--kubios-sd2", type=float, default=None)
    ap.add_argument("--kubios-lfhf", type=float, default=None)
    args = ap.parse_args()

    csv = Path(args.csv).expanduser().resolve()
    if not csv.exists():
        print(f"Не найден: {csv}", file=sys.stderr)
        sys.exit(1)

    rr_raw = load_rr_in_window(csv, args.start, args.end)
    if len(rr_raw) < 60:
        print(f"Слишком мало RR в окне: {len(rr_raw)}", file=sys.stderr)
        sys.exit(1)

    rr = kubios_clean(rr_raw)
    print(f"RR в окне после Kubios fixpeaks: {len(rr)}")

    # Базовые метрики (не зависят от калибровки)
    mean_rr = float(np.mean(rr))
    rmssd = float(np.sqrt(np.mean(np.diff(rr) ** 2)))
    print(f"MeanRR  = {mean_rr:.2f}  (Kubios: {args.kubios_meanrr})  diff = {(mean_rr - args.kubios_meanrr)/args.kubios_meanrr*100:+.2f}%")
    print(f"RMSSD   = {rmssd:.2f}  (Kubios: {args.kubios_rmssd})  diff = {(rmssd - args.kubios_rmssd)/args.kubios_rmssd*100:+.2f}%")

    # Перебор параметров SDNN
    candidates = []
    # polynomial
    for order, hz in product([1, 2, 3, 4, 5, 6, 7, 8], [1.0, 2.0, 4.0, 8.0, 10.0]):
        try:
            sdnn = compute_sdnn(rr, "polynomial", order=order, resample_hz=hz)
            diff = (sdnn - args.kubios_sdnn) / args.kubios_sdnn * 100
            candidates.append(("polynomial", {"order": order, "resample_hz": hz}, sdnn, abs(diff), diff))
        except Exception:
            pass
    # tarvainen
    for reg, hz in product([100, 300, 500, 1000, 2000], [2.0, 4.0, 8.0]):
        try:
            sdnn = compute_sdnn(rr, "tarvainen", regularization=reg, resample_hz=hz)
            diff = (sdnn - args.kubios_sdnn) / args.kubios_sdnn * 100
            candidates.append(("tarvainen", {"regularization": reg, "resample_hz": hz}, sdnn, abs(diff), diff))
        except Exception:
            pass
    # raw
    sdnn_raw = compute_sdnn(rr, "raw")
    diff_raw = (sdnn_raw - args.kubios_sdnn) / args.kubios_sdnn * 100
    candidates.append(("raw", {}, sdnn_raw, abs(diff_raw), diff_raw))

    candidates.sort(key=lambda x: x[3])

    print()
    print("=== Топ-10 по совпадению SDNN с Kubios ===")
    print(f"{'method':12s} {'params':40s} {'SDNN':>8s} {'diff':>8s}")
    for m, p, s, _, d in candidates[:10]:
        print(f"{m:12s} {str(p):40s} {s:8.2f} {d:+7.2f}%")

    best = candidates[0]
    method, params, sdnn_best, _, diff_best = best

    print()
    print(f"✓ Лучший: {method} {params} → SDNN={sdnn_best:.2f}, расхождение {diff_best:+.2f}%")

    # Сохраняем калибровку
    calib = {
        "calibrated_at": datetime.now().isoformat(timespec="seconds"),
        "sdnn_method": method,
        "sdnn_params": params,
        "kubios_reference": {
            "sdnn": args.kubios_sdnn,
            "rmssd": args.kubios_rmssd,
            "mean_rr": args.kubios_meanrr,
            "sd1": args.kubios_sd1,
            "sd2": args.kubios_sd2,
            "lf_hf": args.kubios_lfhf,
        },
        "achieved": {
            "sdnn": sdnn_best,
            "rmssd": rmssd,
            "mean_rr": mean_rr,
        },
        "diff_pct": {
            "sdnn": diff_best,
            "rmssd": (rmssd - args.kubios_rmssd) / args.kubios_rmssd * 100,
            "mean_rr": (mean_rr - args.kubios_meanrr) / args.kubios_meanrr * 100,
        },
        "window": {
            "csv": str(csv),
            "start": args.start,
            "end": args.end,
            "n_rr_raw": int(len(rr_raw)),
            "n_rr_clean": int(len(rr)),
        },
    }
    CALIB_PATH.write_text(json.dumps(calib, indent=2, ensure_ascii=False))
    print(f"\nСохранено в {CALIB_PATH}")
    print("auto_collector подхватит при следующем запросе метрик автоматически.")


if __name__ == "__main__":
    main()
