from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid
from typing import Any, Dict, List, Optional
import zipfile

from .time_utils import now_shanghai, timestamp_to_shanghai_iso


class BackupManager:
    def __init__(self, data_dir: Path, backups_dir: Path):
        self.data_dir = Path(data_dir)
        self.backups_dir = Path(backups_dir)

    def ensure_directory(self) -> None:
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    def list_backups(self) -> List[Dict[str, Any]]:
        self.ensure_directory()
        backups: List[Dict[str, Any]] = []
        for file_path in sorted(self.backups_dir.glob("*.zip"), reverse=True):
            stat = file_path.stat()
            backups.append(
                {
                    "name": file_path.name,
                    "size_bytes": stat.st_size,
                    "modified_at": timestamp_to_shanghai_iso(stat.st_mtime),
                }
            )
        return backups

    def _make_slim_db_copy(self, db_path: Path) -> Optional[Path]:
        """生成一份剔除 run_history（体积大头）的临时数据库副本，用于瘦身备份。

        保留 channel_last_id/login_guard 等关键表；失败时返回 None，回退为原库。
        """
        try:
            fd, tmp_name = tempfile.mkstemp(prefix="t2rss_slimdb_", suffix=".db")
            import os as _os

            _os.close(fd)
            tmp_path = Path(tmp_name)
            source_connection = sqlite3.connect(db_path)
            target_connection = sqlite3.connect(tmp_path)
            try:
                # SQLite 在线备份可得到包含 WAL 已提交内容的一致快照。
                source_connection.backup(target_connection)
            finally:
                target_connection.close()
                source_connection.close()

            connection = sqlite3.connect(tmp_path)
            try:
                has_run_history = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='run_history'"
                ).fetchone()
                if has_run_history:
                    connection.execute("DELETE FROM run_history")
                    connection.commit()
                connection.execute("VACUUM")
            finally:
                connection.close()
            return tmp_path
        except Exception:
            return None

    def _write_archive(self, backup_file: Path) -> Path:
        slim_db = self._make_slim_db_copy(self.data_dir / "panel.db")
        db_abs = (self.data_dir / "panel.db").resolve()
        try:
            with zipfile.ZipFile(backup_file, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(self.data_dir.rglob("*")):
                    if path.is_dir():
                        continue
                    if path == backup_file:
                        continue
                    if self.backups_dir in path.parents:
                        continue
                    if slim_db is not None:
                        if path.resolve() == db_abs:
                            archive.write(slim_db, arcname="panel.db")
                            continue
                        if path.name in {"panel.db-wal", "panel.db-shm", "panel.db-journal"}:
                            continue
                    archive.write(path, arcname=str(path.relative_to(self.data_dir)))
        finally:
            if slim_db is not None:
                slim_db.unlink(missing_ok=True)
        return backup_file

    def create_backup(self) -> Path:
        self.ensure_directory()
        timestamp = now_shanghai().strftime("%Y%m%d_%H%M%S")
        backup_file = self.backups_dir / f"t2rss_backup_{timestamp}.zip"
        return self._write_archive(backup_file)

    def create_backup_with_prefix(self, prefix: str) -> Path:
        self.ensure_directory()
        safe_prefix = "".join(ch for ch in str(prefix) if ch.isalnum() or ch in {"_", "-"}).strip() or "backup"
        timestamp = now_shanghai().strftime("%Y%m%d_%H%M%S")
        backup_file = self.backups_dir / f"{safe_prefix}_{timestamp}.zip"
        return self._write_archive(backup_file)

    def resolve_backup(self, backup_name: str) -> Optional[Path]:
        if not backup_name or "/" in backup_name or "\\" in backup_name:
            return None
        if not backup_name.endswith(".zip"):
            return None

        self.ensure_directory()
        candidate = (self.backups_dir / backup_name).resolve()
        if candidate.parent != self.backups_dir.resolve():
            return None
        if not candidate.exists():
            return None
        return candidate

    def delete_backup(self, backup_name: str) -> bool:
        backup_path = self.resolve_backup(backup_name)
        if backup_path is None:
            return False
        backup_path.unlink(missing_ok=True)
        return True

    def restore_from_backup(self, backup_path: Path) -> Dict[str, int]:
        backup_path = Path(backup_path)
        if not backup_path.exists():
            raise FileNotFoundError("备份文件不存在。")

        temp_dir = self.backups_dir / f".restore_tmp_{uuid.uuid4().hex}"
        temp_dir.mkdir(parents=True, exist_ok=False)

        deleted_count = 0
        copied_count = 0

        try:
            with zipfile.ZipFile(backup_path, "r") as archive:
                members = archive.infolist()
                for member in members:
                    member_path = Path(member.filename)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise ValueError("备份文件包含非法路径，已拒绝恢复。")

                archive.extractall(temp_dir)

            for existing in self.data_dir.iterdir():
                if existing == self.backups_dir:
                    continue
                if existing.name.startswith(".restore_tmp_"):
                    continue

                if existing.is_dir():
                    shutil.rmtree(existing)
                else:
                    existing.unlink(missing_ok=True)
                deleted_count += 1

            for extracted in temp_dir.iterdir():
                if extracted.name == self.backups_dir.name:
                    continue

                target = self.data_dir / extracted.name
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink(missing_ok=True)

                if extracted.is_dir():
                    shutil.copytree(extracted, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(extracted, target)
                copied_count += 1

            return {
                "deleted_count": deleted_count,
                "copied_count": copied_count,
            }
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
