"""
Анализ участника АЦ: контент-анализ речи + сопоставление с rPPG-логом.

Берёт:
- docx-транскрипт упражнения (с диаризацией Спикер 0/1/...)
- rppg_live_log.csv (или срез по участнику)
- ID спикера-участника (обычно Спикер 0)

Возвращает текстовый отчёт со всеми модулями из technical_analysis.py.

Запуск:
    venv_new/bin/python analysis/analyze_participant.py \\
        --docx /path/to/transcript.docx \\
        --log data/alex_20260601_110734.csv \\
        --speaker "Спикер 0" \\
        --out reports/alex_20260601.md
"""

import sys
import csv
import argparse
import statistics
from pathlib import Path
from datetime import datetime, time as dtime

sys.path.insert(0, str(Path(__file__).parent))

import docx as docx_lib

from technical_analysis import (
    analyze_disfluencies,
    analyze_mbti,
    analyze_ocean,
    analyze_pavlov,
    analyze_radicals,
    analyze_dark_tetrad,
    analyze_potential,
    analyze_stress,
    analyze_shadow_sides,
    analyze_prism,
    extract_speaker_turns,
)

import re
from collections import Counter


# ============ извлечение речи из docx ============

def extract_speaker_text_with_offset(docx_path, speaker_label="Спикер 0"):
    """Достаёт реплики указанного спикера из docx с таймштампами.
    Возвращает: (full_text, list_of_(t_start_sec, text))."""
    doc = docx_lib.Document(docx_path)
    full = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    # Кусок после "Полный диалог"
    m = re.search(r"Полный диалог.*?\n(.*)", full, re.S)
    body = m.group(1) if m else full
    items = re.findall(
        r"\[(\d+):(\d+):(\d+(?:\.\d+)?)\] " + re.escape(speaker_label) + r": (.*?)(?=\n\[|\Z)",
        body, re.S,
    )
    parsed = []
    full_text_parts = []
    for hh, mm, ss, txt in items:
        t = int(hh) * 3600 + int(mm) * 60 + float(ss)
        clean = txt.strip().replace("\n", " ")
        parsed.append((t, clean))
        full_text_parts.append(clean)
    return " ".join(full_text_parts), parsed


# ============ доп. речевые метрики ============

WORD_RE = re.compile(r"[А-Яа-яЁё]+", re.U)

def basic_text_metrics(text):
    sents = re.split(r"[.!?]+", text)
    sents = [s.strip() for s in sents if s.strip()]
    words = WORD_RE.findall(text.lower())
    types = set(words)
    return {
        "total_words": len(words),
        "unique_words": len(types),
        "TTR": len(types) / len(words) if words else 0,
        "sentences": len(sents),
        "avg_sentence_len_words": (len(words) / len(sents)) if sents else 0,
    }


def pronoun_balance(text):
    t = " " + text.lower() + " "
    я = len(re.findall(r"\b(я|мне|меня|мной)\b", t))
    мы = len(re.findall(r"\b(мы|нас|нам|нами)\b", t))
    ты_вы = len(re.findall(r"\b(ты|вы|вас|вам)\b", t))
    они = len(re.findall(r"\b(они|их|им|ими)\b", t))
    total = я + мы + ты_вы + они
    return {
        "я_count": я, "мы_count": мы, "ты_вы_count": ты_вы, "они_count": они,
        "я_pct": я / total * 100 if total else 0,
        "мы_pct": мы / total * 100 if total else 0,
    }


def top_words(text, top_n=20, min_len=3, stop=None):
    stop = stop or set("""
        это что как для так все они вот ну да нет если же или но
        он она оно его её ему ей них уже еще ещё там тут где когда
        кто чего чем тем такой такая такие меня тебя вас нас вам нам
        чтобы потому очень только один два три первое второе
    """.split())
    words = WORD_RE.findall(text.lower())
    words = [w for w in words if len(w) >= min_len and w not in stop]
    return Counter(words).most_common(top_n)


# ============ сшивка с rPPG-логом ============

COLS = ["timestamp","hr","snr","rmssd","resp","face_idx",
        "v_db","v_pitch","v_speech","v_tempo","v_pauses","v_avgp"]

def read_rppg_log(log_path):
    """Читает CSV (с заголовком из 12 колонок или старым 5-колоночным)."""
    rows = []
    with open(log_path) as f:
        header = f.readline().strip().split(",")
        for line in f:
            parts = line.strip().split(",")
            if len(parts) != 12: continue
            rec = dict(zip(COLS, parts))
            rows.append(rec)
    return rows


def asfloat(x):
    try: return float(x)
    except: return None


def rppg_summary(rows):
    def s(field, lo=None, hi=None):
        v = [asfloat(r[field]) for r in rows]
        v = [x for x in v if x is not None]
        if lo is not None: v = [x for x in v if x > lo]
        if hi is not None: v = [x for x in v if x < hi]
        if not v: return None
        return {
            "n": len(v),
            "mean": statistics.mean(v),
            "median": statistics.median(v),
            "sigma": statistics.stdev(v) if len(v) > 1 else 0,
            "min": min(v),
            "max": max(v),
        }
    return {
        "HR_clean": s("hr", 50, 110),
        "HR_raw": s("hr", 40, 200),
        "resp": s("resp", 5, 40),
        "pitch_smoothed": s("v_pitch", 60, 350),
        "speech_ratio": s("v_speech"),
        "tempo_per_min": s("v_tempo", 5, 300),
        "pauses_per_min": s("v_pauses"),
        "avg_pause_sec": s("v_avgp", 0.5, 20),
    }


# ============ генерация отчёта ============

def fmt_section(title):
    return f"\n\n## {title}\n"


def fmt_stat(name, s):
    if not s: return f"- **{name}**: нет данных\n"
    return (f"- **{name}**: средн. {s['mean']:.1f}, медиана {s['median']:.1f}, "
            f"σ {s['sigma']:.1f}, n={s['n']}, диапазон {s['min']:.0f}–{s['max']:.0f}\n")


def fmt_dict_top(d, indent="  "):
    out = ""
    for k, v in d.items():
        if isinstance(v, (int, float)):
            out += f"{indent}- {k}: {v}\n"
        elif isinstance(v, str):
            out += f"{indent}- {k}: {v[:200]}\n"
        elif isinstance(v, dict):
            out += f"{indent}- {k}:\n" + fmt_dict_top(v, indent + "  ")
        elif isinstance(v, list):
            out += f"{indent}- {k}: {v[:5]}{'...' if len(v)>5 else ''}\n"
    return out


def build_report(docx_path, log_path, speaker, participant_name):
    text, turns = extract_speaker_text_with_offset(docx_path, speaker)
    rppg_rows = read_rppg_log(log_path) if log_path and Path(log_path).exists() else []

    report = [f"# Отчёт по участнику: {participant_name}\n"]
    report.append(f"**Транскрипт**: `{Path(docx_path).name}`")
    report.append(f"**Спикер**: {speaker}")
    report.append(f"**rPPG-лог**: `{Path(log_path).name}` ({len(rppg_rows)} строк)" if rppg_rows else "**rPPG-лог**: не привязан")
    report.append(f"**Реплик участника**: {len(turns)}")

    # === 1. Базовые метрики речи ===
    report.append(fmt_section("1. Базовые метрики речи"))
    base = basic_text_metrics(text)
    report.append(f"- Всего слов: **{base['total_words']}**")
    report.append(f"- Уникальных слов: **{base['unique_words']}**")
    report.append(f"- **TTR (лексическое разнообразие): {base['TTR']:.3f}**")
    report.append(f"  - <0.40 = ограниченный словарь, 0.40–0.55 = средний, >0.55 = богатый")
    report.append(f"- Предложений: {base['sentences']}")
    report.append(f"- **Средняя длина предложения: {base['avg_sentence_len_words']:.1f} слов**")
    report.append(f"  - <8 = короткие/рубленые, 8–14 = норма, >14 = сложные")

    # === 2. Местоимения ===
    report.append(fmt_section("2. Профиль местоимений (я vs мы vs они)"))
    pn = pronoun_balance(text)
    report.append(f"- **«я» — {pn['я_count']} ({pn['я_pct']:.0f}%)**")
    report.append(f"- **«мы» — {pn['мы_count']} ({pn['мы_pct']:.0f}%)**")
    report.append(f"- «ты/вы» — {pn['ты_вы_count']}")
    report.append(f"- «они» — {pn['они_count']}")
    if pn['я_pct'] > 2 * pn['мы_pct']:
        report.append(f"  → **сильный перекос в «я»** (индивидуалист, не коллектив)")
    elif pn['мы_pct'] > pn['я_pct']:
        report.append(f"  → **командный** (чаще говорит «мы»)")

    # === 3. Дисфлюэнсии (заполнители) ===
    report.append(fmt_section("3. Заполнители речи (дисфлюэнсии)"))
    df = analyze_disfluencies(text)
    report.append(fmt_dict_top(df))

    # === 4. Топ-слова ===
    report.append(fmt_section("4. Топ-25 содержательных слов"))
    tops = top_words(text, top_n=25)
    for w, c in tops:
        report.append(f"- {w}: {c}")

    # === 5. Психотипологии ===
    speech_metrics_for_typology = {
        "speech_rate_wpm": 0,  # для нашего инструмента — пустой
        "word_count": base["total_words"],
    }
    report.append(fmt_section("5. MBTI (по словарным маркерам)"))
    try:
        r = analyze_mbti(text, speech_metrics_for_typology)
        report.append(fmt_dict_top(r))
    except Exception as e:
        report.append(f"_ошибка MBTI: {e}_")

    report.append(fmt_section("6. OCEAN / Big Five"))
    try:
        r = analyze_ocean(text, speech_metrics_for_typology)
        report.append(fmt_dict_top(r))
    except Exception as e:
        report.append(f"_ошибка OCEAN: {e}_")

    report.append(fmt_section("7. Типология Павлова (ВНД)"))
    try:
        r = analyze_pavlov(text, speech_metrics_for_typology)
        report.append(fmt_dict_top(r))
    except Exception as e:
        report.append(f"_ошибка Павлов: {e}_")

    report.append(fmt_section("8. Радикалы"))
    try:
        r = analyze_radicals(text, speech_metrics_for_typology)
        report.append(fmt_dict_top(r))
    except Exception as e:
        report.append(f"_ошибка радикалы: {e}_")

    report.append(fmt_section("9. Тёмная тетрада"))
    try:
        r = analyze_dark_tetrad(text)
        report.append(fmt_dict_top(r))
    except Exception as e:
        report.append(f"_ошибка тёмная тетрада: {e}_")

    report.append(fmt_section("10. Потенциал"))
    try:
        r = analyze_potential(text)
        report.append(fmt_dict_top(r))
    except Exception as e:
        report.append(f"_ошибка потенциал: {e}_")

    report.append(fmt_section("11. Стресс-профиль (по речи)"))
    try:
        r = analyze_stress(text, df, speech_metrics_for_typology)
        report.append(fmt_dict_top(r))
    except Exception as e:
        report.append(f"_ошибка стресс: {e}_")

    report.append(fmt_section("12. Теневые стороны"))
    try:
        r = analyze_shadow_sides(text)
        report.append(fmt_dict_top(r))
    except Exception as e:
        report.append(f"_ошибка теневые стороны: {e}_")

    report.append(fmt_section("13. Призма"))
    try:
        r = analyze_prism(text, df)
        report.append(fmt_dict_top(r))
    except Exception as e:
        report.append(f"_ошибка призма: {e}_")

    # === rPPG сводка ===
    if rppg_rows:
        report.append(fmt_section("14. rPPG: физиология за упражнение"))
        s = rppg_summary(rppg_rows)
        report.append(fmt_stat("HR (без артефактов, 50-110)", s["HR_clean"]))
        report.append(fmt_stat("HR (с артефактами)", s["HR_raw"]))
        report.append(fmt_stat("Дыхание /мин", s["resp"]))
        report.append(fmt_stat("Питч (смешан с асс.)", s["pitch_smoothed"]))
        report.append(fmt_stat("Доля речи", s["speech_ratio"]))
        report.append(fmt_stat("Темп (всплески/мин)", s["tempo_per_min"]))
        report.append(fmt_stat("Паузы >1с /мин", s["pauses_per_min"]))
        report.append(fmt_stat("Ср. длина паузы (сек)", s["avg_pause_sec"]))

    return "\n".join(report)


# ============ CLI ============

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--docx", required=True, help="путь к транскрипт-docx")
    p.add_argument("--log", default=None, help="путь к rPPG CSV (опц.)")
    p.add_argument("--speaker", default="Спикер 0", help="кого считать участником")
    p.add_argument("--name", default="Участник", help="имя для отчёта")
    p.add_argument("--out", default=None, help="куда сохранить отчёт (.md)")
    args = p.parse_args()

    report = build_report(args.docx, args.log, args.speaker, args.name)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"отчёт сохранён: {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
