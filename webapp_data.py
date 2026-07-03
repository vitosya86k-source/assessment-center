"""
webapp_data.py — превращает метрики в JSON для веб-интерфейсов webapp/.

- hrv_payload(...) / write_hrv_json(...) → hrv_data.json для webapp/hrv.html (пульсовой бот).
- combo_to_webapp(summary) → combo_data.json для webapp/combo_live.html (комбо-бот).

Пороги статусов — из HRV_METRICS_REFERENCE.md. Это данные для UI «в той концепции»,
что прислала Виталия (мокапы 25.06).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Русские подписи 7 осей (порядок как в hrv_calculator.calculate_axis_scores)
AXIS_RU = {"RD": "Готовность", "SR": "Стрессоустойчивость", "AD": "Адаптивность",
           "FL": "Гибкость", "RC": "Восстановление", "EN": "Выносливость", "BL": "Баланс"}

# Метрики для экрана: иконка, описание, единица, норма, объяснение, что делать.
METRIC_META = {
    "rmssd": {"ic": "〜", "ds": "Активность восстановления", "unit": "ms", "norm": "25–50 ms",
              "explain": "RMSSD отражает активность восстановления и работу парасимпатической системы. Чем выше — тем лучше организм восстанавливается и справляется со стрессом.",
              "advice": ["Сохраняйте режим сна", "Умеренные нагрузки", "Дыхательные практики"]},
    "sdnn": {"ic": "〜", "ds": "Общая вариабельность", "unit": "ms", "norm": "50–100 ms",
             "explain": "SDNN — общая вариабельность ритма, маркер адаптационных резервов (откалибровано под Kubios).",
             "advice": ["Поддерживайте режим", "Прогулки на воздухе"]},
    "pnn50": {"ic": "%", "ds": "Гибкость нервной системы", "unit": "%", "norm": "10–20 %",
              "explain": "pNN50 — гибкость нервной системы. Стоит смотреть в динамике.",
              "advice": ["Наблюдать в динамике"]},
    "mean_rr": {"ic": "♥", "ds": "Средний интервал", "unit": "ms", "norm": "600–1200 ms",
                "explain": "Средний RR-интервал, обратный пульсу.", "advice": []},
    "lf_hf_ratio": {"ic": "⚖", "ds": "Баланс систем", "unit": "ratio", "norm": "0.5–2.0",
                    "explain": "LF/HF — баланс симпатики и парасимпатики.",
                    "advice": ["Дыхательные практики", "Меньше стимуляторов вечером"]},
    "stress_index": {"ic": "⚡", "ds": "Уровень стресса и напряжения", "unit": "", "norm": "50–150",
                     "explain": "Индекс напряжения Баевского — напряжение регуляторных систем.",
                     "advice": ["Снизить нагрузку", "Дыхание 4-7-8", "Сон 7–8 ч"]},
    "pns_index": {"ic": "%", "ds": "Парасимпатическая активность", "unit": "", "norm": "−0.3…выше",
                  "explain": "PNS Index (Kubios) — активность парасимпатики, восстановление.",
                  "advice": ["Хороший показатель восстановления"]},
    "sns_index": {"ic": "🔥", "ds": "Симпатическая активность", "unit": "", "norm": "−0.3…+0.3",
                  "explain": "SNS Index (Kubios) — мобилизация/симпатика.",
                  "advice": ["Паузы на восстановление"]},
    "biological_age": {"ic": "☺", "ds": "Биологический возраст по HRV", "unit": "лет", "norm": "≈ ваш возраст",
                       "explain": "Функциональный возраст по HRV относительно нормы для возраста.",
                       "advice": ["Так держать"]},
}


# красивые отображаемые имена метрик
DISPLAY_NAMES = {"rmssd": "RMSSD", "sdnn": "SDNN", "pnn50": "pNN50", "mean_rr": "Mean RR",
                 "lf_hf_ratio": "LF/HF", "stress_index": "Stress Index", "pns_index": "PNS Index",
                 "sns_index": "SNS Index", "biological_age": "Bio-age"}


def _classify(key: str, v: float) -> str:
    """Статус метрики: ok | watch | crit."""
    if v is None:
        return "watch"
    if key == "rmssd":
        return "ok" if v >= 25 else "watch" if v >= 15 else "crit"
    if key == "sdnn":
        return "ok" if v >= 50 else "watch" if v >= 30 else "crit"
    if key == "pnn50":
        return "watch" if (v < 3 or v > 25) else "ok"
    if key == "lf_hf_ratio":
        return "ok" if 0.5 <= v <= 2.0 else "crit" if (v > 4 or v < 0.3) else "watch"
    if key == "stress_index":
        return "ok" if v <= 100 else "watch" if v <= 200 else "crit"
    if key == "pns_index":
        return "ok" if v >= -0.3 else "watch" if v >= -1 else "crit"
    if key == "sns_index":
        return "ok" if -0.3 <= v <= 0.3 else "watch" if v <= 1.5 else "crit"
    if key == "mean_rr":
        return "ok" if 600 <= v <= 1200 else "watch"
    if key == "biological_age":
        return "ok"
    return "watch"


def _fmt(key: str, v: float) -> str:
    meta = METRIC_META[key]
    if v is None:
        return "—"
    if key in ("pns_index", "sns_index", "lf_hf_ratio"):
        num = f"{v:.1f}"
    elif key == "biological_age":
        num = f"{int(v)}"
    else:
        num = f"{v:.0f}"
    return f"{num} {meta['unit']}".strip()


def hrv_payload(metrics: dict, axes: dict, overall: int, updated: str = "") -> dict:
    state = ("Отличное состояние" if overall >= 80 else "Хорошее состояние" if overall >= 60
             else "Напряжение" if overall >= 40 else "Сниженное" if overall >= 20 else "Критическое")
    axes_ru = {AXIS_RU[k]: axes.get(k) for k in AXIS_RU if axes.get(k) is not None}

    out_metrics = []
    for key, meta in METRIC_META.items():
        if key not in metrics or metrics[key] is None:
            continue
        v = metrics[key]
        st = _classify(key, v)
        out_metrics.append({
            "ic": meta["ic"], "nm": DISPLAY_NAMES.get(key, key.upper()),
            "val": _fmt(key, v), "ds": meta["ds"], "st": st, "norm": meta["norm"],
            "explain": meta["explain"], "advice": meta["advice"],
            "spark": [round(float(v), 2)],  # история по неделе появится позже
        })

    # карточки «что хорошо / внимание / рекомендация»
    worst = min((m for m in out_metrics), key=lambda m: {"ok": 0, "watch": 1, "crit": 2}[m["st"]], default=None)
    cards = [
        {"ic": "🛡", "t": "Что хорошо", "s": "Парасимпатика и восстановление в норме.", "col": "#00d4aa"},
        {"ic": "⚠", "t": "На что обратить внимание",
         "s": "Симпатическая активность/стресс — наблюдать.", "col": "#ffd93d"},
        {"ic": "ⓘ", "t": "Рекомендация", "s": "Лёгкая активность и дыхательные практики.", "col": "#229ed9"},
    ]
    return {
        "score": int(round(overall)), "state": state,
        "updated": updated or datetime.now().strftime("%d.%m %H:%M"),
        "meaning": "Сводка по HRV-измерению. Подробности — во вкладке «Метрики».",
        "axes": axes_ru, "cards": cards, "metrics": out_metrics,
    }


def write_hrv_json(rr_csv: str, out_path: str, age: int | None = None):
    """Считает метрики из RR-CSV (колонка RR_Interval_ms) и пишет hrv_data.json."""
    import numpy as np
    import pandas as pd
    import hrv_calculator as hc

    df = pd.read_csv(rr_csv)
    col = next((c for c in ("RR_Interval_ms", "rr", "RR") if c in df.columns), None)
    if col is None:
        raise SystemExit(f"нет колонки RR в {rr_csv}")
    rr = df[col].to_numpy(float)
    rr = rr[(rr >= 300) & (rr <= 2000)]
    calc = hc.HRVCalculator(hr_data=(60000.0 / rr).tolist(), rr_intervals=rr.tolist())
    m = calc.calculate_all_metrics()
    axes = calc.calculate_axis_scores(m, freq_valid=True)
    overall = calc.calculate_overall_score(axes)
    payload = hrv_payload(m, axes, overall)
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


# ---------------- КОМБО ----------------
def _badge_from(status: str) -> str:
    return {"ok": "NORMA", "watch": "NABL", "signal": "SIGNAL", "crit": "CRIT"}.get(status, "NORMA")


def combo_to_webapp(summary: dict) -> dict:
    """Маппит сводку combo_analyze.analyze_video → данные для combo_live.html."""
    mods = summary.get("modules", {})
    emo = mods.get("emotions", {})
    pose = mods.get("pose", {})
    rppg = mods.get("rppg", {})
    sp = rppg.get("speech", {}) if rppg.get("ok") else {}
    pulse = rppg.get("pulse", {}) if rppg.get("ok") else {}

    channels = []
    # Речь
    if sp.get("available"):
        channels.append({"title": "Речь", "icon": "💬", "rows": [
            {"nm": "Экстраверсия ↔ Интроверсия", "val": f"{sp.get('ei_score')}", "min": 0, "max": 2,
             "value": sp.get("ei_score", 1), "zones": [[0.7, "#ffd93d"], [1.3, "#00d4aa"], [2, "#a8e063"]],
             "lo": "Интроверсия", "hi": "Экстраверсия", "st": "NORMA"},
            {"nm": "Питч F0", "val": f"{sp.get('pitch_mean_hz')} гц", "min": 70, "max": 350,
             "value": sp.get("pitch_mean_hz", 150), "zones": [[120, "#ffd93d"], [260, "#00d4aa"], [350, "#ffd93d"]], "st": "NORMA"},
            {"nm": "Темп", "val": f"{sp.get('tempo_onsets_per_min')}/мин", "min": 0, "max": 60,
             "value": sp.get("tempo_onsets_per_min", 0), "zones": [[40, "#00d4aa"], [60, "#ffd93d"]], "st": "NORMA"},
            {"nm": "Доля речи", "val": f"{round(sp.get('speech_ratio', 0) * 100)} %", "min": 0, "max": 100,
             "value": sp.get("speech_ratio", 0) * 100, "zones": [[30, "#ffd93d"], [80, "#00d4aa"], [100, "#ffd93d"]], "st": "NORMA"},
        ]})
    # Эмоции
    if emo.get("available"):
        channels.append({"title": "Эмоции", "icon": "🙂", "rows": [
            {"nm": "Доминирующая эмоция", "val": emo.get("dominant_overall", "—"), "min": 0, "max": 1,
             "value": 0.6, "zones": [[1, "#00d4aa"]], "st": "NORMA"},
            {"nm": "Лицо найдено", "val": f"{round(emo.get('face_coverage', 0) * 100)} %", "min": 0, "max": 100,
             "value": emo.get("face_coverage", 0) * 100, "zones": [[50, "#ffd93d"], [100, "#00d4aa"]], "st": "NORMA"},
        ]})
    # Поза
    if pose.get("available"):
        channels.append({"title": "Поза", "icon": "🧍", "rows": [
            {"nm": "Рука у лица", "val": f"{round(pose.get('hand_to_face_rate', 0) * 100)} %", "min": 0, "max": 60,
             "value": pose.get("hand_to_face_rate", 0) * 100, "zones": [[10, "#00d4aa"], [25, "#ffd93d"], [60, "#ff8c42"]], "st": "NORMA"},
            {"nm": "Ёрзанье", "val": pose.get("fidget_level", "—"), "min": 0, "max": 0.05,
             "value": pose.get("fidget_mean", 0), "zones": [[0.01, "#00d4aa"], [0.03, "#ffd93d"], [0.05, "#ff8c42"]], "st": "NORMA"},
        ]})
    # Пульс
    if pulse.get("available"):
        channels.append({"title": "Пульс по видео", "icon": "❤️", "rows": [
            {"nm": "ЧСС", "val": f"{pulse.get('hr_median')} уд/мин", "min": 40, "max": 160,
             "value": pulse.get("hr_median", 70), "zones": [[60, "#ffd93d"], [100, "#00d4aa"], [160, "#ff4757"]], "st": "NORMA"},
        ]})

    return {
        "audio_only": bool(rppg.get("mode") == "audio-only"),
        "elapsed": "—",
        "alerts": [],
        "channels": channels or [{"title": "Нет данных", "icon": "•", "rows": []}],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rr-csv", help="RR-CSV для hrv_data.json")
    ap.add_argument("--out", default=str(HERE / "webapp" / "hrv_data.json"))
    a = ap.parse_args()
    if a.rr_csv:
        p = write_hrv_json(a.rr_csv, a.out)
        print(f"hrv_data.json: score={p['score']} {p['state']}, метрик {len(p['metrics'])}")


if __name__ == "__main__":
    main()
