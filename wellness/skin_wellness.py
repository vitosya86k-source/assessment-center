#!/usr/bin/env python3
"""Кожа лица → wellness-маркеры. Поднято из готового face_markers.analyze.

Кружок уже меряет: яркость кожи (L*), красноту (a*), желтизну (b*), эритему (R/G),
ровность тона, блеск, ITA (фототип), асимметрию красноты. Интерпретируем в состояние:
бледность/прилив, усталость тона, блеск/гидратация, асимметрия (флаг).

Маркеры/тренды, НЕ диагноз. Цвет лица сильно зависит от света/камеры — берём как
относительный тренд (лучше с личной нормой из истории кружков).
"""
from __future__ import annotations


def analyze(skin: dict, baseline: dict | None = None) -> dict:
    # принимаем и плоский skin-dict, и полный face_markers (вложенный 'skin')
    if skin and isinstance(skin.get("skin"), dict):
        skin = skin["skin"]
    if not skin or skin.get("skin_brightness") is None:
        return {"available": False, "reason": "нет данных кожи"}

    bright = skin.get("skin_brightness", 60)
    red = skin.get("skin_redness", 12)
    yellow = skin.get("skin_yellow", 18)
    eryth = skin.get("skin_erythema_rg", 0)
    evenness = skin.get("skin_tone_evenness", 0)
    shine = skin.get("skin_shine", 0)
    asym = skin.get("skin_redness_asym", 0)

    # относительно личной нормы, если есть
    b = baseline or {}
    d_red = red - b.get("skin_redness", red)
    d_bright = bright - b.get("skin_brightness", bright)

    lines, flags = [], []

    # бледность ↔ прилив
    if (baseline and (d_red < -4 or d_bright < -8)) or (not baseline and red < 6):
        lines.append("кожа бледновата — возможно, усталость или мало сна")
        flags.append("pallor")
    if (baseline and d_red > 6) or (not baseline and (red > 20 or eryth > 1.15)):
        lines.append("лицо с приливом/раскраснелось — возбуждение или жарко")
        flags.append("flush")

    # усталость тона: желтизна + неровность
    if yellow > 24 or evenness > 0.18:
        lines.append("тон неровный/желтоватый — похоже на усталость")
        flags.append("tired_tone")

    # блеск/себум (гидратация-прокси, очень относительно)
    if shine > 35:
        lines.append("кожа блестит — жирность/жара")
        flags.append("shine")

    # асимметрия красноты — флаг «присмотреться»
    if asym and asym > 6:
        flags.append("redness_asymmetry")

    return {
        "available": True, "ok": True,
        "pallor_flush": round(red, 1),         # ниже = бледнее, выше = прилив
        "tone_unevenness": evenness,
        "shine": shine,
        "redness_asymmetry": asym,
        "flags": flags,
        "lines": lines,
        "note": "Маркеры кожи (относительный тренд, зависит от света/камеры, не диагноз).",
    }
