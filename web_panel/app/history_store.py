import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from .time_utils import SHANGHAI_TZ, normalize_to_shanghai_iso


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

    def prune_old_records(self, retention_days: int = 30) -> int:
        """删除超过保留期的运行记录，返回删除条数。retention_days<=0 表示不清理。"""
        if retention_days <= 0:
            return 0
        cutoff = (
            datetime.now(SHANGHAI_TZ) - timedelta(days=int(retention_days))
        ).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self.db_path) as connection:
            cursor = connection.execute(
                "DELETE FROM run_history WHERE started_at < ?",
                (cutoff,),
            )
            connection.commit()
            return cursor.rowcount

    def vacuum(self) -> None:
        """回收数据库空间（在批量删除后调用）。"""
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("VACUUM")

    def per_channel_fetched_totals(self, windows_days: Dict[str, int]) -> Dict[str, Dict[int, int]]:
        """按时间窗口聚合每个来源频道的抓取量（stats_json.per_channel_fetched）。

        返回 {window_name: {channel_id: fetched_total}}。用于仪表盘的来源产出统计（D1）。
        以 Python 累加，避免依赖 SQLite 的 JSON 扩展。
        """
        result: Dict[str, Dict[int, int]] = {name: {} for name in windows_days}
        if not windows_days:
            return result

        max_days = max(windows_days.values())
        cutoff = (datetime.now(SHANGHAI_TZ) - timedelta(days=int(max_days))).strftime("%Y-%m-%d %H:%M:%S")
        cutoffs = {
            name: (datetime.now(SHANGHAI_TZ) - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
            for name, days in windows_days.items()
        }

        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT started_at, stats_json FROM run_history WHERE started_at >= ? AND stats_json IS NOT NULL",
                (cutoff,),
            ).fetchall()

        for started_at, stats_json in rows:
            if not stats_json:
                continue
            try:
                payload = json.loads(stats_json)
            except (json.JSONDecodeError, TypeError):
                continue
            per_channel = payload.get("per_channel_fetched") or {}
            if not isinstance(per_channel, dict):
                continue
            for name, cut in cutoffs.items():
                if started_at < cut:
                    continue
                bucket = result[name]
                for cid_str, count in per_channel.items():
                    try:
                        cid_int = int(cid_str)
                        count_int = int(count or 0)
                    except (ValueError, TypeError):
                        continue
                    if count_int:
                        bucket[cid_int] = bucket.get(cid_int, 0) + count_int
        return result

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
