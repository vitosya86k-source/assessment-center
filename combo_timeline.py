"""
combo_timeline.py — сводит CSV всех модулей комбо-разбора в ОДИН таймлайн по времени.

На вход — рабочая папка разбора (combo/results/<...>/) с emotions.csv / pose.csv /
rppg.csv. На выход — timeline.csv на единой 1-сек сетке (по общему t_sec) + краткая
сводка покрытия по каналам. Это основа для интегрированного отчёта (поведение + HRV).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

# какие колонки тащим из каждого модуля в общий таймлайн
_KEEP = {
    "emotions": ["face_ok", "dominant", "valence", "arousal"],
    "pose": ["pose_ok", "hand_to_face", "fidget_idx", "head_tilt_deg"],
    "rppg": ["hr_bpm", "snr"],
}


def _load_on_grid(csv_path: Path, cols: list[str], grid: np.ndarray) -> pd.DataFrame:
    if not csv_path.exists():
        return pd.DataFrame(index=grid)
    df = pd.read_csv(csv_path)
    if "t_sec" not in df.columns or df.empty:
        return pd.DataFrame(index=grid)
    df = df[[c for c in (["t_sec"] + cols) if c in df.columns]].copy()
    df["t_grid"] = df["t_sec"].round().astype(int)
    df = df.drop(columns=["t_sec"]).groupby("t_grid").last()
    return df.reindex(grid)


def build_timeline(workdir: str | Path) -> dict:
    workdir = Path(workdir)
    files = {k: workdir / f"{k}.csv" for k in _KEEP}

    # общий временной диапазон
    tmax = 0
    for f in files.values():
        if f.exists():
            try:
                d = pd.read_csv(f)
                if "t_sec" in d.columns and len(d):
                    tmax = max(tmax, int(np.nanmax(d["t_sec"].to_numpy())))
            except Exception:
                pass
    if tmax <= 0:
        return {"ok": False, "note": "нет данных модулей для таймлайна"}

    grid = np.arange(0, tmax + 1)
    merged = pd.DataFrame({"t_sec": grid}).set_index("t_sec")
    coverage = {}
    for mod, cols in _KEEP.items():
        part = _load_on_grid(files[mod], cols, grid)
        # префиксуем колонки именем модуля, чтобы не пересекались
        part = part.rename(columns={c: f"{mod}_{c}" for c in part.columns})
        merged = merged.join(part)
        flag = f"{mod}_{'face_ok' if mod=='emotions' else 'pose_ok' if mod=='pose' else 'hr_bpm'}"
        coverage[mod] = round(float(merged[flag].notna().mean()), 3) if flag in merged else 0.0

    out = workdir / "timeline.csv"
    merged.reset_index().to_csv(out, index=False, encoding="utf-8-sig")

    return {"ok": True, "csv": str(out), "duration_sec": int(tmax),
            "coverage": coverage, "rows": int(len(grid))}


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    a = ap.parse_args()
    print(json.dumps(build_timeline(a.workdir), ensure_ascii=False, indent=2))
