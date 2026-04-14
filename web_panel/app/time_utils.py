from datetime import datetime, timezone
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
TIME_TEXT_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def now_shanghai_iso() -> str:
    return now_shanghai().strftime(TIME_TEXT_FORMAT)


def timestamp_to_shanghai_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(SHANGHAI_TZ).strftime(TIME_TEXT_FORMAT)


def normalize_to_shanghai_iso(time_text: str) -> str:
    raw = str(time_text or "").strip()
    if not raw:
        return ""

    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return raw

    if dt.tzinfo is None:
        # 无时区信息时，按上海本地时间解释。
        return dt.strftime(TIME_TEXT_FORMAT)

    return dt.astimezone(SHANGHAI_TZ).strftime(TIME_TEXT_FORMAT)
