# METRICS COVERAGE — NeuroHRV vs Kubios

Дата: 2026-02-09

## 1) Что считаем сейчас (в коде)

**Time-domain**
- Mean RR
- Mean HR
- Min HR / Max HR
- SDNN
- RMSSD
- NN50 / pNN50

**Poincaré**
- SD1
- SD2
- SD1/SD2 ratio

**Frequency-domain**
- VLF Power (0.003–0.04 Гц)
- LF Power (0.04–0.15 Гц)
- HF Power (0.15–0.4 Гц)
- LF/HF ratio
- LF n.u.
- HF n.u.
- Total Power

**Other / Derived**
- Stress Index (Баевский)
- Respiratory rate (HF peak)
- SNS% / PNS%
- Biological age
- Axes: RD, SR, AD, FL, RC, EN, BL

## 2) Что есть в Kubios, но ещё нет у нас

**Time-domain**
- HRV Triangular Index
- TINN
- SDANN
- SDNNi

**Nonlinear**
- ApEn (Approximate Entropy)
- SampEn (Sample Entropy)
- DFA α1
- DFA α2

**Frequency-domain**
- Peak LF / Peak HF (частоты пиков)

## 3) Описания (есть/нет)

**Есть описания:**
- HRV_METRICS_REFERENCE.md — базовые time-domain, Poincaré, LF/HF, Stress Index
- PolarVerityHRV/TECHNICAL_DOCUMENTATION.md — DFA, SampEn/ApEn, VLF и др.
- NEUROHRV_TEXT_CATALOG_FULL.md — формулировки по осям, SWOT и правила генерации

**Нет/нужно добавить описания:**
- HRV Triangular Index, TINN
- SDANN, SDNNi
- Peak LF / Peak HF

## 4) Нужно для дашбордов (чего не хватает в текстах)

**Добавить в «Памятку»:**
- VLF Power
- Total Power
- LF/HF n.u.
- NN50
- Min/Max HR

**Индивидуализация формулировок (пока шаблонно):**
- Сильные стороны / Ограничения / Возможности / Риски
  - Нужны персонализированные тексты по шкалам и порогам

## 5) Данные для сравнения с Kubios

- CSV с RR (если доступно по BLE) — сравнение корректнее
- CSV 1Hz (ресемпл) — удобно для графиков и сравнения по времени
