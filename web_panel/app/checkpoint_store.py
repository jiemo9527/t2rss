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
            connection.commit()

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
