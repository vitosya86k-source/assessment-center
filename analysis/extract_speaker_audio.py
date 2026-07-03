"""
Нарезать сегменты речи участника из m4a по таймштампам Deepgram-транскрипта (.docx).

Использование:
    python extract_speaker_audio.py --audio file.m4a --docx file.docx \
        --speaker "Спикер 0" --out alex_seg.wav

Создаёт ОДИН .wav 16kHz mono со всеми склеенными сегментами участника
(тишина между сегментами — 100 мс паузы для VAD-стабильности).
"""

import re, subprocess, argparse, tempfile
from pathlib import Path
import docx as docx_lib


def parse_turns(docx_path, speaker_label):
    doc = docx_lib.Document(str(docx_path))
    full = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    m = re.search(r"Полный диалог.*?\n(.*)", full, re.S)
    body = m.group(1) if m else full
    items = re.findall(
        r"\[(\d+):(\d+):(\d+(?:\.\d+)?)\] " + re.escape(speaker_label) + r": (.*?)(?=\n\[|\Z)",
        body, re.S,
    )
    turns = []
    for hh, mm, ss, txt in items:
        t = int(hh) * 3600 + int(mm) * 60 + float(ss)
        turns.append((t, txt.strip()))
    return turns


def estimate_turn_duration(text, wpm=160):
    """Грубая оценка длительности реплики по тексту: 160 wpm."""
    words = len(text.split())
    return max(0.5, words * 60.0 / wpm)


def extract_segments(audio_path, turns, out_wav, sr=16000):
    """Извлекаем каждый turn в свой временный wav + concat."""
    tmp_dir = Path(tempfile.mkdtemp())
    seg_files = []
    for i, (t0, text) in enumerate(turns):
        dur = estimate_turn_duration(text)
        seg = tmp_dir / f"seg_{i:04d}.wav"
        cmd = [
            "ffmpeg", "-loglevel", "error", "-y",
            "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}",
            "-i", str(audio_path),
            "-ar", str(sr), "-ac", "1",
            str(seg),
        ]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and seg.exists() and seg.stat().st_size > 0:
            seg_files.append(seg)

    # concat list
    list_file = tmp_dir / "list.txt"
    list_file.write_text("\n".join(f"file '{s}'" for s in seg_files))
    cmd = [
        "ffmpeg", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-ar", str(sr), "-ac", "1",
        str(out_wav),
    ]
    subprocess.run(cmd, capture_output=True, check=True)

    total_dur = sum(
        float(subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(s)]
        ).strip())
        for s in seg_files
    ) if seg_files else 0
    return len(seg_files), total_dur


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--audio", required=True)
    p.add_argument("--docx", required=True)
    p.add_argument("--speaker", default="Спикер 0")
    p.add_argument("--out", required=True)
    args = p.parse_args()
    turns = parse_turns(args.docx, args.speaker)
    n, dur = extract_segments(args.audio, turns, args.out)
    print(f"{Path(args.audio).name}: {n} сегментов, {dur:.1f}с -> {args.out}")


if __name__ == "__main__":
    main()
