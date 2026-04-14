import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from .time_utils import normalize_to_shanghai_iso


class RunHistoryStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS run_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT,
                    fetched_total INTEGER NOT NULL DEFAULT 0,
                    final_total INTEGER NOT NULL DEFAULT 0,
                    forwarded_total INTEGER NOT NULL DEFAULT 0,
                    error_total INTEGER NOT NULL DEFAULT 0,
                    stats_json TEXT
                )
                """
            )
            connection.commit()

    def add_record(self, result: Dict[str, Any]) -> None:
        stats = result.get("stats", {})
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO run_history (
                    started_at,
                    finished_at,
                    trigger,
                    status,
                    message,
                    fetched_total,
                    final_total,
                    forwarded_total,
                    error_total,
                    stats_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.get("started_at", ""),
                    result.get("finished_at", ""),
                    result.get("trigger", "manual"),
                    result.get("status", "error"),
                    result.get("message", ""),
                    int(stats.get("fetched_total", 0)),
                    int(stats.get("after_dedup_total", 0)),
                    int(stats.get("forwarded_total", 0)),
                    int(stats.get("error_total", 0)),
                    json.dumps(stats, ensure_ascii=False),
                ),
            )
            connection.commit()

    def list_records(self, limit: int = 30) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT
                    id,
                    started_at,
                    finished_at,
                    trigger,
                    status,
                    message,
                    fetched_total,
                    final_total,
                    forwarded_total,
                    error_total,
                    stats_json
                FROM run_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()

        records: List[Dict[str, Any]] = []
        for row in rows:
            stats_payload = {}
            stats_json = row["stats_json"]
            if stats_json:
                try:
                    stats_payload = json.loads(stats_json)
                except json.JSONDecodeError:
                    stats_payload = {}

            records.append(
                {
                    "id": row["id"],
                    "started_at": normalize_to_shanghai_iso(row["started_at"]),
                    "finished_at": normalize_to_shanghai_iso(row["finished_at"]),
                    "trigger": row["trigger"],
                    "status": row["status"],
                    "message": row["message"],
                    "fetched_total": row["fetched_total"],
                    "final_total": row["final_total"],
                    "forwarded_total": row["forwarded_total"],
                    "error_total": row["error_total"],
                    "stats": stats_payload,
                    "stats_pretty": json.dumps(stats_payload, ensure_ascii=False, indent=2),
                }
            )
        return records
