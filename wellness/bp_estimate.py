#!/usr/bin/env python3
"""Давление по видео (rPPG) — обучаемая оценка SBP/DBP, НЕ медизмерение.

Подход (стандартный для cuffless BP): персонально-калибруемая регрессия по
надёжным rPPG-признакам, которые пайплайн уже выдаёт стабильно — ЧСС, ВСР (RMSSD),
дыхание. Коэффициенты — ОБУЧАЕМЫЕ (fit на размеченных данных манжетой), здесь даны
лишь начальные значения и направление (рост ЧСС и падение ВСР → подъём давления).
Для абсолютной точности — персональная калибровка одним замером манжетой.

Это рабочая v1, чтобы тестить в деле. Если не сходится — выключаем флагом ENABLED.
Upgrade-путь (в README): PPG2ABP (одно-PPG→ABP-волна, DL-веса) когда подключим TF.

Калибровка/обучение: bp_fit.json рядом с модулем — {sbp0,dbp0,hr0,rmssd0,coef:{...}}.
"""
from __future__ import annotations

import json
from pathlib import Path

ENABLED = True
_FIT = Path(__file__).resolve().parent / "bp_fit.json"

# начальная (обучаемая) модель: базовая точка + наклоны. Заменяется обучением/калибровкой.
_DEFAULT = {
    "sbp0": 118.0, "dbp0": 76.0,        # базовое давление в покое (калибруется манжетой)
    "hr0": 70.0, "rmssd0": 40.0,         # базовые ЧСС и ВСР этой точки
    "coef": {
        "sbp_hr": 0.6,   "sbp_rmssd": -0.25, "sbp_resp": 0.8,   # ↑ЧСС, ↓ВСР, ↑дых → ↑SBP
        "dbp_hr": 0.4,   "dbp_rmssd": -0.15, "dbp_resp": 0.5,
    },
    "trained": False,
}


def _load_fit():
    if _FIT.exists():
        try:
            d = json.loads(_FIT.read_text(encoding="utf-8"))
            return {**_DEFAULT, **d}
        except Exception:
            pass
    return dict(_DEFAULT)


def estimate(pulse: dict, fit: dict | None = None) -> dict:
    """pulse — результат rppg_video.analyze (hr_bpm, rmssd_ms, resp_bpm, confidence)."""
    if not ENABLED:
        return {"available": False, "reason": "BP-оценка выключена (ENABLED=False)"}
    if not pulse or not pulse.get("available"):
        return {"available": False, "reason": "нет пульса с лица"}

    f = fit or _load_fit()
    has_calibration = bool(f.get("calibrated_points"))
    hr = pulse.get("hr_bpm")
    if hr is None:
        return {"available": False, "reason": "нет ЧСС"}

    # Жёсткий gate только без персональной калибровки. После манжеты+кружка в один момент
    # rPPG часто ok=False (SNR кружка), но ЧСС всё равно годится для оценки с пониженной уверенностью.
    if not pulse.get("ok") and not has_calibration:
        return {"available": False, "reason": "нет надёжного rPPG-сигнала"}

    c = f["coef"]
    rm = pulse.get("rmssd_ms")
    rp = pulse.get("resp_bpm")
    d_hr = hr - f["hr0"]
    # rPPG ВСР/дыхание с короткого видео шумят — берём только в физиологичном диапазоне,
    # иначе зануляем вклад (ЧСС всё равно остаётся опорной).
    rm_ok = rm is not None and 5 <= rm <= 120
    rp_ok = rp is not None and 6 <= rp <= 30
    d_rm = (rm - f["rmssd0"]) if rm_ok else 0.0
    d_rp = (rp - 14.0) if rp_ok else 0.0

    sbp = f["sbp0"] + c["sbp_hr"] * d_hr + c["sbp_rmssd"] * d_rm + c["sbp_resp"] * d_rp
    dbp = f["dbp0"] + c["dbp_hr"] * d_hr + c["dbp_rmssd"] * d_rm + c["dbp_resp"] * d_rp
    sbp = max(85, min(180, sbp))
    dbp = max(45, min(110, dbp))

    # уверенность: после калибровки на слабом rPPG — явно «indicative»
    if not pulse.get("ok"):
        conf = "indicative"
    else:
        base_conf = pulse.get("confidence", "low")
        conf = base_conf if f.get("trained") or _FIT.exists() else "indicative"

    if sbp >= 140 or dbp >= 90:
        tend = "выше обычного — при повторе сверить манжетой"
    elif sbp <= 105 and dbp <= 65:
        tend = "ниже обычного"
    else:
        tend = "в пределах нормы"

    return {
        "available": True, "ok": True,
        "sbp": round(sbp), "dbp": round(dbp),
        "tendency": tend,
        "confidence": conf,
        "calibrated": bool(_FIT.exists()),
        "note": "Оценка по rPPG (обучаемая модель), НЕ измерение мм рт.ст. "
                "Для точности — калибровка манжетой (см. calibrate()).",
    }


def calibrate(cuff_sbp: float, cuff_dbp: float, pulse: dict) -> dict:
    """Персональная калибровка: один замер манжетой в покое + одновременный rPPG.

    Сдвигает базовую точку так, чтобы формула ВОСПРОИЗВЕЛА манжету в этой точке —
    с учётом остаточного вклада всех дельт (ЧСС/ВСР/дыхание), а не грубой подменой
    sbp0=манжета. Один точечный замер калибрует только интерсепт (наклоны остаются
    начальными); для наклонов нужно несколько точек в разных состояниях.

    Защита: короткий rPPG врёт по ВСР/дыханию — нефизиологичные значения НЕ пишем
    в базовую точку (иначе будущие оценки взорвутся на разнице с ними).
    """
    f = _load_fit()
    c = f["coef"]
    hr = pulse.get("hr_bpm")
    rm = pulse.get("rmssd_ms")
    rp = pulse.get("resp_bpm")

    rm_ok = rm is not None and 5 <= rm <= 120
    rp_ok = rp is not None and 6 <= rp <= 30

    # Сначала СДВИГАЕМ базовую точку в точку калибровки (только физиологичными
    # значениями), и только ПОТОМ считаем интерсепт относительно НЕЁ. Иначе смещение
    # берётся от старой базы, а хранится новая — и estimate() промахивается мимо манжеты.
    if hr is not None:
        f["hr0"] = hr
    if rm_ok:
        f["rmssd0"] = rm

    # дельты ровно как в estimate(), но уже от НОВОЙ базы: hr/rmssd → 0,
    # дыхание — от фиксированной опоры 14 (у него нет хранимого baseline).
    d_hr = (hr - f["hr0"]) if hr is not None else 0.0
    d_rm = (rm - f["rmssd0"]) if rm_ok else 0.0
    d_rp = (rp - 14.0) if rp_ok else 0.0

    # интерсепт = манжета минус вклад всех дельт → estimate(pulse) == манжета точно
    f["sbp0"] = float(cuff_sbp) - (c["sbp_hr"] * d_hr + c["sbp_rmssd"] * d_rm + c["sbp_resp"] * d_rp)
    f["dbp0"] = float(cuff_dbp) - (c["dbp_hr"] * d_hr + c["dbp_rmssd"] * d_rm + c["dbp_resp"] * d_rp)
    f["calibrated_points"] = f.get("calibrated_points", []) + [
        {"cuff_sbp": float(cuff_sbp), "cuff_dbp": float(cuff_dbp),
         "hr": hr, "rmssd": rm, "resp": rp, "rmssd_used": rm_ok}
    ]
    _FIT.write_text(json.dumps(f, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"calibrated": True, "fit": f}
