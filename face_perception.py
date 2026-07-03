#!/usr/bin/env python3
"""
Research layer: dynamic facial expressivity + social *perception* proxies.
(Копия из PUFY pufy-signal-core для live-АЦ. Чистый numpy, без LLM/API.)

Grounded in Todorov (trust/dominance axes) and Zebrowitz (face overgeneralization).
Wellness framing: "how you may be read", not "who you are".
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

HSEMO_8 = ["Anger", "Contempt", "Disgust", "Fear", "Happiness", "Neutral", "Sadness", "Surprise"]

TRUST_POS = {"Happiness", "Neutral", "Surprise"}
TRUST_NEG = {"Anger", "Fear", "Disgust", "Contempt", "Sadness"}
DOM_POS = {"Anger", "Contempt"}
DOM_NEG = {"Happiness", "Fear", "Sadness"}


def _entropy(p: np.ndarray) -> float:
    p = np.clip(p, 1e-9, 1.0)
    p = p / p.sum()
    return float(-np.sum(p * np.log(p)))


def _scores_vec(scores):
    if not scores:
        return None
    try:
        return np.array([float(scores.get(k, 0.0)) for k in HSEMO_8], dtype=float)
    except (TypeError, ValueError):
        return None


def summarize_timeline(timeline, duration_sec=None):
    usable = [f for f in timeline if f.get("scores")]
    if not usable:
        return {"available": False, "reason": "no per-frame emotion timeline",
                "guardrail": "Perception proxies, not personality. vs own baseline only."}

    dur = duration_sec
    if dur is None or dur <= 0:
        ts = [f.get("t_sec") for f in usable if isinstance(f.get("t_sec"), (int, float))]
        dur = max(ts) - min(ts) if len(ts) >= 2 else max(len(usable) * 0.5, 1.0)
    dur = max(float(dur), 0.5)

    dominants = [f.get("dominant") for f in usable if f.get("dominant")]
    switches = sum(1 for i in range(1, len(dominants)) if dominants[i] != dominants[i - 1])
    switches_per_min = round(switches / dur * 60.0, 1)

    vecs, entropies = [], []
    for f in usable:
        v = _scores_vec(f.get("scores"))
        if v is None:
            continue
        v = v / (v.sum() + 1e-9)
        vecs.append(v)
        entropies.append(_entropy(v))
    if not vecs:
        return {"available": False, "reason": "no score vectors", "guardrail": "Perception proxies only."}

    mat = np.vstack(vecs)
    mean_dist = np.mean(mat, axis=0)
    expressivity = float(np.std(mean_dist) * 100)
    if len(vecs) >= 2:
        deltas = [float(np.linalg.norm(vecs[i] - vecs[i - 1])) for i in range(1, len(vecs))]
        dynamic_var = float(np.mean(deltas) * 100)
    else:
        dynamic_var = 0.0
    stability = 1.0 - min(1.0, float(np.std(entropies)) / (float(np.mean(entropies)) + 1e-6))
    emotional_stability_pct = round(stability * 100, 1)

    dist = {HSEMO_8[i]: float(mean_dist[i]) for i in range(len(HSEMO_8))}
    trust_raw = sum(dist.get(k, 0) for k in TRUST_POS) - 0.7 * sum(dist.get(k, 0) for k in TRUST_NEG)
    dom_raw = sum(dist.get(k, 0) for k in DOM_POS) - 0.5 * sum(dist.get(k, 0) for k in DOM_NEG)

    fwhrs = [f["fwhr"] for f in usable if isinstance(f.get("fwhr"), (int, float))]
    fwhr_mean = float(np.mean(fwhrs)) if fwhrs else None
    if fwhr_mean is not None:
        dom_raw += (fwhr_mean - 0.85) * 0.15
        trust_raw += (1.0 - fwhr_mean) * 0.05

    def _squash(x):
        return round(1.0 / (1.0 + math.exp(-4.0 * x)), 3)

    perceived_trust = _squash(trust_raw - 0.15)
    perceived_dominance = _squash(dom_raw - 0.05)
    flat_read_risk = perceived_trust > 0.55 and expressivity < 8 and switches_per_min < 3

    notes = []
    if expressivity >= 12 or switches_per_min >= 8:
        notes.append("динамика мимики заметная — лицо «играет» сильнее обычного ровного кадра")
    elif expressivity < 6 and switches_per_min < 4:
        notes.append("мимика ровная — меньше переключений между выражениями")
    if perceived_trust >= 0.65:
        notes.append("по кадру может считываться открытость/мягче (не факт характера)")
    elif perceived_trust <= 0.35:
        notes.append("по кадру может считываться настороженность/дистанция (эффект света и угла)")
    if perceived_dominance >= 0.65:
        notes.append("возможное считывание собранности/жёсткости в первом впечатлении")
    if flat_read_risk:
        notes.append("риск «плоского» считывания при низкой экспрессивности — контекст важнее лица")

    return {
        "available": True,
        "guardrail": "Social perception proxies (Todorov/Zebrowitz). Not personality type.",
        "duration_sec": round(dur, 1),
        "n_frames": len(usable),
        "dynamics": {
            "emotion_switches": switches,
            "emotion_switches_per_min": switches_per_min,
            "emotional_stability_pct": emotional_stability_pct,
            "expressivity_index": round(expressivity, 2),
            "dynamic_variability": round(dynamic_var, 2),
        },
        "perception": {
            "perceived_trust": perceived_trust,
            "perceived_dominance": perceived_dominance,
            "fwhr_mean": round(fwhr_mean, 3) if fwhr_mean is not None else None,
            "mean_distribution": {k: round(v, 3) for k, v in dist.items()},
        },
        "research_notes_ru": notes,
    }
