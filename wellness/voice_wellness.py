#!/usr/bin/env python3
"""Голосовые биомаркеры wellness — поднимаем из уже считающейся prosody.

Кружок уже меряет (audio_prosody.analyze): jitter, диапазон/вариативность питча,
динамику громкости (+ eGeMAPS jitter/shimmer/HNR, если есть openSMILE). Сводим в
три индекса состояния: вокальный стресс, вокальная усталость, витальность голоса.

Маркеры/тренды, НЕ диагноз.
"""
from __future__ import annotations


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def analyze(prosody: dict) -> dict:
    if not prosody or not prosody.get("available"):
        return {"available": False, "reason": "нет голосового сигнала"}

    def g(k, d=None):
        v = prosody.get(k)
        return v if v is not None else d

    pitch_mean = g("pitch_mean_hz", 0)
    pitch_range_st = g("pitch_iqr_semitone", g("pitch_range_hz", 0) / 10 if g("pitch_range_hz") else 0)
    jitter = g("pitch_jitter_pct", g("jitter_local_egemaps", 0) and g("jitter_local_egemaps") * 100)
    variability = g("pitch_variability_pct", 0)
    loud_mean = g("loudness_mean_db", -40)
    loud_cv = g("loudness_cv", 0)
    hnr = g("hnr_db_egemaps")            # бонус, если eGeMAPS есть

    # --- вокальный стресс: дрожь голоса + высокий питч + нестабильная громкость ---
    s = [_clip((jitter or 0) / 3.0)]
    if pitch_mean:
        s.append(_clip((pitch_mean - 165) / 80.0))     # выше базового тона
    s.append(_clip((loud_cv or 0) / 0.4))
    if hnr is not None:
        s.append(_clip((10 - hnr) / 10.0))             # низкий HNR → напряжение/хрипота
    stress = round(100 * sum(s) / len(s))

    # --- вокальная усталость: плоско, тихо, монотонно ---
    f = [_clip(1 - (pitch_range_st or 0) / 5.0),        # узкий диапазон → плоско
         _clip(1 - (variability or 0) / 25.0),          # низкая вариативность → монотонно
         _clip((-25 - (loud_mean or -40)) / -20.0)]     # тише обычного → вяло
    fatigue = round(100 * sum(f) / len(f))

    # --- витальность: живость, диапазон, энергия ---
    v = [_clip((pitch_range_st or 0) / 6.0),
         _clip((variability or 0) / 30.0),
         _clip((loud_mean + 45) / 35.0)]
    vitality = round(100 * sum(v) / len(v))

    lines = []
    if stress >= 60:
        lines.append("голос напряжённый (дрожь/высокий тон)")
    if fatigue >= 60:
        lines.append("голос плоский, уставший")
    if vitality >= 65:
        lines.append("голос живой, энергичный")

    return {
        "available": True, "ok": True,
        "vocal_stress": stress,
        "vocal_fatigue": fatigue,
        "vocal_vitality": vitality,
        "egemaps_used": hnr is not None,
        "lines": lines,
        "note": "Голосовые биомаркеры (маркеры состояния, не диагноз).",
    }
