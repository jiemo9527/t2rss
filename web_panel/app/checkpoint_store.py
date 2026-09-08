import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from .time_utils import normalize_to_shanghai_iso, now_shanghai_iso


class ChannelCheckpointStore:
    """频道断点存储，使用数据库替代 last_id 文本文件。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_last_id (
                    channel_id INTEGER PRIMARY KEY,
                    last_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Per-message send-failure ledger. Without it a message that can
            # never be sent (e.g. Telegram refuses to serve its media file)
            # holds its channel checkpoint forever, so every 2-minute run
            # re-fetches the same backlog and retries the same message
            # endlessly.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS message_failures (
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    confirmed_count INTEGER NOT NULL DEFAULT 0,
                    first_failed_at TEXT NOT NULL,
                    last_failed_at TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (channel_id, message_id)
                )
                """
            )
            connection.commit()

    # ------------------------------------------------------------------
    # Per-message failure ledger
    # ------------------------------------------------------------------

    def get_failure_counts(self, channel_id: int) -> Dict[int, Dict[str, Any]]:
        """Return {message_id: {attempt_count, confirmed_count}} for a channel."""
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT message_id, attempt_count, confirmed_count
                FROM message_failures
                WHERE channel_id = ?
                """,
                (int(channel_id),),
            ).fetchall()

        return {
            int(row[0]): {"attempt_count": int(row[1]), "confirmed_count": int(row[2])}
            for row in rows
        }

    def record_failure(
        self,
        channel_id: int,
        message_id: int,
        confirmed: bool,
        error: str = "",
    ) -> Dict[str, int]:
        """Bump a message's failure counters and return the new counts.

        `confirmed` means the run proved the pipeline was otherwise healthy
        (it forwarded at least one other message), so this failure is
        attributable to the message itself rather than to a network/Telegram
        outage. Only confirmed failures count towards giving up quickly.
        """
        channel_id = int(channel_id)
        message_id = int(message_id)
        stamp = now_shanghai_iso()
        error_text = str(error or "")[:500]

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO message_failures (
                    channel_id, message_id, attempt_count, confirmed_count,
                    first_failed_at, last_failed_at, last_error
                )
                VALUES (?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(channel_id, message_id)
                DO UPDATE SET
                    attempt_count = attempt_count + 1,
                    confirmed_count = confirmed_count + ?,
                    last_failed_at = excluded.last_failed_at,
                    last_error = excluded.last_error
                """,
                (
                    channel_id,
                    message_id,
                    1 if confirmed else 0,
                    stamp,
                    stamp,
                    error_text,
                    1 if confirmed else 0,
                ),
            )
            row = connection.execute(
                """
                SELECT attempt_count, confirmed_count
                FROM message_failures
                WHERE channel_id = ? AND message_id = ?
                """,
                (channel_id, message_id),
            ).fetchone()
            connection.commit()

        return {"attempt_count": int(row[0]), "confirmed_count": int(row[1])}

    def clear_failure(self, channel_id: int, message_id: int) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "DELETE FROM message_failures WHERE channel_id = ? AND message_id = ?",
                (int(channel_id), int(message_id)),
            )
            connection.commit()

    def prune_failures_below(self, channel_last_ids: Dict[int, int]) -> int:
        """Drop ledger rows the checkpoint has already moved past."""
        if not channel_last_ids:
            return 0

        removed = 0
        with sqlite3.connect(self.db_path) as connection:
            for channel_id, last_id in channel_last_ids.items():
                cursor = connection.execute(
                    "DELETE FROM message_failures WHERE channel_id = ? AND message_id <= ?",
                    (int(channel_id), int(last_id)),
                )
                removed += cursor.rowcount or 0
            connection.commit()
        return removed

    def list_failures(self) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT channel_id, message_id, attempt_count, confirmed_count,
                       first_failed_at, last_failed_at, last_error
                FROM message_failures
                ORDER BY channel_id ASC, message_id ASC
                """
            ).fetchall()

        return [
            {
                "channel_id": int(row["channel_id"]),
                "message_id": int(row["message_id"]),
                "attempt_count": int(row["attempt_count"]),
                "confirmed_count": int(row["confirmed_count"]),
                "first_failed_at": normalize_to_shanghai_iso(row["first_failed_at"]),
                "last_failed_at": normalize_to_shanghai_iso(row["last_failed_at"]),
                "last_error": str(row["last_error"] or ""),
            }
            for row in rows
        ]

    def migrate_from_files(self, last_id_dir: Path) -> int:
        """将旧的 last_id 文本文件迁移到数据库。"""
        last_id_dir = Path(last_id_dir)
        if not last_id_dir.exists():
            return 0

        migrated = 0
        with sqlite3.connect(self.db_path) as connection:
            for file_path in sorted(last_id_dir.glob("*.txt"), key=lambda item: item.stem):
                try:
                    channel_id = int(file_path.stem)
                except ValueError:
                    continue

                try:
                    content = file_path.read_text(encoding="utf-8").strip()
                    last_id = int(content) if content else 0
                except (OSError, ValueError):
                    last_id = 0

                current = connection.execute(
                    "SELECT last_id FROM channel_last_id WHERE channel_id = ?",
                    (channel_id,),
                ).fetchone()

                if current is None:
                    connection.execute(
                        """
                        INSERT INTO channel_last_id (channel_id, last_id, updated_at)
                        VALUES (?, ?, ?)
                        """,
                        (channel_id, last_id, now_shanghai_iso()),
                    )
                    migrated += 1
                else:
                    current_last_id = int(current[0])
                    if last_id > current_last_id:
                        connection.execute(
                            """
                            UPDATE channel_last_id
                            SET last_id = ?, updated_at = ?
                            WHERE channel_id = ?
                            """,
                            (last_id, now_shanghai_iso(), channel_id),
                        )
                        migrated += 1

            connection.commit()

        return migrated

    def get_last_id(self, channel_id: int) -> int:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT last_id FROM channel_last_id WHERE channel_id = ?",
                (int(channel_id),),
            ).fetchone()

        if not row:
            return 0
        return int(row[0])

    def set_last_id(self, channel_id: int, last_id: int) -> None:
        channel_id = int(channel_id)
        last_id = int(last_id)
        if last_id < 0:
            raise ValueError("last_id 不能为负数。")

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO channel_last_id (channel_id, last_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(channel_id)
                DO UPDATE SET
                    last_id = excluded.last_id,
                    updated_at = excluded.updated_at
                """,
                (channel_id, last_id, now_shanghai_iso()),
            )
            connection.commit()

    def bulk_update(self, channel_last_ids: Dict[int, int]) -> None:
        if not channel_last_ids:
            return

        with sqlite3.connect(self.db_path) as connection:
            for channel_id, last_id in channel_last_ids.items():
                connection.execute(
                    """
                    INSERT INTO channel_last_id (channel_id, last_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(channel_id)
                        DO UPDATE SET
                            last_id = excluded.last_id,
                            updated_at = excluded.updated_at
                    """,
                    (int(channel_id), int(last_id), now_shanghai_iso()),
                )
            connection.commit()

    def list_last_ids(self) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT channel_id, last_id, updated_at
                FROM channel_last_id
                ORDER BY channel_id ASC
                """
            ).fetchall()

        return [
            {
                "channel_id": int(row["channel_id"]),
                "last_id": int(row["last_id"]),
                "updated_at": normalize_to_shanghai_iso(row["updated_at"]),
            }
            for row in rows
        ]

    def get_record(self, channel_id: int) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT channel_id, last_id, updated_at
                FROM channel_last_id
                WHERE channel_id = ?
                """,
                (int(channel_id),),
            ).fetchone()

        if not row:
            return None

        return {
            "channel_id": int(row["channel_id"]),
            "last_id": int(row["last_id"]),
            "updated_at": normalize_to_shanghai_iso(row["updated_at"]),
        }

    def delete_last_id(self, channel_id: int) -> bool:
        with sqlite3.connect(self.db_path) as connection:
            cursor = connection.execute(
                "DELETE FROM channel_last_id WHERE channel_id = ?",
                (int(channel_id),),
            )
            connection.commit()
            return cursor.rowcount > 0
