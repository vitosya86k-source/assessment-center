#!/usr/bin/env python3
"""Сводка wellness — композитные карточки + человеческий вывод.

Берёт сигналы кружка (pulse, eye, tension, voice, skin, spo2, neiry) и лепит:
- карточки состояния (Энергия, Стресс, Недосып, Когнитивная нагрузка) — каждая
  с человеческим ОПИСАНИЕМ, не только числом;
- связный `narrative` — 3-5 предложений живым языком (это и есть финальный текст,
  который должен идти пользователю, а не список «Карточка: число»).

Маркеры/тренды, НЕ диагноз. Все компоненты — из уже посчитанных модулей.
"""
from __future__ import annotations


def _clip(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _lvl(score):
    return "низкий" if score < 35 else ("высокий" if score >= 65 else "средний")


# --- человеческие описания карточек по уровню (не числа) ---
_CARD_TEXT = {
    "Энергия": {
        "низкий": "энергии маловато — лицо выглядит подсевшим, похоже на тяжёлый день или недосып",
        "средний": "энергия на обычном рабочем уровне",
        "высокий": "энергия в ресурсе — лицо подвижное, чувствуется живость",
    },
    "Стресс": {
        "низкий": "напряжения не видно — тело держится спокойно",
        "средний": "лёгкое напряжение есть, но некритичное",
        "высокий": "напряжение выражено сильно — тело зажато",
    },
    "Недосып": {
        "низкий": "признаков недосыпа не видно",
        "средний": "есть лёгкие следы усталости в глазах",
        "высокий": "похоже на недосып — веки тяжелеют",
    },
    "Когнитивная нагрузка": {
        "низкий": "голова свободна, фокус держится легко",
        "средний": "умеренная нагрузка на внимание, обычный рабочий режим",
        "высокий": "нагрузка на внимание высокая — частое моргание, взгляд менее устойчив",
    },
}

_VOICE_TEXT = {
    "низкий": "голос звучит спокойно",
    "средний": "голос в обычном тонусе",
    "высокий": "голос звучит напряжённо, с лёгкой дрожью",
}


def compose(*, pulse=None, eye=None, tension=None, voice=None, neiry=None,
            skin=None, spo2=None) -> dict:
    pulse = pulse or {}; eye = eye or {}; tension = tension or {}
    voice = voice or {}; neiry = neiry or {}; skin = skin or {}; spo2 = spo2 or {}

    def have(d):
        return bool(d.get("available") or d.get("ok"))

    raw = {}   # числовые баллы (для калибровки/истории), в текст не идут напрямую

    # --- НЕДОСЫП: PERCLOS + плоский голос ---
    sl = []
    if have(eye):
        sl.append(_clip(eye.get("perclos_pct", 0) * 4))
        sl.append(_clip(eye.get("blink_rate_per_min", 18) / 35 * 100 - 30))
    if have(voice):
        sl.append(_clip(voice.get("vocal_fatigue", 0)))
    if sl:
        raw["Недосып"] = round(sum(sl) / len(sl))

    # --- СТРЕСС: вокальный стресс + напряжение лица + пульс + neiry ---
    st = []
    if have(neiry) and neiry.get("stress") is not None:
        st.append(neiry["stress"])
    if have(voice):
        st.append(voice.get("vocal_stress", 0))
    if have(tension):
        st.append(_clip(tension.get("armor_index", 0) * 100))
    if pulse.get("hr_bpm"):
        st.append(_clip((pulse["hr_bpm"] - 60) / 50 * 100))
    if st:
        raw["Стресс"] = round(sum(st) / len(st))

    # --- ЭНЕРГИЯ: голос-витальность + бодрость глаз + подвижность лица ---
    en = []
    if have(voice):
        en.append(voice.get("vocal_vitality", 50))
    if have(eye):
        en.append(_clip(100 - eye.get("perclos_pct", 0) * 4))
    if have(tension):
        en.append(_clip(100 - tension.get("facial_frozenness", 0) * 100))
    if en:
        raw["Энергия"] = round(sum(en) / len(en))

    # --- КОГНИТИВНАЯ НАГРУЗКА: моргания + нестабильность фиксации + напряжение лба ---
    cg = []
    if have(eye):
        cg.append(_clip(eye.get("blink_rate_per_min", 15) / 30 * 100))
        cg.append(_clip(100 - eye.get("fixation_stability", 0.5) * 100))
    if have(tension):
        cg.append(_clip(tension.get("brow_tension", 0) * 100))
    if cg:
        raw["Когнитивная нагрузка"] = round(sum(cg) / len(cg))

    cards = {
        name: {"score": score, "level": _lvl(score), "text": _CARD_TEXT[name][_lvl(score)]}
        for name, score in raw.items()
    }

    # --- собираем связный текст (это финальный вывод, не список чисел) ---
    sentences = []

    energy_lvl = cards.get("Энергия", {}).get("level")
    stress_lvl = cards.get("Стресс", {}).get("level")
    sleep_lvl = cards.get("Недосып", {}).get("level")
    cog_lvl = cards.get("Когнитивная нагрузка", {}).get("level")

    if stress_lvl == "высокий" and energy_lvl == "низкий":
        verdict = "День был непростой: напряжение высокое, ресурс низкий — стоит отдохнуть."
    elif stress_lvl == "высокий" and energy_lvl == "высокий":
        verdict = "Энергии много, но и напряжение заметное — состояние на подъёме, но не бесплатно."
    elif energy_lvl == "высокий" and stress_lvl in (None, "низкий"):
        verdict = "Состояние ресурсное и спокойное."
    elif energy_lvl == "высокий":
        verdict = "Состояние ресурсное, с лёгким фоновым напряжением."
    elif stress_lvl == "высокий":
        verdict = "Ресурса немного, а напряжение при этом повышено — стоит быть к себе бережнее."
    elif "Энергия" in cards or "Стресс" in cards:
        verdict = "Состояние умеренное, без резких перекосов."
    else:
        verdict = "Данных для общей картины пока маловато."
    # verdict НЕ входит в sentences/narrative — возвращается отдельным полем,
    # чтобы вызывающий код (единый текст) сам решал, использовать ли его как
    # открывающую фразу, не дублируя ту же формулировку дважды в одном ответе

    if "Энергия" in cards:
        sentences.append(cards["Энергия"]["text"].capitalize() + ".")
    if "Стресс" in cards:
        sentences.append(cards["Стресс"]["text"].capitalize() + ".")
    # зоны напряжения — ОДНИМ предложением (не дублировать в скобках у карточки И
    # отдельной фразой про челюсть); показываем только если стресс не "низкий" —
    # иначе получается противоречие вида «напряжения не видно (глаза напряжены)»
    if stress_lvl != "низкий" and have(tension) and tension.get("zones"):
        zones = tension["zones"][:3]
        zone_sent = "Заметно напряжение: " + ", ".join(zones)
        if any("челюсть" in z for z in zones):
            zone_sent += " — стоит расслабить, сбросить напряжение"
        sentences.append(zone_sent + ".")
    # гейт на energy!=высокий: «недосып» и «энергия в ресурсе» — взаимоисключающие
    # утверждения, даже если по разным сигналам обе карточки формально набрали высокий балл
    if sleep_lvl == "высокий" and energy_lvl != "высокий":
        sentences.append(cards["Недосып"]["text"].capitalize() + ".")
    if cog_lvl == "высокий":
        sentences.append(cards["Когнитивная нагрузка"]["text"].capitalize() + ".")

    # голос как отдельный канал сюда НЕ выносим — единый текст (compose_reply_v2)
    # описывает голос один раз, своим отдельным абзацем, из этих же данных

    if skin.get("available"):
        flags = skin.get("flags", [])
        if "tired_tone" in flags:
            sentences.append("Тон кожи уставший — возможно, недосып.")
        if "pallor" in flags:
            sentences.append("Кожа бледновата.")
        if "flush" in flags:
            sentences.append("Лицо раскраснелось — возбуждение или жарко.")

    if have(eye) and eye.get("blink_rate_per_min", 0) >= 25 and eye.get("perclos_pct", 0) < 10:
        sentences.append("Глаза часто моргают — возможно, сухость или много экрана, стоит попить воды.")

    # SpO2 сюда НЕ выносим — это физиологический показатель уровня "Тело" (наравне
    # с пульсом/давлением), единый текст показывает его там, не здесь; возвращаем
    # только значение, для истории/калибровки
    spo2_val = spo2.get("spo2") if spo2.get("available") else None

    narrative = " ".join(sentences)

    return {
        "available": bool(cards) or bool(sentences),
        "cards": cards,
        "narrative": narrative,
        "verdict": verdict,
        "spo2": spo2_val,
        "note": "Wellness-сводка (маркеры состояния, не диагноз).",
    }
