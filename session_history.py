"""
session_history.py — лёгкая БД сессий в SQLite.

Каждая «сессия» — это завершённая запись (один сегмент или склейка дня).
Хранит общий балл, 7 осей, ключевые HRV-метрики, длительность, % артефактов,
имя пользователя (для случая «дала браслет другу»).

Используется auto_collector.py для сохранения каждого закрытого сегмента
и analyze_session.py для дневной склейки.

Используется виджетом истории на live-странице — для сравнения «сегодня vs вчера».
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


DEFAULT_DB = Path(__file__).resolve().parent / "data" / "sessions.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_label TEXT NOT NULL DEFAULT 'я',
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    source TEXT NOT NULL,
    csv_path TEXT,
    n_rr_raw INTEGER,
    n_rr_clean INTEGER,
    artifacts_pct REAL,
    duration_sec REAL,
    hr_mean REAL,
    rmssd REAL,
    sdnn REAL,
    pnn50 REAL,
    mean_rr REAL,
    sd1 REAL,
    sd2 REAL,
    stress_index REAL,
    lf_power REAL,
    hf_power REAL,
    lf_hf_ratio REAL,
    bio_age REAL,
    rd INTEGER,
    sr INTEGER,
    ad INTEGER,
    fl INTEGER,
    rc INTEGER,
    en INTEGER,
    bl INTEGER,
    overall_score INTEGER,
    state_text TEXT,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_date ON sessions(user_label, started_at);
"""


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save_session(
    conn: sqlite3.Connection,
    user_label: str,
    started_at: datetime,
    ended_at: datetime,
    source: str,
    csv_path: Optional[str],
    n_rr_raw: int,
    n_rr_clean: int,
    artifacts_pct: float,
    duration_sec: float,
    project_metrics: dict,
    axis_scores: dict,
    overall_score: int,
    state_text: str,
    note: str = "",
) -> int:
    row = {
        "user_label": user_label,
        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "ended_at": ended_at.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "csv_path": csv_path,
        "n_rr_raw": n_rr_raw,
        "n_rr_clean": n_rr_clean,
        "artifacts_pct": float(artifacts_pct) if artifacts_pct is not None else None,
        "duration_sec": float(duration_sec) if duration_sec is not None else None,
        "hr_mean": project_metrics.get("mean_hr"),
        "rmssd": project_metrics.get("rmssd"),
        "sdnn": project_metrics.get("sdnn"),
        "pnn50": project_metrics.get("pnn50"),
        "mean_rr": project_metrics.get("mean_rr"),
        "sd1": project_metrics.get("sd1"),
        "sd2": project_metrics.get("sd2"),
        "stress_index": project_metrics.get("stress_index"),
        "lf_power": project_metrics.get("lf_power"),
        "hf_power": project_metrics.get("hf_power"),
        "lf_hf_ratio": project_metrics.get("lf_hf_ratio"),
        "bio_age": project_metrics.get("biological_age"),
        "rd": axis_scores.get("RD"),
        "sr": axis_scores.get("SR"),
        "ad": axis_scores.get("AD"),
        "fl": axis_scores.get("FL"),
        "rc": axis_scores.get("RC"),
        "en": axis_scores.get("EN"),
        "bl": axis_scores.get("BL"),
        "overall_score": int(overall_score) if overall_score is not None else None,
        "state_text": state_text,
        "note": note,
    }
    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{k}" for k in row.keys())
    cur = conn.execute(f"INSERT INTO sessions ({cols}) VALUES ({placeholders})", row)
    conn.commit()
    return int(cur.lastrowid)


def recent_sessions(conn: sqlite3.Connection, user_label: str = "я", days: int = 14, limit: int = 50) -> list[dict]:
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT * FROM sessions WHERE user_label = ? AND started_at >= ? ORDER BY started_at DESC LIMIT ?",
        (user_label, cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def last_session(conn: sqlite3.Connection, user_label: str = "я", before: Optional[datetime] = None) -> Optional[dict]:
    if before is None:
        before = datetime.now()
    row = conn.execute(
        "SELECT * FROM sessions WHERE user_label = ? AND ended_at < ? ORDER BY ended_at DESC LIMIT 1",
        (user_label, before.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    return dict(row) if row else None


def daily_aggregate(conn: sqlite3.Connection, user_label: str = "я", days: int = 30) -> list[dict]:
    """Возвращает агрегаты по дням: средний overall, средний RMSSD, кол-во сессий."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """
        SELECT
            substr(started_at, 1, 10) as day,
            AVG(overall_score) as avg_overall,
            AVG(rmssd) as avg_rmssd,
            AVG(sdnn) as avg_sdnn,
            AVG(stress_index) as avg_si,
            AVG(lf_hf_ratio) as avg_lfhf,
            COUNT(*) as n_sessions,
            SUM(duration_sec) as total_sec
        FROM sessions
        WHERE user_label = ? AND started_at >= ?
        GROUP BY day
        ORDER BY day DESC
        """,
        (user_label, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def list_users(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT user_label FROM sessions ORDER BY user_label").fetchall()
    return [r["user_label"] for r in rows] or ["я"]
