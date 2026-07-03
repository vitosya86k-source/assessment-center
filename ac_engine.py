"""Движок телефонного АЦ (Ветка 2): один вход — путь к клипу, один выход — единый dict.

Недостающее ЗВЕНО, которого не было: связать offline-combo (эмоции+поза+пульс+речь)
с wellness-слоем (глаза/зажимы/SpO2/голос/давление) и Neiry-композитами в ОДИН ответ.
Ровно то, что нужно серверному endpoint POST /ac/analyze (тонкий клиент шлёт клип →
сервер считает → метрики + состояние + человекочитаемые итоги).

Ничего нового не «изобретает»: только вызывает уже существующие анализаторы и склеивает
их выходы через готовый wellness_summary.compose + combo_neiry (compute_neiry/summary_cards).

Запуск: в venv_new (mediapipe/cv2 для wellness). Эмоции combo берёт из emo_venv сам,
через subprocess (combo_analyze оркеструет venv'ы).

ОГРАНИЧЕНИЯ offline (честно): rPPG по короткому клипу даёт ЧСС (hr_median), но НЕ ВСР
(rmssd) и НЕ дыхание (resp) — их в offline нет, поэтому давление/стресс считаются по ЧСС
(+ эмоции/голос/поза), это ориентир, не медизмерение. head_tilt тоже пока не в combo-pose
summary → вклад в вовлечённость частичный (по неподвижности + лицу в кадре).
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "wellness"))

import combo_analyze
from combo_neiry import compute_neiry, summary_cards
import bp_estimate, voice_wellness, wellness_summary          # wellness/ в sys.path
import eye_markers, tension_markers, spo2_estimate            # тяжёлые (mediapipe)


def _safe(fn, *a, **kw):
    """Один упавший анализатор не должен рушить весь ответ — endpoint отдаёт частичное."""
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001 — намеренно широко, причина едет в ответ
        return {"available": False, "error": f"{type(e).__name__}: {e}"}


def _pulse_for_wellness(rppg_pulse: dict) -> dict:
    """Адаптер: offline-rppg отдаёт hr_median; bp_estimate/compute_neiry/compose ждут hr_bpm.
    ВСР/дыхание offline недоступны → None (модули корректно считают вклад = 0)."""
    ok = bool(rppg_pulse.get("available"))
    return {
        "ok": ok, "available": ok,
        "hr_bpm": rppg_pulse.get("hr_median"),
        "rmssd_ms": None, "resp_bpm": None,
        "confidence": "low",   # offline rPPG по клипу — ориентир
    }


def analyze_ac_clip(video: str | Path, *, transcript: str | Path | None = None,
                    speaker: str = "Спикер 0", name: str | None = None,
                    label: str = "") -> dict:
    """Полный разбор клипа участника → единый dict для endpoint/бота.

    Структура ответа: metrics (сырое по каналам) + state (Neiry-индексы + карточки-итоги)
    + wellness (человекочитаемый narrative) + content (типология из транскрипта, если дан).
    """
    video = str(video)

    # 1) combo: эмоции + поза + пульс/речь (subprocess по venv'ам — уже оркестрировано)
    combo = _safe(combo_analyze.analyze_video, video, label=label,
                  transcript=transcript, speaker=speaker, name=name)
    mods = (combo.get("modules") if isinstance(combo, dict) else None) or {}
    emo = mods.get("emotions", {}) or {}
    pose = mods.get("pose", {}) or {}
    rppg = mods.get("rppg", {}) or {}
    pulse_raw = rppg.get("pulse", {}) or {}
    speech = rppg.get("speech", {}) or {}
    pulse = _pulse_for_wellness(pulse_raw)

    # 2) wellness-слой по видео (venv_new, в этом же процессе)
    eye = _safe(eye_markers.analyze, video)
    tension = _safe(tension_markers.analyze, video)
    spo2 = _safe(spo2_estimate.analyze, video)
    voice = _safe(voice_wellness.analyze, speech)     # speech как prosody (частично)
    bp = _safe(bp_estimate.estimate, pulse)

    # 3) Neiry-композиты + карточки-итоги (наши, из Ветки 1)
    emap = emo.get("mean_scores") or {}
    perc = emo.get("perception") or {}
    ni = compute_neiry(
        hr=pulse["hr_bpm"], resp=None,
        valence=emo.get("valence"), arousal=emo.get("arousal"),
        e_anger=emap.get("Anger"), e_fear=emap.get("Fear"),
        tempo=speech.get("tempo_onsets_per_min"),
        pitch_std=speech.get("pitch_std_hz"),
        loud_iqr=speech.get("loudness_iqr_db"),
        speech_ratio=speech.get("speech_ratio"),
        fidget=pose.get("fidget_mean"), head_tilt=pose.get("head_tilt_mean"),
        face_present=emo.get("available"),
    )
    cards = summary_cards(
        stress=ni["stress"], fatigue=ni["fatigue"], engagement=ni.get("engagement"),
        trust=perc.get("perceived_trust"), dominance=perc.get("perceived_dominance"),
        pitch_std=speech.get("pitch_std_hz"),
        pause_pct=None,     # offline speech даёт pauses_over_1s, не %; в карточку не идёт
        tempo=speech.get("tempo_onsets_per_min"),
        speech_ratio=speech.get("speech_ratio"),
    )

    # 4) единый человекочитаемый wellness-вывод (тот же compose, что у кружка)
    wellness = _safe(wellness_summary.compose, pulse=pulse, eye=eye, tension=tension,
                     voice=voice, neiry=ni, spo2=spo2)

    return {
        "ok": True,
        "video": video,
        "mode": rppg.get("mode", "нет данных"),
        "metrics": {
            "pulse": pulse_raw, "bp": bp, "spo2": spo2,
            "eye": eye, "tension": tension, "voice": voice,
            "emotions": emo, "pose": pose, "speech": speech,
        },
        "state": {"neiry": ni, "cards": cards},
        "wellness": wellness if isinstance(wellness, dict) else {"available": False},
        "content": combo.get("content") if isinstance(combo, dict) else None,
        "report_md": combo.get("report_md") if isinstance(combo, dict) else None,
        "workdir": combo.get("workdir") if isinstance(combo, dict) else None,
        "note": "Offline-разбор клипа. ЧСС по видео (ВСР/дыхание offline недоступны). "
                "Маркеры состояния, не медизмерение.",
    }


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser(description="Разбор клипа участника (combo + wellness) → JSON")
    ap.add_argument("video", help="путь к видеоклипу")
    ap.add_argument("--transcript", default=None, help="путь к .docx транскрипту (типология)")
    ap.add_argument("--speaker", default="Спикер 0")
    ap.add_argument("--name", default=None)
    a = ap.parse_args()
    res = analyze_ac_clip(a.video, transcript=a.transcript, speaker=a.speaker, name=a.name)
    print(json.dumps(res, ensure_ascii=False, indent=2))
