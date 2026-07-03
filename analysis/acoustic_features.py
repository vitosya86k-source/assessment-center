"""
openSMILE eGeMAPS + parselmouth по чистым сегментам участника.

Берёт wav со склеенной речью, режет на 5-секундные кадры, считает:
- vocal energy IQR (E-маркер)
- pitch range (контроль интонации)
- mean F0, intensity (стресс)
- HNR, Hammarberg, spectral roll-off Q1 (нейротизм secondary)
- CPP (vocal effort)

Также считает «network shift» — какой параметр доминирует в IQR
(formant-based vs intensity-based). При сдвиге к intensity hub = тревога.
"""

import argparse
import json
import statistics
from pathlib import Path

import numpy as np
import opensmile
import parselmouth
from parselmouth.praat import call


KEY_FEATURES = {
    "F0semitoneFrom27.5Hz_sma3nz_amean": "F0_mean_semitones",
    "F0semitoneFrom27.5Hz_sma3nz_stddevNorm": "F0_stddev_norm",
    "F0semitoneFrom27.5Hz_sma3nz_percentile20.0": "F0_p20",
    "F0semitoneFrom27.5Hz_sma3nz_percentile80.0": "F0_p80",
    "F0semitoneFrom27.5Hz_sma3nz_pctlrange0-2": "pitch_range",
    "loudness_sma3_amean": "loudness_mean",
    "loudness_sma3_stddevNorm": "loudness_stddev_norm",
    "loudness_sma3_percentile20.0": "loudness_p20",
    "loudness_sma3_percentile80.0": "loudness_p80",
    "loudness_sma3_pctlrange0-2": "loudness_iqr",
    "HNRdBACF_sma3nz_amean": "HNR_mean",
    "spectralFlux_sma3_amean": "spectral_flux",
    "alphaRatioV_sma3nz_amean": "alpha_ratio",
    "hammarbergIndexV_sma3nz_amean": "hammarberg",
    "slopeV0-500_sma3nz_amean": "spectral_slope_0_500",
    "slopeV500-1500_sma3nz_amean": "spectral_slope_500_1500",
    "F1frequency_sma3nz_amean": "F1_mean",
    "F1bandwidth_sma3nz_amean": "F1_bw",
    "F2frequency_sma3nz_amean": "F2_mean",
    "F3frequency_sma3nz_amean": "F3_mean",
    "jitterLocal_sma3nz_amean": "jitter",  # пишем но не используем как stress
    "shimmerLocaldB_sma3nz_amean": "shimmer",
    "mfcc1_sma3_amean": "mfcc1",
    "mfcc2_sma3_amean": "mfcc2",
    "mfcc3_sma3_amean": "mfcc3",
    "mfcc4_sma3_amean": "mfcc4",
}


def egemaps_full(wav_path):
    sm = opensmile.Smile(
        feature_set=opensmile.FeatureSet.eGeMAPSv02,
        feature_level=opensmile.FeatureLevel.Functionals,
    )
    f = sm.process_file(str(wav_path))
    row = f.iloc[0].to_dict()
    out = {tgt: row[src] for src, tgt in KEY_FEATURES.items() if src in row}
    return out, row


def egemaps_lld_windows(wav_path, win_sec=30.0):
    """Кадровый eGeMAPS LLD, разрезанный на окна по win_sec — для динамики."""
    sm = opensmile.Smile(
        feature_set=opensmile.FeatureSet.eGeMAPSv02,
        feature_level=opensmile.FeatureLevel.LowLevelDescriptors,
    )
    df = sm.process_file(str(wav_path))
    df = df.reset_index()
    # колонка start — секунда в файле
    if "start" in df.columns:
        df["t"] = df["start"].dt.total_seconds()
    else:
        df["t"] = np.arange(len(df)) * 0.01

    total = float(df["t"].iloc[-1]) if len(df) else 0.0
    bins = np.arange(0, total + win_sec, win_sec)
    windows = []
    for i in range(len(bins) - 1):
        lo, hi = bins[i], bins[i + 1]
        sub = df[(df["t"] >= lo) & (df["t"] < hi)]
        if len(sub) < 30:
            continue
        F0 = sub["F0semitoneFrom27.5Hz_sma3nz"].replace(0, np.nan).dropna()
        loud = sub["Loudness_sma3"].replace(0, np.nan).dropna()
        F1 = sub.get("F1frequency_sma3nz", None)
        if F0.size < 5 or loud.size < 5:
            continue
        rec = {
            "win_start_sec": float(lo),
            "win_end_sec": float(hi),
            "F0_iqr": float(np.percentile(F0, 80) - np.percentile(F0, 20)),
            "F0_mean": float(F0.mean()),
            "loudness_iqr": float(np.percentile(loud, 80) - np.percentile(loud, 20)),
            "loudness_mean": float(loud.mean()),
        }
        if F1 is not None:
            F1c = F1.replace(0, np.nan).dropna()
            if F1c.size > 5:
                rec["F1_iqr"] = float(np.percentile(F1c, 80) - np.percentile(F1c, 20))
                rec["F1_mean"] = float(F1c.mean())
        # network hub — что доминирует: formant или intensity
        loud_norm = rec["loudness_iqr"] / (abs(rec["loudness_mean"]) + 1e-6)
        F0_norm = rec["F0_iqr"] / (rec["F0_mean"] + 1e-6)
        rec["hub_intensity_over_formant"] = loud_norm / (F0_norm + 1e-6)
        windows.append(rec)
    return windows


def parselmouth_features(wav_path):
    """CPP через Praat: cepstral peak prominence, voiceQuality."""
    snd = parselmouth.Sound(str(wav_path))
    pitch = snd.to_pitch(time_step=0.01, pitch_floor=75, pitch_ceiling=400)
    # PointProcess для jitter/shimmer (но мы их не используем как stress, только пишем)
    pp = call(snd, "To PointProcess (periodic, cc)", 75, 400)
    try:
        cpp = call(snd, "To PowerCepstrogram", 60, 0.002, 5000, 50)
        cpp_mean = call(cpp, "Get CPPS", True, 0.01, 0.001, 60, 330, 0.05, "Parabolic", 0.001, 0,
                        "Straight", "Robust")
    except Exception:
        cpp_mean = None
    # HNR
    try:
        harm = snd.to_harmonicity_cc(time_step=0.01, minimum_pitch=75)
        hnr_vals = harm.values.flatten()
        hnr_vals = hnr_vals[~np.isnan(hnr_vals)]
        hnr_mean = float(np.mean(hnr_vals)) if hnr_vals.size else None
    except Exception:
        hnr_mean = None
    # Speech rate (примерно через voiced fraction)
    voiced_frames = pitch.selected_array["frequency"]
    voiced_frac = float(np.mean(voiced_frames > 0))
    return {
        "CPP": float(cpp_mean) if cpp_mean is not None else None,
        "HNR_parselmouth": hnr_mean,
        "voiced_fraction": voiced_frac,
        "duration_sec": float(snd.duration),
    }


def composite_anxiety(features, lld_windows):
    """Композитный anxiety score из акустики + динамики.
    Положительный = больше тревоги."""
    # F0_mean ↑, loudness ↑, HNR ↓, jitter ↓ (парадокс), Hammarberg ↑
    # Используем z-score-подобные нормирующие константы из литературы (грубые)
    F0 = features.get("F0_mean_semitones") or 0
    loud_iqr = features.get("loudness_iqr") or 0
    hnr = features.get("HNR_mean") or 0
    hammar = features.get("hammarberg") or 0
    # network hub mean
    if lld_windows:
        hub_mean = statistics.mean(w["hub_intensity_over_formant"] for w in lld_windows)
    else:
        hub_mean = 0
    # эмпирическая нормировка (для сравнения упражнений участника, не абсолют)
    return {
        "F0_component": (F0 - 35) / 5,  # ~35 семитонов = база для мужчины
        "intensity_hub_shift": (hub_mean - 1.0),  # >1 = intensity доминирует
        "hammarberg_component": hammar / 10,
        "hnr_inverted": -hnr / 10,
        "score": (F0 - 35) / 5 + (hub_mean - 1.0) + hammar / 10 - hnr / 10,
    }


def process(wav_path):
    feats, raw = egemaps_full(wav_path)
    feats.update(parselmouth_features(wav_path))
    windows = egemaps_lld_windows(wav_path, win_sec=60.0)
    feats["anxiety"] = composite_anxiety(feats, windows)
    return {"summary": feats, "windows": windows}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--wav", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    result = process(args.wav)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=float))
    print(f"OK: {args.wav} -> {args.out}")


if __name__ == "__main__":
    main()
