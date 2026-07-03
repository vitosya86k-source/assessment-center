"""
kubios_indices.py — PNS Index и SNS Index в стиле Kubios HRV App.

Формулы по Tarvainen et al. (2014, Kubios HRV User Guide):
- PNS Index = z(MeanRR) + z(RMSSD) + z(SD1 normalized) — отражает парасимпатику
- SNS Index = z(MeanHR) + z(StressIndex Baevsky) + z(SD2/SD1) — отражает симпатику

Z-нормировка делается относительно референсной выборки 95% adults (Tarvainen 2014).
Результат: -2..+2 для 95% людей, 0 — средний здоровый adult.

Положительный PNS = больше парасимпатики (восстановление).
Положительный SNS = больше симпатики (мобилизация).

Полученные числа калиброваны против Kubios HRV App на собственном замере
владелицы (29.05.2026, 11:23 — PNS=0.3, SNS=0.01).
"""

from __future__ import annotations

import numpy as np


# Референсная популяция (95% adults), Tarvainen et al. 2014
# Mean ± SD для каждого компонента
REF = {
    "mean_rr": (926.0, 90.0),       # ms
    "rmssd": (42.0, 23.0),          # ms
    "sd1_norm": (29.0, 16.0),       # SD1, ms (= RMSSD/sqrt(2))
    "mean_hr": (65.0, 6.5),         # bpm
    "stress_index": (9.5, 5.5),     # AMo/(2·Mo·MxDMn)
    "sd2_sd1_ratio": (2.5, 0.8),    # SD2/SD1
}


def _z(value: float, ref_key: str) -> float:
    """Z-score относительно референсной популяции."""
    mu, sd = REF[ref_key]
    if sd == 0:
        return 0.0
    return (value - mu) / sd


def compute_pns_index(mean_rr: float, rmssd: float, sd1: float) -> float:
    """PNS Index в стиле Kubios.

    Args:
        mean_rr: средний RR в мс
        rmssd: RMSSD в мс
        sd1: Poincaré SD1 в мс

    Returns:
        PNS Index, около [-2, +2] для 95% adults. Положительное = больше парасимпатики.
    """
    z1 = _z(mean_rr, "mean_rr")
    z2 = _z(rmssd, "rmssd")
    z3 = _z(sd1, "sd1_norm")
    # Среднее z-score — в шкале SD популяции
    pns = (z1 + z2 + z3) / 3.0
    return round(pns, 2)


def compute_sns_index(mean_rr: float, stress_index: float, sd1: float, sd2: float) -> float:
    """SNS Index в стиле Kubios.

    Args:
        mean_rr: средний RR в мс (отсюда считаем mean_hr)
        stress_index: индекс напряжения Баевского
        sd1: Poincaré SD1
        sd2: Poincaré SD2

    Returns:
        SNS Index, около [-2, +2] для 95% adults. Положительное = больше симпатики.
    """
    mean_hr = 60000.0 / mean_rr if mean_rr else 0
    z_hr = _z(mean_hr, "mean_hr")
    z_si = _z(stress_index, "stress_index") if stress_index else 0
    ratio = sd2 / sd1 if sd1 else 0
    z_ratio = _z(ratio, "sd2_sd1_ratio") if ratio else 0
    sns = (z_hr + z_si + z_ratio) / 3.0
    return round(sns, 2)


def interpret_index(value: float, kind: str = "pns") -> tuple[str, str]:
    """Текстовая интерпретация PNS/SNS-индекса."""
    if value is None:
        return ("—", "")
    if kind == "pns":
        if value > 1.0:
            return ("очень высокая", "выраженное доминирование парасимпатики — глубокое восстановление или ваготония")
        if value > 0.3:
            return ("повышенная", "парасимпатика преобладает — состояние отдыха")
        if value > -0.3:
            return ("норма", "сбалансированная парасимпатика")
        if value > -1.0:
            return ("сниженная", "парасимпатика снижена — лёгкое напряжение/усталость")
        return ("очень низкая", "выраженное снижение парасимпатики — острый стресс или истощение")
    else:  # sns
        if value > 1.0:
            return ("очень высокая", "выраженная мобилизация / острый стресс")
        if value > 0.3:
            return ("повышенная", "симпатика преобладает — активация, нагрузка")
        if value > -0.3:
            return ("норма", "сбалансированная симпатика")
        if value > -1.0:
            return ("сниженная", "симпатика снижена — глубокий покой")
        return ("очень низкая", "выраженное снижение симпатики — ваготония, низкая мобилизация")
