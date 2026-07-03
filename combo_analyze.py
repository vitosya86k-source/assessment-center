"""
combo_analyze.py — оркестратор оффлайн-разбора видео для комбо-бота.

Идея (важно — это решение против зависаний): каждый анализатор гоняется
ОТДЕЛЬНЫМ subprocess'ом в СВОЁМ venv и ПОСЛЕДОВАТЕЛЬНО (эмоции → поза → rPPG/речь),
а не три тяжёлых процесса параллельно в realtime, как раньше. Для разбора файла
скорость некритична, зато ничего не виснет и не конфликтует по venv.

Сейчас подключён модуль эмоций (analyze_video_emotions.py). Модули позы и
rPPG/речи подхватятся автоматически, как только появятся файлы
analyze_video_pose.py / analyze_video_rppg.py (см. _MODULES).
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import combo_config as cfg

# Описание модулей-анализаторов: (ключ, скрипт, интерпретер venv)
_MODULES = [
    ("emotions", "analyze_video_emotions.py", cfg.EMO_PYTHON),
    ("pose", "analyze_video_pose.py", cfg.MAIN_PYTHON),       # появится позже
    ("rppg", "analyze_video_rppg.py", cfg.MAIN_PYTHON),       # появится позже (пульс+речь)
]


def _run_module(key: str, script: str, python: str, video: Path, workdir: Path,
                sample_fps: float) -> dict:
    script_path = cfg.BASE_DIR / script
    if not script_path.exists():
        return {"ok": False, "skipped": True, "module": key, "reason": "модуль ещё не реализован"}
    if not Path(python).exists():
        return {"ok": False, "skipped": True, "module": key, "reason": f"нет venv: {python}"}

    out_csv = workdir / f"{key}.csv"
    out_json = workdir / f"{key}.json"
    cmd = [python, str(script_path), "--video", str(video),
           "--out-csv", str(out_csv), "--out-json", str(out_json),
           "--sample-fps", str(sample_fps)]
    try:
        proc = subprocess.run(cmd, cwd=str(cfg.BASE_DIR), capture_output=True,
                              text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        return {"ok": False, "module": key, "error": "timeout (>1ч)"}
    if out_json.exists():
        try:
            return json.loads(out_json.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"ok": False, "module": key, "error": (proc.stderr or proc.stdout or "нет вывода")[-500:]}


def analyze_video(video: str | Path, label: str = "", sample_fps: float | None = None,
                  transcript: str | Path | None = None, speaker: str = "Спикер 0",
                  name: str | None = None) -> dict:
    """Прогоняет видео через все доступные модули и собирает единую сводку.

    Если задан transcript (.docx) — добавляет контент-анализ речи (12 модулей) и
    богатый ассессмент-отчёт через claude CLI (паттерн FS).
    """
    video = Path(video)
    name = name or label or "Участник"
    sample_fps = sample_fps if sample_fps is not None else cfg.SAMPLE_FPS
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = "".join(c for c in label if c.isalnum() or c in "-_") or "session"
    workdir = cfg.RESULTS_DIR / f"{stamp}_{safe_label}"
    workdir.mkdir(parents=True, exist_ok=True)

    modules = {}
    for key, script, python in _MODULES:
        modules[key] = _run_module(key, script, python, video, workdir, sample_fps)

    summary = {
        "label": label,
        "video": str(video),
        "created_at": stamp,
        "workdir": str(workdir),
        "modules": modules,
    }
    (workdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["report_md"] = _render_markdown(summary)
    (workdir / "report.md").write_text(summary["report_md"], encoding="utf-8")

    # Единый таймлайн всех каналов по времени
    try:
        import combo_timeline
        summary["timeline"] = combo_timeline.build_timeline(workdir)
    except Exception as e:
        print(f"timeline не собран: {e}")

    # Word-версия отчёта (.docx) без pandoc/LLM
    try:
        import report_docx
        summary["report_docx"] = report_docx.md_to_docx(
            summary["report_md"], workdir / "report.docx",
            title=f"Комбо-разбор: {label or 'участник'}")
    except Exception as e:
        print(f"report.docx не собран: {e}")

    # Контент-анализ речи (12 модулей) — если приложен транскрипт
    content_text = ""
    if transcript:
        try:
            import combo_content
            rppg_csv = workdir / "rppg.csv"
            content = combo_content.run_content_analysis(
                transcript, rppg_csv if rppg_csv.exists() else None,
                speaker=speaker, name=name, out_md=workdir / "content.md")
            summary["content"] = content
            if content.get("ok"):
                content_text = content.get("text", "")
        except Exception as e:
            print(f"контент-анализ не выполнен: {e}")

    # Богатый ассессмент-отчёт через claude CLI (паттерн FS); .docx тоже
    try:
        import combo_report_writer, report_docx
        rich = combo_report_writer.write_rich_report(
            summary["report_md"], content_text, workdir / "report_rich.md", name=name)
        summary["report_rich"] = rich
        if rich.get("ok"):
            report_docx.md_file_to_docx(rich["md_path"], workdir / "report_rich.docx")
    except Exception as e:
        print(f"богатый отчёт не собран: {e}")

    # Данные для веб-интерфейса webapp/combo_live.html (в той концепции, что прислала Виталия)
    try:
        import webapp_data
        web = webapp_data.combo_to_webapp(summary)
        (workdir / "combo_data.json").write_text(
            json.dumps(web, ensure_ascii=False, indent=2), encoding="utf-8")
        # дублируем в webapp-каталог (конфигурируемый), чтобы страница показывала последний разбор
        try:
            cfg.WEBAPP_DIR.mkdir(parents=True, exist_ok=True)
            (cfg.WEBAPP_DIR / "combo_data.json").write_text(
                json.dumps(web, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    except Exception as e:
        print(f"combo_data.json не записан: {e}")
    return summary


def _render_markdown(summary: dict) -> str:
    lines = [f"# Комбо-разбор: {summary.get('label') or '—'}",
             f"_видео: {Path(summary['video']).name} · {summary['created_at']}_", ""]
    mods = summary.get("modules", {})

    emo = mods.get("emotions", {})
    lines.append("## Эмоции")
    if emo.get("skipped"):
        lines.append(f"- модуль пропущен: {emo.get('reason')}")
    elif not emo.get("ok"):
        lines.append(f"- ошибка: {emo.get('error', 'неизвестно')}")
    elif not emo.get("available"):
        lines.append(f"- {emo.get('note', 'лицо не найдено')} (audio-only режим)")
    else:
        lines.append(f"- доминирующая эмоция: **{emo.get('dominant_overall')}**")
        lines.append(f"- лицо найдено в {round(emo.get('face_coverage', 0) * 100)}% кадров")
        perc = emo.get("perception") or {}
        if perc.get("available"):
            d = perc.get("dynamics", {})
            lines.append(f"- воспринимаемое доверие: {perc.get('perceived_trust')}, "
                         f"доминантность: {perc.get('perceived_dominance')}")
            if d:
                lines.append(f"- переключений эмоций/мин: {d.get('emotion_switches_per_min')}, "
                             f"экспрессивность: {d.get('expressivity_index')}")
        ms = emo.get("mean_scores") or {}
        if ms:
            top = sorted(ms.items(), key=lambda kv: kv[1], reverse=True)[:3]
            lines.append("- средние доли: " + ", ".join(f"{k} {v}" for k, v in top))
    lines.append("")

    # Поза
    pose = mods.get("pose", {})
    lines.append("## Поза")
    if pose.get("skipped"):
        lines.append(f"- модуль ещё не подключён ({pose.get('reason')})")
    elif not pose.get("ok"):
        lines.append(f"- ошибка: {pose.get('error', 'неизвестно')}")
    elif not pose.get("available"):
        lines.append(f"- {pose.get('note', 'фигура не найдена')}")
    else:
        lines.append(f"- фигура найдена в {round(pose.get('pose_coverage', 0) * 100)}% кадров")
        lines.append(f"- рука у лица: {pose.get('hand_to_face_events')} эпизодов "
                     f"({round(pose.get('hand_to_face_rate', 0) * 100)}% времени)")
        lines.append(f"- ёрзанье: {pose.get('fidget_level')} ({pose.get('fidget_mean')})")
    lines.append("")

    # Пульс по видео + речь
    rppg = mods.get("rppg", {})
    lines.append("## Пульс по видео + речь")
    if rppg.get("skipped"):
        lines.append(f"- модуль ещё не подключён ({rppg.get('reason')})")
    elif not rppg.get("ok"):
        lines.append(f"- ошибка: {rppg.get('error', 'неизвестно')}")
    else:
        lines.append(f"- режим: **{rppg.get('mode')}**")
        pulse = rppg.get("pulse", {})
        if pulse.get("available"):
            lines.append(f"- пульс: медиана **{pulse.get('hr_median')}** уд/мин "
                         f"(диапазон {pulse.get('hr_min')}–{pulse.get('hr_max')}, окон {pulse.get('windows')})")
        else:
            lines.append(f"- пульс недоступен: {pulse.get('note', '—')}")
        sp = rppg.get("speech", {})
        if sp.get("available"):
            lines.append(f"- речь: доля речи {round(sp.get('speech_ratio', 0) * 100)}%, "
                         f"F0 {sp.get('pitch_mean_hz')}±{sp.get('pitch_std_hz')} Гц, "
                         f"темп {sp.get('tempo_onsets_per_min')}/мин, пауз>1с: {sp.get('pauses_over_1s')}")
            lines.append(f"- E/I: {sp.get('ei_label')} (score {sp.get('ei_score')})")
        else:
            lines.append(f"- речь недоступна: {sp.get('note', '—')}")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--sample-fps", type=float, default=None)
    a = ap.parse_args()
    res = analyze_video(a.video, a.label, a.sample_fps)
    print(res["report_md"])
