import os
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from dotenv import dotenv_values


FORWARDER_ENV_KEYS = [
    "API_ID",
    "API_HASH",
    "PHONE",
    "PASSWORD",
    "DESTINATION_CHANNEL",
    "CHANNEL_IDS",
    "CHANNEL_IDENTIFIERS",
    "CHANNEL_SOURCES_JSON",
    "KEYWORD_BLACKLIST",
    "TEXT_REPLACEMENT_TERMS",
    "TEXT_REPLACEMENT_REGEX",
    "USER_ID_BLACKLIST",
    "DEDUPLICATION_ENABLED",
    "DEDUPLICATION_CACHE_SIZE",
]

PANEL_ENV_KEYS = [
    "PANEL_AUTO_RUN_ENABLED",
    "PANEL_AUTO_RUN_INTERVAL_MINUTES",
    "PANEL_TOTAL_TIMEOUT_SECONDS",
    "PANEL_TEST_MODE_ENABLED",
    "PANEL_SESSION_SECRET",
    "PANEL_ADMIN_USERNAME",
    "PANEL_ADMIN_PASSWORD",
    "PANEL_ADMIN_PASSWORD_HASH",
    "PANEL_LOGIN_MAX_FAILURES",
    "PANEL_LOGIN_WINDOW_SECONDS",
    "PANEL_LOGIN_LOCK_SECONDS",
]

ALL_ENV_KEYS = FORWARDER_ENV_KEYS + PANEL_ENV_KEYS

DEFAULT_ENV_VALUES = {
    "API_ID": "",
    "API_HASH": "",
    "PHONE": "",
    "PASSWORD": "",
    "DESTINATION_CHANNEL": "",
    "CHANNEL_IDS": "",
    "CHANNEL_IDENTIFIERS": "",
    "CHANNEL_SOURCES_JSON": "[]",
    "KEYWORD_BLACKLIST": "",
    "TEXT_REPLACEMENT_TERMS": "",
    "TEXT_REPLACEMENT_REGEX": "",
    "USER_ID_BLACKLIST": "",
    "DEDUPLICATION_ENABLED": "false",
    "DEDUPLICATION_CACHE_SIZE": "200",
    "PANEL_AUTO_RUN_ENABLED": "false",
    "PANEL_AUTO_RUN_INTERVAL_MINUTES": "15",
    "PANEL_TOTAL_TIMEOUT_SECONDS": "600",
    "PANEL_TEST_MODE_ENABLED": "false",
    "PANEL_SESSION_SECRET": "",
    "PANEL_ADMIN_USERNAME": "admin",
    "PANEL_ADMIN_PASSWORD": "",
    "PANEL_ADMIN_PASSWORD_HASH": "",
    "PANEL_LOGIN_MAX_FAILURES": "5",
    "PANEL_LOGIN_WINDOW_SECONDS": "600",
    "PANEL_LOGIN_LOCK_SECONDS": "900",
}


@dataclass
class ForwarderConfig:
    api_id: str
    api_hash: str
    phone: str
    password: str
    destination_channel: str
    channel_ids: List[int]
    channel_identifiers: List[str]
    channel_sources: List[Dict[str, Any]]
    keyword_blacklist: List[str]
    text_replacement_terms: List[str]
    text_replacement_regex: str
    user_id_blacklist: Set[int]
    deduplication_enabled: bool
    deduplication_cache_size: int


@dataclass
class PanelSettings:
    auto_run_enabled: bool
    auto_run_interval_minutes: int
    total_timeout_seconds: int
    test_mode_enabled: bool


def parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_csv(value: str) -> List[str]:
    if not value:
        return []
    normalized = str(value).replace("\n", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def parse_int_csv(value: str, field_name: str) -> List[int]:
    items = parse_csv(value)
    numbers: List[int] = []
    for item in items:
        try:
            numbers.append(int(item))
        except ValueError as exc:
            raise ValueError(f"{field_name} 必须为英文逗号分隔的整数。") from exc
    return numbers


def parse_positive_int(value: str, field_name: str, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是有效整数。") from exc

    if parsed <= 0:
        raise ValueError(f"{field_name} 必须大于 0。")
    return parsed


def parse_channel_sources(value: str) -> List[Dict[str, Any]]:
    if not value:
        return []

    try:
        data = json.loads(str(value))
    except json.JSONDecodeError:
        return []

    if not isinstance(data, list):
        return []

    items: List[Dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue

        source = str(row.get("source", "")).strip()
        if not source:
            continue

        cid_value = row.get("cid", None)
        cid: Optional[int] = None
        if cid_value is not None and str(cid_value).strip() != "":
            try:
                cid = int(str(cid_value).strip())
            except ValueError:
                cid = None

        enabled_raw = row.get("enabled", True)
        if isinstance(enabled_raw, bool):
            enabled = enabled_raw
        else:
            enabled = parse_bool(str(enabled_raw), True)

        items.append(
            {
                "source": source,
                "cid": cid,
                "enabled": enabled,
                "status": str(row.get("status", "")),
                "error": str(row.get("error", "")),
            }
        )

    return items


class ConfigStore:
    def __init__(self, data_dir: Optional[Path] = None):
        base_dir = data_dir or Path(os.environ.get("DATA_DIR", "data"))
        self.data_dir = Path(base_dir).resolve()
        self.env_file = self.data_dir / "config.env"
        self.state_dir = self.data_dir / "state"
        self.last_id_dir = self.state_dir / "last_ids"
        self.download_dir = self.state_dir / "downloads"
        self.lock_file = self.state_dir / "forwarder.lock"
        self.session_dir = self.data_dir / "session"
        self.session_base_path = self.session_dir / "t2rss"
        self.session_file = self.session_dir / "t2rss.session"
        self.legacy_session_base_path = self.session_dir / "session_name"
        self.legacy_session_file = self.session_dir / "session_name.session"
        self.backups_dir = self.data_dir / "backups"
        self.log_dir = self.data_dir / "logs"
        self.log_file = self.log_dir / "panel.log"
        self.db_path = self.data_dir / "panel.db"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.last_id_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def migrate_legacy_session_files(self) -> int:
        moved = 0
        pairs = [
            (Path(f"{self.legacy_session_base_path}.session"), Path(f"{self.session_base_path}.session")),
            (Path(f"{self.legacy_session_base_path}.session-journal"), Path(f"{self.session_base_path}.session-journal")),
            (Path(f"{self.legacy_session_base_path}.session-shm"), Path(f"{self.session_base_path}.session-shm")),
            (Path(f"{self.legacy_session_base_path}.session-wal"), Path(f"{self.session_base_path}.session-wal")),
        ]

        for source, target in pairs:
            if source.exists() and not target.exists():
                source.replace(target)
                moved += 1

        return moved

    def load_raw_config(self) -> Dict[str, str]:
        values: Dict[str, str] = {}

        if self.env_file.exists():
            file_values = dotenv_values(self.env_file)
            for key, value in file_values.items():
                if value is None:
                    continue
                values[key] = str(value)

        for key in ALL_ENV_KEYS:
            if key not in values and os.environ.get(key) is not None:
                values[key] = str(os.environ[key])

        for key, default_value in DEFAULT_ENV_VALUES.items():
            values.setdefault(key, default_value)

        return values

    def save_raw_config(self, updated_config: Dict[str, str]) -> None:
        self.ensure_directories()
        current = self.load_raw_config()

        merged = dict(current)
        for key, value in updated_config.items():
            merged[key] = "" if value is None else str(value).replace("\n", " ").strip()

        ordered_keys = list(ALL_ENV_KEYS)
        extra_keys = sorted([key for key in merged.keys() if key not in ALL_ENV_KEYS])
        ordered_keys.extend(extra_keys)

        lines = []
        for key in ordered_keys:
            value = merged.get(key, "")
            lines.append(f"{key}={value}")

        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def build_forwarder_config(self) -> ForwarderConfig:
        raw = self.load_raw_config()

        channel_ids = parse_int_csv(raw.get("CHANNEL_IDS", ""), "CHANNEL_IDS")
        channel_sources = parse_channel_sources(raw.get("CHANNEL_SOURCES_JSON", "[]"))
        user_id_blacklist = set(parse_int_csv(raw.get("USER_ID_BLACKLIST", ""), "USER_ID_BLACKLIST"))

        return ForwarderConfig(
            api_id=raw.get("API_ID", "").strip(),
            api_hash=raw.get("API_HASH", "").strip(),
            phone=raw.get("PHONE", "").strip(),
            password=raw.get("PASSWORD", "").strip(),
            destination_channel=raw.get("DESTINATION_CHANNEL", "").strip(),
            channel_ids=channel_ids,
            channel_identifiers=parse_csv(raw.get("CHANNEL_IDENTIFIERS", "")),
            channel_sources=channel_sources,
            keyword_blacklist=[item.lower() for item in parse_csv(raw.get("KEYWORD_BLACKLIST", ""))],
            text_replacement_terms=parse_csv(raw.get("TEXT_REPLACEMENT_TERMS", "")),
            text_replacement_regex=raw.get("TEXT_REPLACEMENT_REGEX", "").strip(),
            user_id_blacklist=user_id_blacklist,
            deduplication_enabled=parse_bool(raw.get("DEDUPLICATION_ENABLED", "false"), False),
            deduplication_cache_size=parse_positive_int(
                raw.get("DEDUPLICATION_CACHE_SIZE", "200"),
                "DEDUPLICATION_CACHE_SIZE",
                default=200,
            ),
        )

    def build_panel_settings(self) -> PanelSettings:
        raw = self.load_raw_config()
        return PanelSettings(
            auto_run_enabled=parse_bool(raw.get("PANEL_AUTO_RUN_ENABLED", "false"), False),
            auto_run_interval_minutes=parse_positive_int(
                raw.get("PANEL_AUTO_RUN_INTERVAL_MINUTES", "15"),
                "PANEL_AUTO_RUN_INTERVAL_MINUTES",
                default=15,
            ),
            total_timeout_seconds=parse_positive_int(
                raw.get("PANEL_TOTAL_TIMEOUT_SECONDS", "600"),
                "PANEL_TOTAL_TIMEOUT_SECONDS",
                default=600,
            ),
            test_mode_enabled=parse_bool(raw.get("PANEL_TEST_MODE_ENABLED", "false"), False),
        )

    def list_last_ids(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        if not self.last_id_dir.exists():
            return records

        for file_path in sorted(self.last_id_dir.glob("*.txt"), key=lambda item: item.stem):
            channel_id = file_path.stem
            last_id = 0
            try:
                content = file_path.read_text(encoding="utf-8").strip()
                if content:
                    last_id = int(content)
            except (ValueError, OSError):
                last_id = 0
            records.append({"channel_id": channel_id, "last_id": last_id})

        return records
