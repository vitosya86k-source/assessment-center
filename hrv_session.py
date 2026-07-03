"""
hrv_session.py — длинная запись HRV → сегменты, тренды, история.

Продуктовый сценарий (запрос Виталии): носить браслет 1–3 часа, метить упражнения/
паузы/еду/нагрузку, и видеть ДИНАМИКУ (например, био-возраст после еды +20, как
меняется SDNN/стресс на нагрузке vs baseline).

Сегментирует запись по меткам (/mark → user_<id>_marks.csv) или по окнам, считает
калиброванные метрики на сегмент, сравнивает с baseline, копит историю по сессиям.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import hrv_calculator as hc

RR_COL_CANDIDATES = ("RR_Interval_ms", "rr", "RR")
TS_COL_CANDIDATES = ("Timestamp_ISO", "timestamp_iso", "timestamp", "Timestamp")


def _cols(df):
    rr = next((c for c in RR_COL_CANDIDATES if c in df.columns), None)
    ts = next((c for c in TS_COL_CANDIDATES if c in df.columns), None)
    return rr, ts


def _metrics_for(rr: np.ndarray) -> dict:
    rr = rr[(rr >= 300) & (rr <= 2000)]
    if len(rr) < 20:
        return {}
    calc = hc.HRVCalculator(hr_data=(60000.0 / rr).tolist(), rr_intervals=rr.tolist())
    m = calc.calculate_all_metrics()
    axes = calc.calculate_axis_scores(m, freq_valid=True)
    m["overall"] = calc.calculate_overall_score(axes)
    return m


def segment_recording(csv_path: str | Path, marks_path: str | Path | None = None,
                      window_sec: int = 300) -> list[dict]:
    """Делит запись на сегменты: по меткам (если есть) или по окнам window_sec."""
    df = pd.read_csv(csv_path)
    rr_col, ts_col = _cols(df)
    if rr_col is None:
        raise ValueError(f"нет колонки RR в {csv_path}")
    df = df[df[rr_col].between(300, 2000)].reset_index(drop=True)
    if ts_col:
        df["_t"] = pd.to_datetime(df[ts_col], errors="coerce")
        df = df.dropna(subset=["_t"]).reset_index(drop=True)
    else:
        # нет времени — синтезируем по накоплению RR
        df["_t"] = pd.to_datetime(np.cumsum(df[rr_col].to_numpy()) / 1000.0, unit="s")

    t0 = df["_t"].iloc[0]
    df["_sec"] = (df["_t"] - t0).dt.total_seconds()
    total = df["_sec"].iloc[-1]

    bounds = []  # (label, start_sec, end_sec)
    marks = []
    if marks_path and Path(marks_path).exists():
        mdf = pd.read_csv(marks_path)
        if "timestamp_iso" in mdf.columns:
            mdf["_t"] = pd.to_datetime(mdf["timestamp_iso"], errors="coerce")
            mdf = mdf.dropna(subset=["_t"])
            for _, r in mdf.iterrows():
                sec = (r["_t"] - t0).total_seconds()
                if 0 <= sec <= total + 1:
                    marks.append((sec, str(r["label"])))
    if marks:
        marks.sort()
        for i, (sec, label) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else total
            bounds.append((label, sec, end))
    else:
        i = 0
        s = 0.0
        while s < total:
            bounds.append((f"окно {i+1}", s, min(s + window_sec, total)))
            s += window_sec
            i += 1

    segments = []
    for label, s, e in bounds:
        seg = df[(df["_sec"] >= s) & (df["_sec"] < e)]
        m = _metrics_for(seg[rr_col].to_numpy(float))
        if not m:
            continue
        segments.append({
            "label": label, "start_sec": round(s), "end_sec": round(e),
            "dur_min": round((e - s) / 60.0, 1),
            "sdnn": round(m["sdnn"], 1), "rmssd": round(m["rmssd"], 1),
            "mean_hr": round(m["mean_hr"], 0), "stress_index": round(m["stress_index"], 0),
            "pns_index": m.get("pns_index"), "sns_index": m.get("sns_index"),
            "bio_age": m.get("biological_age"), "overall": m.get("overall"),
        })
    return segments


def trend_report(segments: list[dict], name: str = "Участник") -> str:
    if not segments:
        return "Недостаточно данных для сегментного анализа."
    base = segments[0]
    L = [f"# Динамика HRV: {name}", f"_сегментов: {len(segments)}_", "",
         "## Сегменты (baseline = первый)", "",
         "| Сегмент | мин | SDNN | RMSSD | ЧСС | Stress | PNS | SNS | Bio-age | Балл |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for s in segments:
        L.append(f"| {s['label']} | {s['dur_min']} | {s['sdnn']} | {s['rmssd']} | "
                 f"{s['mean_hr']:.0f} | {s['stress_index']:.0f} | "
                 f"{s['pns_index'] if s['pns_index'] is not None else '—'} | "
                 f"{s['sns_index'] if s['sns_index'] is not None else '—'} | "
                 f"{s['bio_age'] if s['bio_age'] is not None else '—'} | {s['overall']} |")
    # тренды vs baseline
    L += ["", "## Сдвиги относительно baseline"]
    for s in segments[1:]:
        d_sdnn = s["sdnn"] - base["sdnn"]
        d_stress = s["stress_index"] - base["stress_index"]
        bio_txt = ""
        if s["bio_age"] is not None and base["bio_age"] is not None:
            db = s["bio_age"] - base["bio_age"]
            bio_txt = f", био-возраст {db:+d} лет"
        L.append(f"- **{s['label']}**: SDNN {d_sdnn:+.1f} мс, стресс {d_stress:+.0f}{bio_txt}")
    # авто-наблюдения
    L += ["", "## Наблюдения"]
    worst = max(segments, key=lambda s: s["stress_index"])
    best = max(segments, key=lambda s: s["overall"])
    L.append(f"- Пик стресса: «{worst['label']}» (SI {worst['stress_index']:.0f}).")
    L.append(f"- Лучшее состояние: «{best['label']}» (балл {best['overall']}).")
    if any(s["bio_age"] is not None for s in segments):
        bios = [(s["label"], s["bio_age"]) for s in segments if s["bio_age"] is not None]
        hi = max(bios, key=lambda x: x[1])
        L.append(f"- Макс. био-возраст: «{hi[0]}» ({hi[1]} лет) — смотри связь с едой/нагрузкой.")
    return "\n".join(L)


def append_history(user_id, segments, history_dir: str | Path) -> str:
    Path(history_dir).mkdir(parents=True, exist_ok=True)
    f = Path(history_dir) / f"user_{user_id}_history.jsonl"
    rec = {"ts": datetime.now().isoformat(timespec="seconds"),
           "segments": len(segments),
           "overall_avg": round(float(np.mean([s["overall"] for s in segments])), 1),
           "sdnn_avg": round(float(np.mean([s["sdnn"] for s in segments])), 1)}
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return str(f)


def analyze_long_recording(csv_path, marks_path=None, name="Участник",
                           user_id="local", history_dir=None, window_sec=300) -> dict:
    segments = segment_recording(csv_path, marks_path, window_sec)
    report = trend_report(segments, name)
    out = {"segments": segments, "report_md": report}
    if history_dir:
        out["history"] = append_history(user_id, segments, history_dir)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--marks", default=None)
    ap.add_argument("--window", type=int, default=300)
    ap.add_argument("--name", default="Участник")
    a = ap.parse_args()
    r = analyze_long_recording(a.csv, a.marks, a.name, window_sec=a.window)
    print(r["report_md"])
