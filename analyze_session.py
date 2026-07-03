"""
analyze_session.py — берёт CSV с RR-интервалами (формат PMD/PPI из этого проекта)
и собирает дневной HRV-отчёт со всеми 124 метриками NeuroKit2 + артефакт-коррекцией
Lipponen-Tarvainen (та же, что в Kubios). На выходе — самодостаточный HTML-файл
со сравнением «до коррекции / после», графиками RR-trend, тахограммой,
скользящим окном RMSSD/SDNN и таблицей всех метрик.

Запуск:
    ./venv_new/bin/python analyze_session.py data/user_322848528_latest.csv

Открывает отчёт в браузере автоматически.
"""

from __future__ import annotations

import sys
import json
import webbrowser
from pathlib import Path
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import neurokit2 as nk
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from hrv_interpret import (
    ALL as INTERP_SPECS, NEUROKIT_TO_SPEC, ARTIFACTS, STRESS_INDEX,
    overall_summary, MEDICAL_NOTES, PRIMARY_METRICS,
    biofeedback_recommendations, overall_picture, stability_analysis,
)
from hrv_calculator import HRVCalculator


def render_bullet(spec, value: Optional[float]) -> str:
    """Горизонтальная шкала с зонами + точка значения. Возвращает HTML.

    Описание метрики идёт СВЕРХУ шкалы (а не под ней), под шкалой — твоё значение
    + что значит «В нейроассессменте» (контрастно, не бледно).
    """
    if not spec or not spec.ranges:
        return ""
    ranges = spec.ranges
    lo_total = ranges[0][0]
    hi_total = ranges[-1][1]
    if hi_total >= 9999:
        norm_hi = max((r[1] for r in ranges if "норма" in r[2].lower() or "баланс" in r[2].lower()),
                      default=ranges[-2][1] if len(ranges) > 1 else 100)
        hi_total = max(norm_hi * 2.5, ranges[-2][1] * 1.4 if len(ranges) > 1 else 100)
    span = max(hi_total - lo_total, 1e-9)

    def pct_pos(v):
        return max(0.0, min(100.0, 100.0 * (v - lo_total) / span))

    def zone_color(label: str) -> str:
        lab = label.lower()
        if "норма" in lab or "баланс" in lab:
            return "#a8d5ad"  # зелёный — норма
        if "критич" in lab or "очень" in lab or "стресс" in lab or "перенапря" in lab:
            return "#f4a8a8"  # красный — критическое
        if "ваготон" in lab:
            return "#c4d8f0"  # голубой — ваготония / парасимпатический сдвиг
        if "снс" in lab or "симпат" in lab:
            return "#ffd2a8"  # тёплый оранжевый — симпатический
        if "пнс" in lab or "парасимпат" in lab:
            return "#c4d8f0"  # голубой
        return "#ffd699"  # жёлтый — отклонение/умеренное

    segments_html = ""
    zone_labels_html = ""
    for lo, hi, label, meaning in ranges:
        hi_clip = min(hi, hi_total)
        if hi_clip <= lo:
            continue
        left = pct_pos(lo)
        width = pct_pos(hi_clip) - left
        color = zone_color(label)
        segments_html += (
            f"<div class='bullet-seg' style='left:{left:.2f}%;width:{width:.2f}%;background:{color}' "
            f"title='{label}: {meaning}'></div>"
        )

    marker_html = ""
    label_str = ""
    if value is not None and value == value:
        pos = pct_pos(value)
        marker_html = f"<div class='bullet-marker' style='left:{pos:.2f}%'></div>"
        lab, mean = spec.interpret(value)
        label_str = (
            f"<div class='bullet-label'>"
            f"<b style='font-size:18px'>{value:.2f}</b> {spec.unit} → "
            f"<span style='color:#0a4ea3;font-weight:600'>{lab}</span> — {mean}"
            f"</div>"
        )

    # Подписи под шкалой: первая и последняя метка диапазонов
    first_label = ranges[0][2]
    last_label = ranges[-1][2]
    zone_labels_html = (
        f"<div class='bullet-zone-labels'>"
        f"<span>← {first_label}</span>"
        f"<span>{last_label} →</span>"
        f"</div>"
    )

    return (
        f"<div class='bullet-row'>"
        f"<div class='bullet-name'>{spec.name}</div>"
        f"<div class='bullet-descr-top'>{spec.description}</div>"
        f"<div class='bullet-bar'>{segments_html}{marker_html}</div>"
        f"{zone_labels_html}"
        f"{label_str}"
        f"<div class='bullet-app'><strong>В нейроассессменте:</strong> {spec.applicability}</div>"
        f"</div>"
    )


def render_stability_block(stability: dict) -> str:
    if not stability or not stability.get("metrics"):
        return ""
    m = stability["metrics"]
    interp = stability.get("interpretation", [])
    rows = ""
    for name in ("HR", "MeanRR", "RMSSD", "SDNN"):
        if name not in m:
            continue
        st = m[name]
        rows += (
            f"<tr><td><b>{name}</b></td><td>{st['mean']}</td>"
            f"<td>{st['min']} – {st['max']}</td>"
            f"<td>{st['cv_pct']}%</td><td>{st['n_windows']}</td></tr>"
        )
    interp_html = "<ul>" + "".join(f"<li>{l}</li>" for l in interp) + "</ul>"
    return (
        f"<div class='stability-box'>"
        f"<div class='label'>Что в твоём профиле стабильно, а что плавает</div>"
        f"<p style='margin:0 0 8px'>Сессия разбита на 5-минутные окна с шагом 1 минута, в каждом окне посчитаны "
        f"метрики, и CV — насколько они колеблются. Маленький CV (&lt;5%) = это твоя устойчивая характеристика; "
        f"большой CV (&gt;15%) = метрика реагирует на текущее состояние (нагрузка, дыхание, эмоции).</p>"
        f"<table style='margin-top:6px;font-size:13px;border-collapse:collapse'>"
        f"<thead><tr><th style='text-align:left;padding:4px 10px'>метрика</th>"
        f"<th style='text-align:left;padding:4px 10px'>среднее</th>"
        f"<th style='text-align:left;padding:4px 10px'>мин–макс</th>"
        f"<th style='text-align:left;padding:4px 10px'>CV</th>"
        f"<th style='text-align:left;padding:4px 10px'>окон</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"{interp_html}"
        f"</div>"
    )


LEGEND_HTML = """
<div class="legend-box">
  <div style="font-weight:600;font-size:14px;margin-bottom:6px">Как читать шкалы ниже</div>
  <div style="font-size:13px;color:#444;line-height:1.5">
    Под каждой метрикой — горизонтальная шкала, разбитая на цветные зоны (диапазоны нормы по справочнику для нейроассессмента).
    Тёмно-синяя метка <b>ВЫ</b> на шкале — твоё значение в этой сессии.
  </div>
  <div class="legend-row">
    <div class="legend-item"><div class="legend-swatch" style="background:#a8d5ad"></div>Норма / баланс</div>
    <div class="legend-item"><div class="legend-swatch" style="background:#ffd699"></div>Отклонение (внимание)</div>
    <div class="legend-item"><div class="legend-swatch" style="background:#ffd2a8"></div>Симпатический сдвиг</div>
    <div class="legend-item"><div class="legend-swatch" style="background:#c4d8f0"></div>Парасимпатический сдвиг / ваготония</div>
    <div class="legend-item"><div class="legend-swatch" style="background:#f4a8a8"></div>Критическое значение</div>
    <div class="legend-item"><div class="legend-marker"></div>Твоё значение</div>
  </div>
</div>
"""


AXIS_LABELS = {
    "RD": ("Готовность", "комплексная готовность к нагрузкам"),
    "SR": ("Стрессоустойчивость", "способность сохранять эффективность под давлением"),
    "AD": ("Адаптивность", "скорость подстройки регуляции к новым условиям"),
    "FL": ("Гибкость", "способность переключаться, нервная пластичность"),
    "RC": ("Восстановление", "парасимпатическая активность, скорость отдыха"),
    "EN": ("Выносливость", "устойчивость к длительным нагрузкам"),
    "BL": ("Вегетативный баланс", "соотношение симпатической и парасимпатической активности"),
}


def axis_label(score: float | None) -> str:
    if score is None:
        return "—"
    if score >= 80: return "отличный"
    if score >= 60: return "хороший"
    if score >= 40: return "средний"
    if score >= 20: return "сниженный"
    return "критически низкий"


def load_rr_csv(path: Path) -> tuple[np.ndarray, pd.DataFrame]:
    df = pd.read_csv(path)
    if "RR_Interval_ms" not in df.columns:
        raise ValueError(f"В CSV нет колонки RR_Interval_ms (есть: {list(df.columns)})")
    rr = df["RR_Interval_ms"].to_numpy(dtype=float)
    rr = rr[~np.isnan(rr)]
    rr = rr[rr > 0]
    return rr, df


def to_peaks(rr_ms: np.ndarray, sampling_rate: int = 1000) -> np.ndarray:
    cumulative_ms = np.cumsum(rr_ms)
    peaks_samples = (cumulative_ms * sampling_rate / 1000.0).astype(int)
    return peaks_samples


def clean_with_kubios(rr_ms: np.ndarray, sampling_rate: int = 1000) -> tuple[np.ndarray, dict]:
    peaks = to_peaks(rr_ms, sampling_rate=sampling_rate)
    info, _peaks_corrected = nk.signal_fixpeaks(
        peaks=peaks,
        sampling_rate=sampling_rate,
        iterative=True,
        method="kubios",
    )
    rr_clean_sec = info.get("rr")
    if rr_clean_sec is None or len(rr_clean_sec) == 0:
        rr_clean = rr_ms.copy()
    else:
        rr_clean = np.asarray(rr_clean_sec, dtype=float) * 1000.0
    return rr_clean, info


def extra_threshold_filter(rr_ms: np.ndarray, min_ms: int = 300, max_ms: int = 2000, max_jump_pct: float = 25.0) -> tuple[np.ndarray, np.ndarray]:
    keep = (rr_ms >= min_ms) & (rr_ms <= max_ms)
    rr = rr_ms.copy()
    if len(rr) > 1:
        diffs = np.abs(np.diff(rr)) / np.maximum(rr[:-1], 1)
        jump_mask = np.concatenate([[False], diffs > (max_jump_pct / 100.0)])
        keep = keep & ~jump_mask
    return rr[keep], keep


def hrv_full(rr_ms: np.ndarray, sampling_rate: int = 1000) -> pd.DataFrame:
    peaks = to_peaks(rr_ms, sampling_rate=sampling_rate)
    metrics = nk.hrv(peaks, sampling_rate=sampling_rate, show=False)
    return metrics


def rolling_metric(rr_ms: np.ndarray, window_sec: int = 300, step_sec: int = 60) -> pd.DataFrame:
    cum_sec = np.cumsum(rr_ms) / 1000.0
    if len(cum_sec) == 0:
        return pd.DataFrame()
    total = cum_sec[-1]
    out = []
    t_start = 0.0
    while t_start + window_sec <= total:
        t_end = t_start + window_sec
        mask = (cum_sec >= t_start) & (cum_sec < t_end)
        rr_win = rr_ms[mask]
        if len(rr_win) >= 30:
            rmssd = float(np.sqrt(np.mean(np.diff(rr_win) ** 2)))
            sdnn = float(np.std(rr_win, ddof=1))
            mean_hr = float(60000.0 / np.mean(rr_win))
            out.append({"t_mid_sec": t_start + window_sec / 2, "RMSSD": rmssd, "SDNN": sdnn, "HR": mean_hr})
        t_start += step_sec
    return pd.DataFrame(out)


def stress_index_baevsky(rr_ms: np.ndarray) -> float:
    if len(rr_ms) < 10:
        return float("nan")
    rr_sec = rr_ms / 1000.0
    bin_width = 0.05
    bins = np.arange(rr_sec.min(), rr_sec.max() + bin_width, bin_width)
    if len(bins) < 2:
        return float("nan")
    hist, edges = np.histogram(rr_sec, bins=bins)
    mode_idx = int(np.argmax(hist))
    mo = (edges[mode_idx] + edges[mode_idx + 1]) / 2
    amo = hist[mode_idx] / len(rr_sec) * 100.0
    mxdmn = float(rr_sec.max() - rr_sec.min())
    if mo == 0 or mxdmn == 0:
        return float("nan")
    return float(amo / (2 * mo * mxdmn))


def render_html(
    src_path: Path,
    rr_raw: np.ndarray,
    rr_clean: np.ndarray,
    fix_info: dict,
    metrics_raw: pd.DataFrame,
    metrics_clean: pd.DataFrame,
    rolling: pd.DataFrame,
    out_path: Path,
    n_pre_filter_dropped: int = 0,
    axis_scores: dict | None = None,
    overall_score: float | None = None,
    state_text: str | None = None,
    project_metrics: dict | None = None,
    freq_valid: bool = True,
):
    fig = make_subplots(
        rows=4,
        cols=1,
        subplot_titles=(
            "Тахограмма: исходные RR (красные точки = найденные артефакты)",
            "Тахограмма: после артефакт-коррекции Kubios / Lipponen-Tarvainen",
            "Скользящий RMSSD (окно 5 мин, шаг 1 мин)",
            "Скользящий SDNN (окно 5 мин, шаг 1 мин)",
        ),
        vertical_spacing=0.08,
    )

    t_raw = np.cumsum(rr_raw) / 1000.0
    t_clean = np.cumsum(rr_clean) / 1000.0

    artifacts = set()
    for key in ("ectopic", "extra", "missed", "longshort"):
        idx = fix_info.get(key, [])
        if isinstance(idx, (list, np.ndarray)):
            artifacts.update(int(i) for i in idx)

    is_artifact = np.array([i in artifacts for i in range(len(rr_raw))])

    fig.add_trace(
        go.Scatter(x=t_raw, y=rr_raw, mode="lines", name="RR raw", line=dict(width=1, color="#1f77b4")),
        row=1, col=1,
    )
    if is_artifact.any():
        fig.add_trace(
            go.Scatter(
                x=t_raw[is_artifact],
                y=rr_raw[is_artifact],
                mode="markers",
                name="артефакты",
                marker=dict(color="red", size=6, symbol="x"),
            ),
            row=1, col=1,
        )

    fig.add_trace(
        go.Scatter(x=t_clean, y=rr_clean, mode="lines", name="RR clean", line=dict(width=1, color="#2ca02c")),
        row=2, col=1,
    )

    if not rolling.empty:
        fig.add_trace(
            go.Scatter(x=rolling["t_mid_sec"] / 60.0, y=rolling["RMSSD"], mode="lines+markers", name="RMSSD"),
            row=3, col=1,
        )
        fig.add_trace(
            go.Scatter(x=rolling["t_mid_sec"] / 60.0, y=rolling["SDNN"], mode="lines+markers", name="SDNN"),
            row=4, col=1,
        )

    fig.update_xaxes(title_text="время, сек", row=1, col=1)
    fig.update_xaxes(title_text="время, сек", row=2, col=1)
    fig.update_xaxes(title_text="время, мин", row=3, col=1)
    fig.update_xaxes(title_text="время, мин", row=4, col=1)
    fig.update_yaxes(title_text="RR, мс", row=1, col=1)
    fig.update_yaxes(title_text="RR, мс", row=2, col=1)
    fig.update_yaxes(title_text="мс", row=3, col=1)
    fig.update_yaxes(title_text="мс", row=4, col=1)
    fig.update_layout(height=1100, showlegend=True, template="plotly_white", title_text="")

    plot_html = fig.to_html(include_plotlyjs="cdn", full_html=False)

    n_total = len(rr_raw)
    n_kubios = len(artifacts)
    n_artifacts = n_pre_filter_dropped + n_kubios
    pct_artifacts = 100.0 * n_artifacts / n_total if n_total else 0.0

    duration_min_raw = float(np.sum(rr_raw) / 1000.0 / 60.0)
    duration_min_clean = float(np.sum(rr_clean) / 1000.0 / 60.0)
    mean_hr_raw = float(60000.0 / np.mean(rr_raw)) if n_total else float("nan")
    mean_hr_clean = float(60000.0 / np.mean(rr_clean)) if len(rr_clean) else float("nan")

    stress_raw = stress_index_baevsky(rr_raw)
    stress_clean = stress_index_baevsky(rr_clean)

    def fmt(v):
        if isinstance(v, (int, np.integer)):
            return f"{int(v)}"
        if isinstance(v, (float, np.floating)):
            if np.isnan(v):
                return "—"
            if abs(v) >= 100:
                return f"{v:.1f}"
            if abs(v) >= 1:
                return f"{v:.2f}"
            return f"{v:.3f}"
        return str(v)

    def _as_float(v):
        try:
            f = float(v)
            if f != f:  # NaN
                return None
            return f
        except Exception:
            return None

    raw_cols = set(metrics_raw.columns) if not metrics_raw.empty else set()
    clean_cols = set(metrics_clean.columns) if not metrics_clean.empty else set()
    all_cols = sorted(raw_cols | clean_cols)

    metric_rows = []
    unexplained_rows = []  # технические/исследовательские метрики без расшифровки
    for c in all_cols:
        v_raw = metrics_raw[c].iloc[0] if c in raw_cols else float("nan")
        v_clean = metrics_clean[c].iloc[0] if c in clean_cols else float("nan")
        spec_key = NEUROKIT_TO_SPEC.get(c)
        explain = INTERP_SPECS.get(spec_key) if spec_key else None
        if explain:
            label, meaning = explain.interpret(_as_float(v_clean))
            human_name = (
                f"<b style='color:#1a1a1a'>{explain.name}</b>"
                f"<br><span style='color:#555;font-size:12px'>{explain.description[:140]}{'…' if len(explain.description) > 140 else ''}</span>"
            )
            interp_html = (
                f"<span style='font-weight:600;color:#0a4ea3'>{label}</span>"
                f"<br><span style='color:#333;font-size:12px'>{meaning}</span>"
                f"<br><span style='color:#444;font-size:12px;font-style:italic'>В нейроассессменте: {explain.applicability}</span>"
            )
            metric_rows.append((human_name, fmt(v_raw), fmt(v_clean), interp_html))
        else:
            unexplained_rows.append((c, fmt(v_raw), fmt(v_clean)))

    rows_html = "\n".join(
        f"<tr><td>{name}</td><td style='text-align:right'>{r}</td><td style='text-align:right'>{c}</td><td>{interp}</td></tr>"
        for name, r, c, interp in metric_rows
    )

    # Сводный человеческий вывод (новая модель)
    project_metrics_for_summary = project_metrics or {}
    summary_lines = overall_summary(
        project_metrics=project_metrics_for_summary,
        axis_scores=axis_scores,
        overall_score=overall_score,
        state_text=state_text,
        artifacts_pct=pct_artifacts,
    )
    summary_html = "<ul style='line-height:1.6'>" + "".join(f"<li>{l}</li>" for l in summary_lines) + "</ul>"

    # Комплексная картина одним абзацем
    picture_text = overall_picture(project_metrics_for_summary, axis_scores)

    # Биофидбэк-рекомендации «что попробовать сейчас»
    recs = biofeedback_recommendations(project_metrics_for_summary, artifacts_pct=pct_artifacts)
    rec_color = {"good": "#cce8d0", "info": "#e6efff", "action": "#fff3e0", "alert": "#f5c6c6"}
    rec_icon = {"good": "✓", "info": "ℹ", "action": "→", "alert": "⚠"}
    recs_html = ""
    for r in recs:
        col = rec_color.get(r["level"], "#eef2f5")
        ic = rec_icon.get(r["level"], "•")
        recs_html += (
            f"<div class='rec' style='background:{col}'>"
            f"<div class='rec-text'><b>{ic}</b> {r['text']}</div>"
            f"<div class='rec-why'>{r['why']}</div>"
            f"</div>"
        )

    # Анализ стабильности — какие метрики «гуляют», какие у тебя характеристика
    stability = stability_analysis(rr_clean.tolist(), window_sec=300, step_sec=60)

    # Bullet-charts для всех первичных метрик (по справочнику)
    project_for_bullets = {**project_metrics_for_summary}
    # Подменяем стандартные имена на ключи интерпретатора
    aliases = {
        "rmssd": project_for_bullets.get("rmssd"),
        "sdnn": project_for_bullets.get("sdnn"),
        "pnn50": project_for_bullets.get("pnn50"),
        "mean_rr": project_for_bullets.get("mean_rr"),
        "sd1": project_for_bullets.get("sd1"),
        "sd2": project_for_bullets.get("sd2"),
        "sd1_sd2": project_for_bullets.get("sd1_sd2_ratio"),
        "stress_index": project_for_bullets.get("stress_index"),
        "lf_hf_ratio": project_for_bullets.get("lf_hf_ratio"),
        "lf_nu": project_for_bullets.get("lf_nu"),
        "hf_nu": project_for_bullets.get("hf_nu"),
        "lf_power": project_for_bullets.get("lf_power"),
        "hf_power": project_for_bullets.get("hf_power"),
        "total_power": project_for_bullets.get("total_power"),
    }
    bullets_html = ""
    for key in PRIMARY_METRICS:
        spec = INTERP_SPECS.get(key)
        if not spec:
            continue
        val = aliases.get(key)
        # Если не нашли — попробуем то же имя в project_metrics
        if val is None:
            val = project_metrics_for_summary.get(key)
        bullets_html += render_bullet(spec, val if (val is not None and val == val) else None)

    art_label, art_meaning = ARTIFACTS.interpret(pct_artifacts)
    bav_label, bav_meaning = STRESS_INDEX.interpret(stress_clean)

    # Quality gate: при сильно зашумлённых данных не показывать радар/баллы как факт
    quality_unreliable = pct_artifacts > 20
    quality_caveat = 10 < pct_artifacts <= 20

    # Блок 7 осей + паутинка
    axes_html = ""
    radar_html = ""
    if axis_scores and not quality_unreliable:
        axes_html = "<div class='axes-grid'>"
        for code, (rus_name, descr) in AXIS_LABELS.items():
            val = axis_scores.get(code)
            lab = axis_label(val)
            display = f"{int(val)}%" if val is not None else "н/д"
            bar_pct = int(val) if val is not None else 0
            color = "#1a73e8" if (val is not None and val >= 60) else ("#f57c00" if (val is not None and val >= 40) else "#d32f2f")
            axes_html += (
                f"<div class='axis-card'>"
                f"<div class='axis-name'>{rus_name} <span class='axis-code'>({code})</span></div>"
                f"<div class='axis-val'>{display}</div>"
                f"<div class='axis-bar'><div class='axis-bar-fill' style='width:{bar_pct}%;background:{color}'></div></div>"
                f"<div class='axis-label'>{lab}</div>"
                f"<div class='axis-descr'>{descr}</div>"
                f"</div>"
            )
        axes_html += "</div>"

        # Паутинка plotly
        codes = list(AXIS_LABELS.keys())
        rus_names = [AXIS_LABELS[c][0] for c in codes]
        vals = [axis_scores.get(c) or 0 for c in codes]
        radar = go.Figure(go.Scatterpolar(
            r=vals + [vals[0]],
            theta=rus_names + [rus_names[0]],
            fill="toself",
            line=dict(color="#1a73e8"),
            fillcolor="rgba(26,115,232,0.2)",
            name="Профиль",
        ))
        # Эталонное «оптимальное» 70% для сравнения
        radar.add_trace(go.Scatterpolar(
            r=[70] * len(codes) + [70],
            theta=rus_names + [rus_names[0]],
            line=dict(color="#aaa", dash="dot"),
            fill=None,
            name="Хорошее (70%)",
        ))
        radar.update_layout(
            polar=dict(radialaxis=dict(visible=True, range=[0, 100])),
            showlegend=True,
            height=420,
            margin=dict(t=20, b=20, l=20, r=20),
        )
        radar_html = radar.to_html(include_plotlyjs="cdn", full_html=False)

    state_block = ""
    if quality_unreliable:
        state_block = (
            "<div class='state-block' style='background:#fff3e6;border-color:#f0a020'>"
            "<div style='font-size:48px;line-height:1'>⚠</div>"
            "<div class='state-text'>Запись непригодна</div>"
            f"<div class='state-hint'>{pct_artifacts:.0f}% артефактов — баллы и состояние не считаются. "
            "Перепиши: сидя, рука неподвижна, ремешок плотнее.</div>"
            "</div>"
        )
    elif overall_score is not None:
        state_color = "#1a73e8" if overall_score >= 60 else ("#f57c00" if overall_score >= 40 else "#d32f2f")
        caveat = " (с оговоркой — сигнал шумноват)" if quality_caveat else ""
        state_block = (
            f"<div class='state-block'>"
            f"<div class='state-score' style='color:{state_color}'>{int(overall_score)}<span class='of'>/100</span></div>"
            f"<div class='state-text'>{state_text}</div>"
            f"<div class='state-hint'>Общий балл функционального состояния{caveat}</div>"
            f"</div>"
        )

    summary = f"""
    <div class="summary">
      <div class="card"><div class="k">Длительность</div><div class="v">{duration_min_clean:.1f} мин</div><div class="hint">после очистки (было {duration_min_raw:.1f})</div></div>
      <div class="card"><div class="k">RR-интервалов</div><div class="v">{n_total}</div><div class="hint">{n_total - n_artifacts} оставлено, {n_artifacts} отброшено</div></div>
      <div class="card"><div class="k">Качество сигнала</div><div class="v">{art_label}</div><div class="hint">{pct_artifacts:.1f}% артефактов — {art_meaning}</div></div>
      <div class="card"><div class="k">Средний пульс</div><div class="v">{mean_hr_clean:.0f} bpm</div><div class="hint">после очистки (сырое: {mean_hr_raw:.0f})</div></div>
      <div class="card"><div class="k">Индекс Баевского</div><div class="v">{fmt(stress_clean)}</div><div class="hint">{bav_label} — {bav_meaning}</div></div>
    </div>
    """

    html = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>HRV-отчёт — {src_path.name}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 1200px; margin: 24px auto; padding: 0 16px; color: #222; }}
  h1 {{ font-weight: 600; margin-bottom: 4px; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 24px; }}
  .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .card {{ background: #f4f6f8; border-radius: 8px; padding: 12px 14px; }}
  .card .k {{ font-size: 12px; color: #666; text-transform: uppercase; letter-spacing: .04em; }}
  .card .v {{ font-size: 20px; font-weight: 600; margin-top: 4px; }}
  .card .hint {{ font-size: 11px; color: #666; margin-top: 6px; line-height: 1.4; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 20px; font-size: 13px; }}
  th, td {{ border: 1px solid #e3e6ea; padding: 8px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #f0f3f6; font-weight: 600; }}
  tr:nth-child(even) td {{ background: #fafbfc; }}
  .note {{ background: #fff8e6; border-left: 3px solid #f0c020; padding: 10px 14px; margin: 16px 0; font-size: 13px; }}
  .verdict {{ background: #eef5ff; border-left: 3px solid #1a73e8; padding: 14px 18px; margin: 20px 0; font-size: 14px; }}
  .verdict h2 {{ margin: 0 0 8px; font-size: 16px; }}
  .top-row {{ display: grid; grid-template-columns: 1fr 1.4fr; gap: 20px; align-items: start; margin-bottom: 24px; }}
  @media (max-width: 800px) {{ .top-row {{ grid-template-columns: 1fr; }} }}
  .state-block {{ background: #fafbfc; border: 1px solid #e3e6ea; border-radius: 10px; padding: 24px; text-align: center; }}
  .state-score {{ font-size: 64px; font-weight: 700; line-height: 1; }}
  .state-score .of {{ font-size: 24px; color: #999; font-weight: 400; }}
  .state-text {{ font-size: 20px; font-weight: 600; margin-top: 8px; }}
  .state-hint {{ font-size: 12px; color: #777; margin-top: 8px; }}
  .axes-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }}
  .axis-card {{ background: #fafbfc; border: 1px solid #e8eaed; border-radius: 8px; padding: 12px 14px; }}
  .axis-name {{ font-size: 13px; font-weight: 600; }}
  .axis-code {{ color: #999; font-weight: 400; font-size: 11px; }}
  .axis-val {{ font-size: 26px; font-weight: 600; margin-top: 4px; }}
  .axis-bar {{ height: 6px; background: #eaecef; border-radius: 4px; margin-top: 6px; overflow: hidden; }}
  .axis-bar-fill {{ height: 100%; transition: width .3s; }}
  .axis-label {{ font-size: 12px; color: #444; margin-top: 6px; font-weight: 500; }}
  .axis-descr {{ font-size: 11px; color: #888; margin-top: 4px; line-height: 1.35; }}
  .picture {{ background: #fafbfc; border: 1px solid #e3e6ea; border-radius: 8px; padding: 14px 18px; margin: 16px 0; font-size: 15px; line-height: 1.55; }}
  .picture .label {{ font-size: 11px; color: #999; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 6px; }}
  .recs {{ margin: 16px 0; }}
  .rec {{ border-radius: 8px; padding: 12px 16px; margin-bottom: 8px; }}
  .rec-text {{ font-size: 14px; line-height: 1.5; }}
  .rec-why {{ font-size: 11px; color: #666; margin-top: 4px; font-style: italic; }}
  .bullet-section {{ margin-top: 32px; }}
  .bullet-row {{ margin-bottom: 26px; padding: 14px; background: white; border: 1px solid #e8eaed; border-radius: 8px; }}
  .bullet-name {{ font-size: 15px; font-weight: 600; margin-bottom: 4px; color: #1a1a1a; }}
  .bullet-descr-top {{ font-size: 12px; color: #444; line-height: 1.5; margin-bottom: 10px; }}
  .bullet-bar {{ position: relative; height: 22px; background: #f0f2f4; border-radius: 4px; overflow: visible; }}
  .bullet-seg {{ position: absolute; top: 0; height: 100%; }}
  .bullet-marker {{ position: absolute; top: -4px; bottom: -4px; width: 4px; background: #0a4ea3; border-radius: 2px; box-shadow: 0 0 0 2px white, 0 0 4px rgba(0,0,0,0.35); transform: translateX(-2px); z-index: 2; }}
  .bullet-marker::after {{ content: 'ВЫ'; position: absolute; bottom: -22px; left: -14px; font-size: 10px; font-weight: 700; color: #0a4ea3; letter-spacing: .5px; }}
  .bullet-zone-labels {{ display: flex; justify-content: space-between; font-size: 10px; color: #666; margin-top: 4px; padding: 0 2px; }}
  .bullet-label {{ font-size: 14px; margin-top: 18px; color: #1a1a1a; }}
  .bullet-app {{ font-size: 13px; color: #333; margin-top: 8px; line-height: 1.5; padding: 8px 12px; background: #f5f8ff; border-left: 3px solid #1a73e8; border-radius: 0 4px 4px 0; }}
  .bullet-app strong {{ color: #1a73e8; }}
  .legend-box {{ background: #fafbfc; border: 2px solid #1a73e8; border-radius: 8px; padding: 16px 20px; margin-bottom: 20px; }}
  .legend-row {{ display: flex; gap: 24px; flex-wrap: wrap; align-items: center; margin-top: 8px; }}
  .legend-item {{ display: flex; align-items: center; gap: 8px; font-size: 13px; color: #222; }}
  .legend-swatch {{ width: 24px; height: 14px; border-radius: 3px; border: 1px solid rgba(0,0,0,0.1); }}
  .legend-marker {{ width: 4px; height: 18px; background: #0a4ea3; border-radius: 2px; box-shadow: 0 0 0 2px white, 0 0 4px rgba(0,0,0,0.35); }}
  .stability-box {{ background: #fafbfc; border: 1px solid #e3e6ea; border-radius: 8px; padding: 14px 18px; margin: 16px 0; font-size: 14px; line-height: 1.55; color: #222; }}
  .stability-box .label {{ font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 8px; }}
  .stability-box ul {{ margin: 6px 0 0 0; padding-left: 20px; }}
</style>
</head>
<body>
  <h1>HRV-отчёт сессии</h1>
  <div class="meta">Источник: <code>{src_path}</code> · Сформировано {datetime.now().strftime('%Y-%m-%d %H:%M')} · NeuroKit2 {nk.__version__}</div>

  <div class="top-row">
    {state_block}
    <div>{radar_html}</div>
  </div>

  {axes_html}

  <div class="picture">
    <div class="label">Что сейчас по совокупности метрик</div>
    {picture_text}
  </div>

  <div class="recs">
    <div style="font-size:13px;color:#666;margin-bottom:8px;text-transform:uppercase;letter-spacing:.04em">Что можно попробовать</div>
    {recs_html}
  </div>

  <div class="verdict">
    <h2>Краткая сводка</h2>
    {summary_html}
    {('<p style="margin:8px 0 0;color:#888;font-size:12px">Частотные метрики (LF/HF) считались на короткой записи — приблизительные.</p>' if not freq_valid else '')}
  </div>

  <div class="note">
    Колонка «сырое» = метрики на RR-интервалах как пришли из Polar.
    Колонка «после коррекции» = после артефакт-коррекции методом Kubios / Lipponen-Tarvainen.
    Если значения сильно расходятся — это и есть та неточность, из-за которой раньше плыл биологический возраст.
    Доверять надо колонке <b>после коррекции</b>.
  </div>

  {summary}

  {plot_html}

  <h2 style="margin-top:32px;">Основные показатели — шкалы с зонами норм</h2>
  {LEGEND_HTML}
  <div class="bullet-section">{bullets_html}</div>

  {render_stability_block(stability)}

  <details style="margin-top:32px">
    <summary style="cursor:pointer;font-weight:500;font-size:14px;color:#555">Описанные метрики ({len(metric_rows)}) — таблица с расшифровкой</summary>
    <table style="margin-top:12px">
      <thead><tr><th style="width:25%">метрика</th><th style="width:10%;text-align:right">сырое</th><th style="width:15%;text-align:right">после коррекции</th><th style="width:50%">что это значит</th></tr></thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </details>

  {('<details style="margin-top:12px"><summary style="cursor:pointer;font-weight:500;font-size:13px;color:#888">Технические/исследовательские метрики без расшифровки (' + str(len(unexplained_rows)) + ') — для экспорта и исследований</summary><table style="margin-top:12px;font-size:12px"><thead><tr><th>код NeuroKit</th><th style="text-align:right">сырое</th><th style="text-align:right">после коррекции</th></tr></thead><tbody>' + ''.join(f"<tr><td><code>{n}</code></td><td style='text-align:right'>{r}</td><td style='text-align:right'>{c}</td></tr>" for n, r, c in unexplained_rows) + '</tbody></table></details>') if unexplained_rows else ''}

  <details style="margin-top:24px">
    <summary style="cursor:pointer;font-weight:500;font-size:14px;color:#555">Медицинские факторы, влияющие на HRV</summary>
    <pre style="margin-top:12px;background:#fafbfc;padding:12px 16px;border-radius:6px;font-size:12px;line-height:1.6;font-family:inherit;white-space:pre-wrap;color:#444">{MEDICAL_NOTES}</pre>
  </details>
</body>
</html>
"""

    out_path.write_text(html, encoding="utf-8")
    return out_path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: analyze_session.py <path/to/rr.csv>", file=sys.stderr)
        return 2
    src = Path(argv[1]).resolve()
    if not src.exists():
        print(f"Файл не найден: {src}", file=sys.stderr)
        return 2

    rr_raw, _df = load_rr_csv(src)
    print(f"Загружено RR-интервалов: {len(rr_raw)}, длительность ≈ {np.sum(rr_raw)/1000.0/60.0:.1f} мин")

    rr_thresh, _keep = extra_threshold_filter(rr_raw)
    print(f"После порогового фильтра (300-2000мс + jump<25%): {len(rr_thresh)} (выкинуто {len(rr_raw) - len(rr_thresh)})")

    rr_clean, info = clean_with_kubios(rr_thresh)
    n_artifacts_kubios = sum(
        len(info.get(k, [])) for k in ("ectopic", "extra", "missed", "longshort")
        if isinstance(info.get(k, []), (list, np.ndarray))
    )
    n_artifacts = (len(rr_raw) - len(rr_thresh)) + n_artifacts_kubios
    print(f"Артефактов всего: {n_artifacts} (порог: {len(rr_raw) - len(rr_thresh)}, Kubios: {n_artifacts_kubios}) = {100.0*n_artifacts/len(rr_raw):.1f}%")
    print(f"После очистки RR-интервалов: {len(rr_clean)}")

    if len(rr_clean) < 10:
        print("ВНИМАНИЕ: после очистки осталось слишком мало интервалов, метрики не считаются", file=sys.stderr)
        rr_clean = rr_thresh if len(rr_thresh) >= 10 else rr_raw

    metrics_raw = hrv_full(rr_raw) if len(rr_raw) >= 10 else pd.DataFrame()
    metrics_clean = hrv_full(rr_clean) if len(rr_clean) >= 10 else pd.DataFrame()
    rolling = rolling_metric(rr_clean, window_sec=60, step_sec=20)  # короткое окно для коротких записей

    # 7 осей паутинки через готовый HRVCalculator
    hr_from_rr_clean = (60000.0 / rr_clean).tolist()
    calc = HRVCalculator(hr_data=hr_from_rr_clean, rr_intervals=rr_clean.tolist())
    project_metrics = calc.calculate_all_metrics()
    freq_valid = len(rr_clean) >= 300  # ~5 минут при HR 60
    axis_scores = calc.calculate_axis_scores(project_metrics, freq_valid=freq_valid)
    overall_score = calc.calculate_overall_score(axis_scores)
    state_text = calc.get_state_text(overall_score)

    out_dir = src.parent.parent / "dashboards"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"report_{src.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    render_html(src, rr_raw, rr_clean, info, metrics_raw, metrics_clean, rolling, out_path,
                n_pre_filter_dropped=len(rr_raw) - len(rr_thresh),
                axis_scores=axis_scores, overall_score=overall_score, state_text=state_text,
                project_metrics=project_metrics, freq_valid=freq_valid)
    print(f"Отчёт сохранён: {out_path}")

    summary = {
        "n_raw": int(len(rr_raw)),
        "n_clean": int(len(rr_clean)),
        "artifacts_pct": round(100.0 * n_artifacts / len(rr_raw), 2),
        "hr_mean_raw": round(float(60000.0 / np.mean(rr_raw)), 1),
        "hr_mean_clean": round(float(60000.0 / np.mean(rr_clean)), 1),
    }
    print("Сводка:", json.dumps(summary, ensure_ascii=False))

    try:
        webbrowser.open(out_path.as_uri())
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
