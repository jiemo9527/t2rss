from pathlib import Path
import shutil
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

    def create_backup(self) -> Path:
        self.ensure_directory()
        timestamp = now_shanghai().strftime("%Y%m%d_%H%M%S")
        backup_file = self.backups_dir / f"t2rss_backup_{timestamp}.zip"

        with zipfile.ZipFile(backup_file, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self.data_dir.rglob("*")):
                if path.is_dir():
                    continue
                if path == backup_file:
                    continue
                if self.backups_dir in path.parents:
                    continue
                archive.write(path, arcname=str(path.relative_to(self.data_dir)))

        return backup_file

    def create_backup_with_prefix(self, prefix: str) -> Path:
        self.ensure_directory()
        safe_prefix = "".join(ch for ch in str(prefix) if ch.isalnum() or ch in {"_", "-"}).strip() or "backup"
        timestamp = now_shanghai().strftime("%Y%m%d_%H%M%S")
        backup_file = self.backups_dir / f"{safe_prefix}_{timestamp}.zip"

        with zipfile.ZipFile(backup_file, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self.data_dir.rglob("*")):
                if path.is_dir():
                    continue
                if path == backup_file:
                    continue
                if self.backups_dir in path.parents:
                    continue
                archive.write(path, arcname=str(path.relative_to(self.data_dir)))

        return backup_file

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
