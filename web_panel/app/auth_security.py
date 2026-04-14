import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional


def build_password_hash(password: str, iterations: int = 260000) -> str:
    """使用 PBKDF2-SHA256 生成密码哈希。"""
    if not password:
        raise ValueError("密码不能为空。")

    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, password_hash: str, legacy_password: str = "") -> bool:
    """校验密码，优先使用哈希，兼容旧的明文密码。"""
    if password_hash:
        parts = password_hash.split("$")
        if len(parts) == 4 and parts[0] == "pbkdf2_sha256":
            try:
                iterations = int(parts[1])
                salt = parts[2]
                expected = parts[3]
                digest = hashlib.pbkdf2_hmac(
                    "sha256",
                    password.encode("utf-8"),
                    bytes.fromhex(salt),
                    iterations,
                )
                return hmac.compare_digest(digest.hex(), expected)
            except (ValueError, TypeError):
                return False

    if legacy_password:
        return hmac.compare_digest(password, legacy_password)

    return False


def _safe_positive_int(value: Any, default_value: int) -> int:
    try:
        parsed = int(str(value).strip())
        if parsed <= 0:
            return default_value
        return parsed
    except Exception:
        return default_value


class LoginGuardStore:
    """登录防爆破存储：按 IP 和账户维度统计失败并锁定。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS login_guard (
                    ip TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    first_failure_ts INTEGER NOT NULL DEFAULT 0,
                    locked_until_ts INTEGER NOT NULL DEFAULT 0,
                    updated_at_ts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (ip, scope)
                )
                """
            )
            connection.commit()

    def _get_record(self, connection: sqlite3.Connection, ip: str, scope: str) -> Optional[sqlite3.Row]:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT ip, scope, failure_count, first_failure_ts, locked_until_ts, updated_at_ts
            FROM login_guard
            WHERE ip = ? AND scope = ?
            """,
            (ip, scope),
        ).fetchone()

    def _upsert_record(
        self,
        connection: sqlite3.Connection,
        ip: str,
        scope: str,
        failure_count: int,
        first_failure_ts: int,
        locked_until_ts: int,
        updated_at_ts: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO login_guard (ip, scope, failure_count, first_failure_ts, locked_until_ts, updated_at_ts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ip, scope)
            DO UPDATE SET
                failure_count = excluded.failure_count,
                first_failure_ts = excluded.first_failure_ts,
                locked_until_ts = excluded.locked_until_ts,
                updated_at_ts = excluded.updated_at_ts
            """,
            (ip, scope, failure_count, first_failure_ts, locked_until_ts, updated_at_ts),
        )

    def get_lock_seconds(self, ip: str, username: str, now_ts: Optional[int] = None) -> int:
        now_ts = now_ts or int(time.time())
        lock_seconds = 0

        with sqlite3.connect(self.db_path) as connection:
            for scope in (username, "*"):
                record = self._get_record(connection, ip, scope)
                if not record:
                    continue

                remaining = int(record["locked_until_ts"]) - now_ts
                if remaining > lock_seconds:
                    lock_seconds = remaining

        return max(0, lock_seconds)

    def record_failure(self, ip: str, username: str, raw_config: Dict[str, str]) -> int:
        max_failures = _safe_positive_int(raw_config.get("PANEL_LOGIN_MAX_FAILURES", "5"), 5)
        window_seconds = _safe_positive_int(raw_config.get("PANEL_LOGIN_WINDOW_SECONDS", "600"), 600)
        lock_seconds = _safe_positive_int(raw_config.get("PANEL_LOGIN_LOCK_SECONDS", "900"), 900)
        now_ts = int(time.time())

        with sqlite3.connect(self.db_path) as connection:
            for scope in (username, "*"):
                record = self._get_record(connection, ip, scope)

                if not record:
                    failure_count = 1
                    first_failure_ts = now_ts
                    locked_until_ts = 0
                else:
                    old_failure_count = int(record["failure_count"])
                    old_first_failure_ts = int(record["first_failure_ts"])
                    old_locked_until_ts = int(record["locked_until_ts"])

                    if old_locked_until_ts > now_ts:
                        failure_count = old_failure_count + 1
                        first_failure_ts = old_first_failure_ts or now_ts
                        locked_until_ts = old_locked_until_ts
                    else:
                        if old_first_failure_ts <= 0 or (now_ts - old_first_failure_ts) > window_seconds:
                            failure_count = 1
                            first_failure_ts = now_ts
                        else:
                            failure_count = old_failure_count + 1
                            first_failure_ts = old_first_failure_ts
                        locked_until_ts = 0

                if failure_count >= max_failures:
                    locked_until_ts = now_ts + lock_seconds
                    failure_count = 0
                    first_failure_ts = 0

                self._upsert_record(
                    connection=connection,
                    ip=ip,
                    scope=scope,
                    failure_count=failure_count,
                    first_failure_ts=first_failure_ts,
                    locked_until_ts=locked_until_ts,
                    updated_at_ts=now_ts,
                )

            connection.commit()

        return self.get_lock_seconds(ip, username, now_ts=now_ts)

    def clear_failures(self, ip: str, username: str) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                DELETE FROM login_guard
                WHERE ip = ? AND (scope = ? OR scope = '*')
                """,
                (ip, username),
            )
            connection.commit()


def ensure_auth_baseline(raw_config: Dict[str, str]) -> tuple[Dict[str, str], str]:
    """保证认证基础配置存在，缺失时自动补齐。"""
    updates: Dict[str, str] = {}
    initial_password = ""

    if not str(raw_config.get("PANEL_ADMIN_USERNAME", "")).strip():
        updates["PANEL_ADMIN_USERNAME"] = "admin"

    if not str(raw_config.get("PANEL_SESSION_SECRET", "")).strip():
        updates["PANEL_SESSION_SECRET"] = secrets.token_urlsafe(48)

    has_hash = bool(str(raw_config.get("PANEL_ADMIN_PASSWORD_HASH", "")).strip())
    has_plain = bool(str(raw_config.get("PANEL_ADMIN_PASSWORD", "")).strip())
    if not has_hash and not has_plain:
        initial_password = secrets.token_urlsafe(12)
        updates["PANEL_ADMIN_PASSWORD_HASH"] = build_password_hash(initial_password)
        updates["PANEL_ADMIN_PASSWORD"] = ""

    return updates, initial_password
