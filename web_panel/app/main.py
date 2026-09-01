import asyncio
import hmac
import html
import json
import os
import re
import secrets
import shutil
from datetime import datetime, timezone
from email.utils import format_datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlencode

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.middleware.sessions import SessionMiddleware

from .auth_security import LoginGuardStore, build_password_hash, ensure_auth_baseline, verify_password
from .backup_manager import BackupManager
from .checkpoint_store import ChannelCheckpointStore
from .config_store import ConfigStore, parse_bool, parse_channel_sources, parse_csv, parse_int_csv
from .forwarder_service import ForwarderRunner, resolve_identifiers_preview
from .history_store import RunHistoryStore
from .logging_utils import create_logger, rebind_logger_file_handler
from .time_utils import now_shanghai_iso, timestamp_to_shanghai_iso
from telethon import TelegramClient
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl


BASE_DIR = Path(__file__).resolve().parent

config_store = ConfigStore()
config_store.ensure_directories()

logger = create_logger(config_store.log_file)
history_store = RunHistoryStore(config_store.db_path)
login_guard_store = LoginGuardStore(config_store.db_path)
checkpoint_store = ChannelCheckpointStore(config_store.db_path)
backup_manager = BackupManager(config_store.data_dir, config_store.backups_dir)
runner = ForwarderRunner(config_store, checkpoint_store, history_store, logger)

bootstrap_updates, bootstrap_password = ensure_auth_baseline(config_store.load_raw_config())
if bootstrap_updates:
    config_store.save_raw_config(bootstrap_updates)
if bootstrap_password:
    bootstrap_username = bootstrap_updates.get("PANEL_ADMIN_USERNAME", "admin")
    logger.warning(
        "首次启动已自动生成管理员账户，用户名: %s，初始密码: %s，请登录后立即修改。",
        bootstrap_username,
        bootstrap_password,
    )

rss_token_config = config_store.load_raw_config()
if not str(rss_token_config.get("PANEL_RSS_TOKEN", "")).strip():
    config_store.save_raw_config({"PANEL_RSS_TOKEN": secrets.token_urlsafe(24)})
    logger.info("已生成 RSS 订阅 token。")

raw_for_secret = config_store.load_raw_config()
session_secret = str(raw_for_secret.get("PANEL_SESSION_SECRET", "")).strip() or os.environ.get("PANEL_SESSION_SECRET", "")
if not session_secret:
    session_secret = secrets.token_urlsafe(48)
    logger.warning("未配置 PANEL_SESSION_SECRET，当前进程使用临时会话密钥。建议在配置页中设置固定值。")

app = FastAPI(title="T2RSS 管理面板", version="1.0.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    session_cookie="t2rss_panel_session",
    max_age=60 * 60 * 12,
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
RSS_REFRESH_TIMEOUT_SECONDS = 20
RSS_BACKGROUND_REFRESH_TIMEOUT_SECONDS = 300
RSS_IMAGE_DISPLAY_WIDTH = 450
RSS_IMAGE_DISPLAY_MAX_HEIGHT = 450
RSS_IMAGE_JPEG_QUALITY = 85
RSS_IMAGE_CACHE_VERSION = f"img{RSS_IMAGE_DISPLAY_WIDTH}x{RSS_IMAGE_DISPLAY_MAX_HEIGHT}"
RSS_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
RSS_URL_TRAILING_CHARS = ".,;:!?)]}，。！？、；：）】》"
RSS_MEDIA_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
RSS_IMAGE_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
RSS_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
rss_refresh_task: asyncio.Task | None = None


@app.on_event("startup")
async def on_startup() -> None:
    config_store.ensure_directories()
    moved_sessions = config_store.migrate_legacy_session_files()
    if moved_sessions > 0:
        logger.info("已将旧会话文件迁移到 t2rss.session，迁移文件数: %s", moved_sessions)
    backup_manager.ensure_directory()
    history_store.init_db()
    login_guard_store.init_db()
    checkpoint_store.init_db()
    migrated = checkpoint_store.migrate_from_files(config_store.last_id_dir)
    if migrated > 0:
        logger.info("已将旧版 last_id 文本记录迁移到数据库，共 %s 条。", migrated)
    _prune_run_history_on_startup()
    _reconcile_source_config_on_startup()
    await runner.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await runner.stop()


def _prune_run_history_on_startup() -> None:
    """按保留期清理运行历史并回收数据库空间（A1）。"""
    raw = config_store.load_raw_config()
    try:
        retention_days = int(str(raw.get("PANEL_HISTORY_RETENTION_DAYS", "30")).strip() or "30")
    except ValueError:
        retention_days = 30
    if retention_days <= 0:
        return
    try:
        deleted = history_store.prune_old_records(retention_days)
        if deleted > 0:
            history_store.vacuum()
            logger.info("已清理 %s 天前的运行历史 %s 条并回收数据库空间。", retention_days, deleted)
    except Exception:
        logger.exception("清理运行历史时出错，已跳过。")


def _reconcile_source_config_on_startup() -> None:
    """以 CHANNEL_SOURCES_JSON 为准，校验并重新派生 CHANNEL_IDS/CHANNEL_IDENTIFIERS（B4）。"""
    raw = config_store.load_raw_config()
    source_items = parse_channel_sources(raw.get("CHANNEL_SOURCES_JSON", "[]"))
    if not source_items:
        return

    derived_ids = sorted(
        {item["cid"] for item in source_items if isinstance(item.get("cid"), int) and item.get("enabled")}
    )
    derived_ids_text = ",".join(str(cid) for cid in derived_ids)
    derived_identifiers_text = ",".join(item["source"] for item in source_items)

    current_ids_text = str(raw.get("CHANNEL_IDS", "")).strip()
    current_identifiers_text = str(raw.get("CHANNEL_IDENTIFIERS", "")).strip()

    updates: Dict[str, str] = {}
    if current_ids_text != derived_ids_text:
        updates["CHANNEL_IDS"] = derived_ids_text
    if current_identifiers_text != derived_identifiers_text:
        updates["CHANNEL_IDENTIFIERS"] = derived_identifiers_text

    if updates:
        config_store.save_raw_config(updates)
        logger.info(
            "启动一致性校验：已按来源列表重新派生 %s（启用源 %s 个）。",
            "、".join(updates.keys()),
            len(derived_ids),
        )


def redirect_with_message(path: str, message: str, level: str = "info") -> RedirectResponse:
    query = urlencode({"msg": message, "level": level})
    return RedirectResponse(url=f"{path}?{query}", status_code=303)


def common_context(request: Request, title: str) -> Dict[str, Any]:
    return {
        "request": request,
        "title": title,
        "msg": request.query_params.get("msg", ""),
        "level": request.query_params.get("level", "info"),
        "auth_user": request.session.get("username", ""),
    }


def read_panel_log_tail(log_file: Path, line_limit: int = 300) -> str:
    safe_limit = max(20, min(int(line_limit), 2000))
    if not log_file.exists():
        return "日志文件尚未生成。"

    try:
        content = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"读取日志失败: {exc}"

    lines = content.splitlines()
    if not lines:
        return "暂无日志输出。"

    return "\n".join(lines[-safe_limit:])


def clear_panel_log(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("", encoding="utf-8")


def ensure_channel_checkpoints(
    channel_ids: list[int],
    default_last_ids: Dict[int, int] | None = None,
) -> int:
    """为新频道自动补齐断点记录。

    默认从该频道“当前最新消息”起（default_last_ids 提供），使新增来源只转发加入后的新内容；
    未提供时回退为 0。已存在的断点不覆盖，用户仍可在界面手动改小以补历史。
    """
    default_last_ids = default_last_ids or {}
    created_count = 0
    for channel_id in sorted(set(channel_ids)):
        if checkpoint_store.get_record(channel_id) is None:
            checkpoint_store.set_last_id(channel_id, int(default_last_ids.get(channel_id, 0)))
            created_count += 1
    return created_count


def collect_session_view_data() -> Dict[str, Any]:
    session_exists = config_store.session_file.exists()
    session_info: Dict[str, Any] = {}
    if session_exists:
        stat = config_store.session_file.stat()
        session_info = {
            "size_bytes": stat.st_size,
            "updated_at": timestamp_to_shanghai_iso(stat.st_mtime),
        }

    return {
        "session_exists": session_exists,
        "session_info": session_info,
        "session_path": str(config_store.session_file),
    }


def collect_form_payload(form, current: Dict[str, str], keys: list[str], bool_keys: set[str]) -> Dict[str, str]:
    payload: Dict[str, str] = {}
    for key in keys:
        if key in bool_keys:
            payload[key] = "true" if form.get(key) else "false"
        else:
            if key in form:
                payload[key] = str(form.get(key, "")).strip()
            else:
                payload[key] = current.get(key, "")
    return payload


def normalize_source_token(token: Any) -> str:
    raw = str(token or "").strip()
    if not raw:
        return ""

    if "#" in raw:
        raw = raw.split("#", 1)[0].strip()

    raw = raw.strip().strip(",")
    raw = raw.strip("[]")
    raw = raw.strip().strip("'\"")
    if raw.startswith("-"):
        raw = raw[1:].strip()

    return raw.strip()


def build_tme_link(channel_text: str) -> tuple[str, str]:
    raw = str(channel_text or "").strip()
    if not raw:
        return "", ""

    if raw.startswith("https://t.me/") or raw.startswith("http://t.me/"):
        url = raw.replace("http://", "https://", 1)
        display = url.replace("https://", "", 1)
        return display, url

    token = raw.lstrip("@")
    if not token:
        return "", ""

    url = f"https://t.me/{token}"
    display = f"t.me/{token}"
    return display, url


def build_rss_url(request: Request, token: str) -> str:
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/rss/{token}.xml"


def build_message_link(destination_channel: str, message_id: int) -> str:
    raw = str(destination_channel or "").strip()
    if not raw:
        return ""

    if raw.startswith("https://t.me/") or raw.startswith("http://t.me/"):
        base = raw.replace("http://", "https://", 1).rstrip("/")
        return f"{base}/{message_id}"

    token = raw.lstrip("@").strip()
    if not token:
        return ""
    return f"https://t.me/{token}/{message_id}"


def xml_escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def safe_rss_limit(raw_value: str) -> int:
    try:
        parsed = int(str(raw_value or "").strip())
    except ValueError:
        return 500
    return max(50, min(parsed, 2000))


def message_text_for_feed(message) -> str:
    return (
        getattr(message, "raw_text", None)
        or getattr(message, "message", None)
        or getattr(message, "text", None)
        or getattr(message, "caption", None)
        or ""
    )


def rss_title_from_text(text: str, fallback: str) -> str:
    for line in str(text or "").splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return fallback


def rss_pub_date(message) -> str:
    date_value = getattr(message, "date", None)
    if not date_value:
        return format_datetime(datetime.now(timezone.utc))
    if date_value.tzinfo is None:
        date_value = date_value.replace(tzinfo=timezone.utc)
    return format_datetime(date_value)


def rss_linkify_plain_text(raw: str) -> str:
    pieces: list[str] = []
    position = 0
    for match in RSS_HTTP_URL_RE.finditer(str(raw or "")):
        url = match.group(0)
        while url and url[-1] in RSS_URL_TRAILING_CHARS:
            url = url[:-1]
        if not url:
            continue

        link_end = match.start() + len(url)
        pieces.append(html.escape(raw[position : match.start()], quote=False))
        escaped_href = html.escape(url, quote=True)
        pieces.append(f'<a href="{escaped_href}">{html.escape(url, quote=False)}</a>')
        pieces.append(html.escape(raw[link_end : match.end()], quote=False))
        position = match.end()

    pieces.append(html.escape(raw[position:], quote=False))
    return "".join(pieces)


def rss_utf16_boundaries(text: str) -> dict[int, int]:
    boundaries = {0: 0}
    units = 0
    for index, char in enumerate(str(text or "")):
        units += len(char.encode("utf-16-le")) // 2
        boundaries[units] = index + 1
    return boundaries


def rss_entity_text_map(message) -> dict[int, str]:
    get_entities_text = getattr(message, "get_entities_text", None)
    if not callable(get_entities_text):
        return {}

    results: dict[int, str] = {}
    for entity_type in (MessageEntityTextUrl, MessageEntityUrl):
        try:
            pairs = get_entities_text(entity_type)
        except Exception:
            continue
        try:
            iterator = iter(pairs)
        except TypeError:
            continue
        for pair in iterator:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            entity, entity_text = pair
            results[id(entity)] = str(entity_text or "")
    return results


def rss_entity_span(content: str, entity, expected_text: str, utf16_boundaries: dict[int, int]) -> tuple[int, int] | None:
    offset = int(getattr(entity, "offset", 0) or 0)
    length = int(getattr(entity, "length", 0) or 0)
    if length <= 0 or offset < 0:
        return None

    raw_span = None
    raw_end = offset + length
    if raw_end <= len(content):
        raw_span = (offset, raw_end)

    utf16_span = None
    utf16_start = utf16_boundaries.get(offset)
    utf16_end = utf16_boundaries.get(raw_end)
    if utf16_start is not None and utf16_end is not None and utf16_start <= utf16_end:
        utf16_span = (utf16_start, utf16_end)

    if expected_text:
        if utf16_span and content[utf16_span[0] : utf16_span[1]] == expected_text:
            return utf16_span
        if raw_span and content[raw_span[0] : raw_span[1]] == expected_text:
            return raw_span

    if utf16_span and raw_span and utf16_span != raw_span:
        if any(ord(char) > 0xFFFF for char in content[:offset]):
            return utf16_span

    return raw_span or utf16_span


def rss_link_entities_from_message(message, text: str) -> list[tuple[int, int, str]]:
    content = str(text or "")
    if not content:
        return []

    candidates: list[tuple[int, int, str]] = []
    utf16_boundaries = rss_utf16_boundaries(content)
    expected_by_entity = rss_entity_text_map(message)
    for entity in getattr(message, "entities", None) or []:
        if isinstance(entity, MessageEntityTextUrl):
            href = str(getattr(entity, "url", "") or "").strip()
        elif isinstance(entity, MessageEntityUrl):
            href = ""
        else:
            continue

        expected_text = expected_by_entity.get(id(entity), "")
        span = rss_entity_span(content, entity, expected_text, utf16_boundaries)
        if not span:
            continue

        start, end = span
        if isinstance(entity, MessageEntityUrl):
            href = content[start:end].strip()
        if not href:
            continue
        candidates.append((start, end, href))

    results: list[tuple[int, int, str]] = []
    cursor = 0
    for start, end, href in sorted(candidates, key=lambda item: (item[0], item[1])):
        if start < cursor:
            continue
        results.append((start, end, href))
        cursor = end
    return results


def rss_description_cdata(text: str, image_url: str = "", link_entities: list[tuple[int, int, str]] | None = None) -> str:
    raw = str(text or "（媒体消息）")
    pieces: list[str] = []

    if image_url:
        escaped_image_url = html.escape(image_url, quote=True)
        pieces.append(
            (
                f'<p><img src="{escaped_image_url}" alt="" width="{RSS_IMAGE_DISPLAY_WIDTH}" '
                f'style="width:100%;max-width:{RSS_IMAGE_DISPLAY_WIDTH}px;'
                f'height:auto;max-height:{RSS_IMAGE_DISPLAY_MAX_HEIGHT}px;'
                'object-fit:contain;display:block;margin:0 0 12px 0;border-radius:8px;" /></p>'
            )
        )

    position = 0
    for start, end, href in link_entities or []:
        if start < position or end > len(raw):
            continue
        pieces.append(rss_linkify_plain_text(raw[position:start]))
        anchor_text = raw[start:end]
        escaped_href = html.escape(href, quote=True)
        pieces.append(f'<a href="{escaped_href}">{html.escape(anchor_text, quote=False)}</a>')
        position = end

    pieces.append(rss_linkify_plain_text(raw[position:]))
    html_body = "".join(pieces).replace("\n", "<br />")
    return f"<![CDATA[{html_body.replace(']]>', ']]]]><![CDATA[>')}]]>"


class RssRefreshUnavailable(Exception):
    pass


def rss_cache_file() -> Path:
    return config_store.state_dir / "rss_feed.xml"


def rss_media_dir() -> Path:
    return config_store.state_dir / "rss_media"


def rss_media_type_for_path(path: Path) -> str:
    return RSS_IMAGE_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def build_rss_media_url(request: Request, token: str, filename: str) -> str:
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/rss-media/{token}/{filename}"


def rss_media_payload(path: Path, request: Request, token: str) -> Dict[str, Any]:
    return {
        "filename": path.name,
        "url": build_rss_media_url(request, token, path.name),
        "length": path.stat().st_size,
        "mime_type": rss_media_type_for_path(path),
    }


def rss_message_image_metadata(message) -> tuple[str, str] | None:
    if getattr(message, "photo", None):
        return ".jpg", "image/jpeg"

    file_info = getattr(message, "file", None)
    mime_type = str(getattr(file_info, "mime_type", "") or "").lower()
    if mime_type.startswith("image/"):
        ext = str(getattr(file_info, "ext", "") or "").lower()
        if ext == ".jpe":
            ext = ".jpg"
        if ext not in RSS_IMAGE_MEDIA_TYPES:
            ext = RSS_IMAGE_EXT_BY_MIME.get(mime_type, ".jpg")
        return ext, RSS_IMAGE_MEDIA_TYPES.get(ext, mime_type)

    media = getattr(message, "media", None)
    if getattr(media, "photo", None):
        return ".jpg", "image/jpeg"

    webpage = getattr(media, "webpage", None)
    if getattr(webpage, "photo", None):
        return ".jpg", "image/jpeg"

    return None


def rss_media_prefix(destination_channel: str, message_id: int) -> str:
    channel_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", str(destination_channel or "channel")).strip("_")
    channel_slug = channel_slug[:80] or "channel"
    return f"{channel_slug}_{message_id}_{RSS_IMAGE_CACHE_VERSION}"


def standardize_rss_image_payload(payload: bytes, fallback_ext: str, fallback_mime_type: str) -> tuple[bytes, str, str]:
    try:
        with Image.open(BytesIO(payload)) as image:
            image = ImageOps.exif_transpose(image)
            resampling_filter = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            image.thumbnail((RSS_IMAGE_DISPLAY_WIDTH, RSS_IMAGE_DISPLAY_MAX_HEIGHT), resampling_filter)

            if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                alpha = image.convert("RGBA").getchannel("A")
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image.convert("RGBA"), mask=alpha)
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")

            output = BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=RSS_IMAGE_JPEG_QUALITY,
                optimize=True,
                progressive=True,
            )
            return output.getvalue(), ".jpg", "image/jpeg"
    except (OSError, ValueError, UnidentifiedImageError):
        return payload, fallback_ext, fallback_mime_type


async def cache_rss_message_image(
    client: TelegramClient,
    message,
    request: Request,
    token: str,
    destination_channel: str,
) -> Dict[str, Any] | None:
    metadata = rss_message_image_metadata(message)
    if not metadata:
        return None

    message_id = int(getattr(message, "id", 0) or 0)
    if message_id <= 0:
        return None

    ext, mime_type = metadata
    media_dir = rss_media_dir()
    media_dir.mkdir(parents=True, exist_ok=True)
    prefix = rss_media_prefix(destination_channel, message_id)

    for existing in media_dir.glob(f"{prefix}.*"):
        if existing.is_file() and existing.suffix.lower() in RSS_IMAGE_MEDIA_TYPES:
            return rss_media_payload(existing, request, token)

    temporary_path = media_dir / f"{prefix}.{secrets.token_hex(8)}.tmp"
    try:
        payload = await client.download_media(message, file=bytes)
        if not payload:
            return None
        payload, ext, mime_type = standardize_rss_image_payload(payload, ext, mime_type)
        target_path = media_dir / f"{prefix}{ext}"
        temporary_path.write_bytes(payload)
        temporary_path.replace(target_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    return {
        "filename": target_path.name,
        "url": build_rss_media_url(request, token, target_path.name),
        "length": target_path.stat().st_size,
        "mime_type": mime_type,
    }


def cleanup_stale_rss_media(active_filenames: set[str]) -> None:
    media_dir = rss_media_dir()
    if not media_dir.exists():
        return
    for item in media_dir.iterdir():
        if not item.is_file():
            continue
        if item.name in active_filenames:
            continue
        if item.suffix.lower() not in RSS_IMAGE_MEDIA_TYPES and item.suffix.lower() != ".tmp":
            continue
        try:
            item.unlink(missing_ok=True)
        except OSError:
            pass


def read_rss_cache() -> str | None:
    path = rss_cache_file()
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def write_rss_cache(content: str) -> None:
    path = rss_cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    temporary_path.write_text(content, encoding="utf-8")
    temporary_path.replace(path)


def create_rss_session_copy() -> Path:
    rss_session_dir = config_store.state_dir / "rss_session"
    rss_session_dir.mkdir(parents=True, exist_ok=True)
    session_base = rss_session_dir / f"t2rss_rss_{secrets.token_hex(8)}"

    source_candidates = [
        (config_store.session_file, Path(f"{session_base}.session")),
        (Path(f"{config_store.session_base_path}.session-journal"), Path(f"{session_base}.session-journal")),
        (Path(f"{config_store.session_base_path}.session-shm"), Path(f"{session_base}.session-shm")),
        (Path(f"{config_store.session_base_path}.session-wal"), Path(f"{session_base}.session-wal")),
    ]
    for source, destination in source_candidates:
        if source.exists():
            shutil.copy2(source, destination)

    return session_base


def cleanup_rss_session_copy(session_base: Path) -> None:
    for candidate in [
        Path(f"{session_base}.session"),
        Path(f"{session_base}.session-journal"),
        Path(f"{session_base}.session-shm"),
        Path(f"{session_base}.session-wal"),
    ]:
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


def build_rss_xml(
    request: Request,
    token: str,
    raw_config: Dict[str, str],
    items_xml: list[str],
    note: str = "T2RSS 目标频道消息订阅",
) -> str:
    destination_channel = str(raw_config.get("DESTINATION_CHANNEL", "")).strip()
    destination_display, destination_url = build_tme_link(destination_channel)
    feed_title = f"T2RSS - {destination_display or destination_channel or 'Feed'}"
    feed_link = destination_url or str(request.base_url).rstrip("/")
    self_url = build_rss_url(request, token)

    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:content="http://purl.org/rss/1.0/modules/content/">',
            "  <channel>",
            f"    <title>{xml_escape(feed_title)}</title>",
            f"    <link>{xml_escape(feed_link)}</link>",
            f"    <atom:link href=\"{xml_escape(self_url)}\" rel=\"self\" type=\"application/rss+xml\" />",
            f"    <description>{xml_escape(note)}</description>",
            "    <language>zh-CN</language>",
            f"    <lastBuildDate>{xml_escape(format_datetime(datetime.now(timezone.utc)))}</lastBuildDate>",
            *items_xml,
            "  </channel>",
            "</rss>",
        ]
    )


async def build_live_rss_xml(request: Request, token: str, raw_config: Dict[str, str]) -> str:
    api_id = str(raw_config.get("API_ID", "")).strip()
    api_hash = str(raw_config.get("API_HASH", "")).strip()
    destination_channel = str(raw_config.get("DESTINATION_CHANNEL", "")).strip()
    if not api_id or not api_hash or not destination_channel:
        raise RssRefreshUnavailable("RSS 尚未配置 API 或目标频道")
    if not config_store.session_file.exists():
        raise RssRefreshUnavailable("Telegram 会话缺失，暂时无法刷新 RSS")

    try:
        api_id_int = int(api_id)
    except ValueError as exc:
        raise RssRefreshUnavailable("API_ID 无效，暂时无法刷新 RSS") from exc
    item_limit = safe_rss_limit(raw_config.get("PANEL_RSS_ITEM_LIMIT", "500"))
    _, destination_url = build_tme_link(destination_channel)
    feed_link = destination_url or str(request.base_url).rstrip("/")
    items_xml: list[str] = []
    active_image_filenames: set[str] = set()
    image_download_failed = False

    session_copy_base = create_rss_session_copy()
    try:
        async with TelegramClient(str(session_copy_base), api_id_int, api_hash) as client:
            if not await client.is_user_authorized():
                raise RssRefreshUnavailable("Telegram 会话未授权，暂时无法刷新 RSS")

            async for message in client.iter_messages(destination_channel, limit=item_limit):
                message_id = int(getattr(message, "id", 0) or 0)
                text = message_text_for_feed(message)
                title = rss_title_from_text(text, f"Telegram 消息 {message_id}")
                link = build_message_link(destination_channel, message_id) or feed_link
                image_info = None
                try:
                    image_info = await cache_rss_message_image(client, message, request, token, destination_channel)
                except Exception as exc:
                    image_download_failed = True
                    logger.warning("RSS 图片缓存失败，消息 %s：%s", message_id, exc)

                if image_info:
                    active_image_filenames.add(str(image_info["filename"]))

                link_entities = rss_link_entities_from_message(message, text)
                description = rss_description_cdata(
                    text,
                    str(image_info["url"]) if image_info else "",
                    link_entities,
                )
                guid = link or f"t2rss:{destination_channel}:{message_id}"
                pub_date = rss_pub_date(message)

                item_lines = [
                    "    <item>",
                    f"      <title>{xml_escape(title)}</title>",
                    f"      <link>{xml_escape(link)}</link>",
                    f"      <guid isPermaLink=\"false\">{xml_escape(guid)}</guid>",
                    f"      <pubDate>{xml_escape(pub_date)}</pubDate>",
                ]
                if image_info:
                    item_lines.append(
                        (
                            f"      <enclosure url=\"{xml_escape(str(image_info['url']))}\" "
                            f"length=\"{int(image_info['length'])}\" "
                            f"type=\"{xml_escape(str(image_info['mime_type']))}\" />"
                        )
                    )
                item_lines.extend(
                    [
                        f"      <description>{description}</description>",
                        f"      <content:encoded>{description}</content:encoded>",
                        "    </item>",
                    ]
                )

                items_xml.append("\n".join(item_lines))
    finally:
        cleanup_rss_session_copy(session_copy_base)

    if not image_download_failed:
        cleanup_stale_rss_media(active_image_filenames)

    return build_rss_xml(request, token, raw_config, items_xml)


async def refresh_rss_cache_in_background(request: Request, token: str, raw_config: Dict[str, str]) -> None:
    try:
        rss_xml = await asyncio.wait_for(
            build_live_rss_xml(request, token, raw_config),
            timeout=RSS_BACKGROUND_REFRESH_TIMEOUT_SECONDS,
        )
        write_rss_cache(rss_xml)
    except RssRefreshUnavailable as exc:
        logger.info("RSS 后台刷新不可用，已保留旧缓存：%s", exc)
    except asyncio.TimeoutError:
        logger.warning("RSS 后台刷新超过 %s 秒，已保留旧缓存。", RSS_BACKGROUND_REFRESH_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("RSS 后台刷新失败，已保留旧缓存。")


def schedule_rss_cache_refresh(request: Request, token: str, raw_config: Dict[str, str]) -> None:
    global rss_refresh_task
    if rss_refresh_task and not rss_refresh_task.done():
        return
    rss_refresh_task = asyncio.create_task(refresh_rss_cache_in_background(request, token, dict(raw_config)))


def parse_sources_input(text: str) -> list[str]:
    raw_text = str(text or "")
    normalized = raw_text.replace("，", ",")
    tokens = normalized.replace("\n", ",").split(",")

    items: list[str] = []
    seen = set()
    for token in tokens:
        cleaned = normalize_source_token(token)
        if not cleaned:
            continue
        if cleaned in seen:
            continue
        items.append(cleaned)
        seen.add(cleaned)
    return items


def load_source_items_from_config(raw_config: Dict[str, str]) -> list[Dict[str, Any]]:
    items = parse_channel_sources(raw_config.get("CHANNEL_SOURCES_JSON", "[]"))
    if items:
        return items

    fallback_identifiers = parse_csv(raw_config.get("CHANNEL_IDENTIFIERS", ""))
    fallback_ids: list[int] = []
    try:
        fallback_ids = parse_int_csv(raw_config.get("CHANNEL_IDS", ""), "CHANNEL_IDS")
    except ValueError:
        fallback_ids = []

    results: list[Dict[str, Any]] = []
    if fallback_identifiers:
        for source in fallback_identifiers:
            results.append(
                {
                    "source": source,
                    "cid": None,
                    "enabled": True,
                    "status": "pending",
                    "error": "",
                }
            )

    if fallback_ids and not results:
        for cid in fallback_ids:
            results.append(
                {
                    "source": str(cid),
                    "cid": cid,
                    "enabled": True,
                    "status": "ok",
                    "error": "",
                }
            )

    return results



def build_forward_settings_context(
    request: Request,
    raw_config: Dict[str, str],
    source_items: list[Dict[str, Any]] | None = None,
    pending_source_items: list[Dict[str, Any]] | None = None,
    pending_sources_input: str = "",
    override_destination: str | None = None,
) -> Dict[str, Any]:
    items = source_items if source_items is not None else load_source_items_from_config(raw_config)
    config_view = dict(raw_config)
    if override_destination is not None:
        config_view["DESTINATION_CHANNEL"] = override_destination

    return {
        **common_context(request, "转发设置"),
        "config": config_view,
        "source_items": items,
        "pending_source_items": pending_source_items or [],
        "pending_sources_input": pending_sources_input,
        "last_ids": checkpoint_store.list_last_ids(),
    }


def build_checkpoint_rows(raw_config: Dict[str, str]) -> list[Dict[str, Any]]:
    try:
        fallback_channel_ids = sorted(set(parse_int_csv(raw_config.get("CHANNEL_IDS", ""), "CHANNEL_IDS")))
    except ValueError:
        fallback_channel_ids = []

    source_items = parse_channel_sources(raw_config.get("CHANNEL_SOURCES_JSON", "[]"))
    resolved_cids_all = {int(item["cid"]) for item in source_items if isinstance(item.get("cid"), int)}
    resolved_cids_enabled = {
        int(item["cid"])
        for item in source_items
        if isinstance(item.get("cid"), int) and bool(item.get("enabled", True))
    }

    if not source_items:
        resolved_cids_enabled = set(fallback_channel_ids)
        resolved_cids_all = set(fallback_channel_ids)

    last_ids = checkpoint_store.list_last_ids()

    # D1: 附加每源近 7/30 天抓取量统计
    try:
        totals = history_store.per_channel_fetched_totals({"d7": 7, "d30": 30})
    except Exception:
        totals = {"d7": {}, "d30": {}}

    for row in last_ids:
        cid = int(row.get("channel_id", 0))
        if cid in resolved_cids_enabled:
            row["status_text"] = "开启"
        elif cid in resolved_cids_all:
            row["status_text"] = "停用"
        else:
            row["status_text"] = "停用"
        row["fetched_7d"] = int(totals.get("d7", {}).get(cid, 0))
        row["fetched_30d"] = int(totals.get("d30", {}).get(cid, 0))

    return last_ids


def extract_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def safe_next_path(next_path: str) -> str:
    normalized = (next_path or "").strip()
    if not normalized.startswith("/"):
        return "/"
    if normalized.startswith("//"):
        return "/"
    return normalized


def auth_redirect_if_needed(request: Request) -> RedirectResponse | None:
    if request.session.get("authenticated") is True:
        return None

    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    query = urlencode({"next": next_path})
    return RedirectResponse(url=f"/login?{query}", status_code=303)


@app.get("/login")
async def login_page(request: Request):
    if request.session.get("authenticated") is True:
        return RedirectResponse(url="/", status_code=303)

    context = {
        "request": request,
        "title": "管理员登录",
        "msg": request.query_params.get("msg", ""),
        "level": request.query_params.get("level", "info"),
        "next": safe_next_path(request.query_params.get("next", "/")),
    }
    return templates.TemplateResponse("login.html", context)


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    next_path = safe_next_path(str(form.get("next", "/")))

    raw_config = config_store.load_raw_config()
    admin_username = str(raw_config.get("PANEL_ADMIN_USERNAME", "admin")).strip() or "admin"
    admin_password_hash = str(raw_config.get("PANEL_ADMIN_PASSWORD_HASH", "")).strip()
    admin_password_plain = str(raw_config.get("PANEL_ADMIN_PASSWORD", "")).strip()

    if not admin_password_hash and not admin_password_plain:
        query = urlencode(
            {
                "msg": "管理员密码未配置，请先在服务器配置后重试。",
                "level": "error",
                "next": next_path,
            }
        )
        return RedirectResponse(url=f"/login?{query}", status_code=303)

    ip = extract_client_ip(request)
    scope_username = username or admin_username
    locked_seconds = login_guard_store.get_lock_seconds(ip, scope_username)
    if locked_seconds > 0:
        query = urlencode(
            {
                "msg": f"登录已被临时锁定，请在 {locked_seconds} 秒后重试。",
                "level": "error",
                "next": next_path,
            }
        )
        return RedirectResponse(url=f"/login?{query}", status_code=303)

    is_valid_user = hmac_compare(username, admin_username)
    is_valid_password = verify_password(password, admin_password_hash, admin_password_plain)
    if is_valid_user and is_valid_password:
        login_guard_store.clear_failures(ip, admin_username)
        request.session.clear()
        request.session["authenticated"] = True
        request.session["username"] = admin_username
        return RedirectResponse(url=next_path, status_code=303)

    locked_after_failure = login_guard_store.record_failure(ip, scope_username, raw_config)
    await asyncio.sleep(0.8)

    if locked_after_failure > 0:
        msg = f"用户名或密码错误，已触发防爆破锁定 {locked_after_failure} 秒。"
    else:
        msg = "用户名或密码错误。"

    query = urlencode({"msg": msg, "level": "error", "next": next_path})
    return RedirectResponse(url=f"/login?{query}", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    query = urlencode({"msg": "已安全退出登录。", "level": "success"})
    return RedirectResponse(url=f"/login?{query}", status_code=303)


def hmac_compare(left: str, right: str) -> bool:
    return secrets.compare_digest(left or "", right or "")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/rss-media/{token}/{filename}")
async def rss_media(token: str, filename: str):
    raw_config = config_store.load_raw_config()
    expected_token = str(raw_config.get("PANEL_RSS_TOKEN", "")).strip()
    if not expected_token or not hmac.compare_digest(str(token or ""), expected_token):
        raise HTTPException(status_code=404, detail="RSS media not found")
    if not parse_bool(raw_config.get("PANEL_RSS_ENABLED", "true"), True):
        raise HTTPException(status_code=404, detail="RSS media not found")
    if not RSS_MEDIA_FILENAME_RE.fullmatch(str(filename or "")):
        raise HTTPException(status_code=404, detail="RSS media not found")

    media_path = rss_media_dir() / filename
    if not media_path.exists() or not media_path.is_file():
        raise HTTPException(status_code=404, detail="RSS media not found")
    return FileResponse(path=str(media_path), media_type=rss_media_type_for_path(media_path))


@app.get("/rss/{token}.xml")
async def rss_feed(token: str, request: Request):
    raw_config = config_store.load_raw_config()
    expected_token = str(raw_config.get("PANEL_RSS_TOKEN", "")).strip()
    if not expected_token or not hmac.compare_digest(str(token or ""), expected_token):
        raise HTTPException(status_code=404, detail="RSS feed not found")
    if not parse_bool(raw_config.get("PANEL_RSS_ENABLED", "true"), True):
        raise HTTPException(status_code=404, detail="RSS feed not found")

    cached_xml = read_rss_cache()
    if cached_xml:
        schedule_rss_cache_refresh(request, expected_token, raw_config)
        return Response(content=cached_xml, media_type="application/rss+xml; charset=utf-8")

    if runner.is_running:
        schedule_rss_cache_refresh(request, expected_token, raw_config)
        rss_xml = build_rss_xml(request, expected_token, raw_config, [], note="转发任务运行中，RSS 稍后会自动刷新")
        return Response(content=rss_xml, media_type="application/rss+xml; charset=utf-8")

    try:
        rss_xml = await asyncio.wait_for(
            build_live_rss_xml(request, expected_token, raw_config),
            timeout=RSS_REFRESH_TIMEOUT_SECONDS,
        )
        write_rss_cache(rss_xml)
    except RssRefreshUnavailable as exc:
        cached_xml = cached_xml or read_rss_cache()
        if cached_xml:
            logger.info("RSS 实时刷新不可用，已返回缓存内容：%s", exc)
            rss_xml = cached_xml
        else:
            rss_xml = build_rss_xml(request, expected_token, raw_config, [], note=str(exc))
    except asyncio.TimeoutError:
        cached_xml = cached_xml or read_rss_cache()
        if cached_xml:
            logger.warning("RSS 实时刷新超过 %s 秒，已返回缓存内容。", RSS_REFRESH_TIMEOUT_SECONDS)
            rss_xml = cached_xml
        else:
            schedule_rss_cache_refresh(request, expected_token, raw_config)
            rss_xml = build_rss_xml(request, expected_token, raw_config, [], note="RSS 实时刷新超时，稍后会自动恢复")
    except Exception:
        cached_xml = cached_xml or read_rss_cache()
        logger.exception("RSS 实时刷新失败，已尝试返回缓存内容。")
        if cached_xml:
            rss_xml = cached_xml
        else:
            rss_xml = build_rss_xml(request, expected_token, raw_config, [], note="RSS 暂时无法刷新，稍后会自动恢复")

    return Response(content=rss_xml, media_type="application/rss+xml; charset=utf-8")


@app.get("/")
async def dashboard(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    raw_config = config_store.load_raw_config()
    panel_settings = config_store.build_panel_settings()
    destination_display, destination_url = build_tme_link(raw_config.get("DESTINATION_CHANNEL", ""))
    rss_token = str(raw_config.get("PANEL_RSS_TOKEN", "")).strip()
    rss_enabled = parse_bool(raw_config.get("PANEL_RSS_ENABLED", "true"), True)
    rss_item_limit = safe_rss_limit(raw_config.get("PANEL_RSS_ITEM_LIMIT", "500"))

    keyword_blacklist = parse_csv(raw_config.get("KEYWORD_BLACKLIST", ""))
    text_replacement_terms = parse_csv(raw_config.get("TEXT_REPLACEMENT_TERMS", ""))
    user_id_blacklist = parse_csv(raw_config.get("USER_ID_BLACKLIST", ""))

    try:
        fallback_channel_ids = sorted(set(parse_int_csv(raw_config.get("CHANNEL_IDS", ""), "CHANNEL_IDS")))
    except ValueError:
        fallback_channel_ids = []

    source_items = parse_channel_sources(raw_config.get("CHANNEL_SOURCES_JSON", "[]"))
    resolved_cids_all = {int(item["cid"]) for item in source_items if isinstance(item.get("cid"), int)}
    resolved_cids_enabled = {
        int(item["cid"])
        for item in source_items
        if isinstance(item.get("cid"), int) and bool(item.get("enabled", True))
    }

    if source_items:
        total_source_count = len(source_items)
        enabled_source_count = len([item for item in source_items if bool(item.get("enabled", True))])
        channel_id_count = len(resolved_cids_enabled)
    else:
        total_source_count = len(fallback_channel_ids)
        enabled_source_count = len(fallback_channel_ids)
        channel_id_count = len(fallback_channel_ids)
        resolved_cids_enabled = set(fallback_channel_ids)
        resolved_cids_all = set(fallback_channel_ids)

    last_ids = build_checkpoint_rows(raw_config)

    context = common_context(request, "仪表盘")
    context.update(
        {
            "session_exists": config_store.session_file.exists(),
            "lock_exists": config_store.lock_file.exists(),
            "last_ids": last_ids,
            "runner_status": runner.status_payload(),
            "config_preview": {
                "destination_channel": raw_config.get("DESTINATION_CHANNEL", ""),
                "destination_display": destination_display,
                "destination_url": destination_url,
                "rss_enabled": rss_enabled,
                "rss_item_limit": rss_item_limit,
                "rss_url": build_rss_url(request, rss_token) if rss_enabled and rss_token else "",
                "source_summary": f"{enabled_source_count}/{total_source_count}",
                "keyword_blacklist_text": "，".join(keyword_blacklist),
                "keyword_blacklist_count": len(keyword_blacklist),
                "text_replacement_terms_count": len(text_replacement_terms),
                "user_id_blacklist_text": "，".join(user_id_blacklist),
                "user_id_blacklist_count": len(user_id_blacklist),
                "deduplication_enabled": raw_config.get("DEDUPLICATION_ENABLED", "false"),
                "deduplication_115_enabled": raw_config.get("DEDUPLICATION_115_ENABLED", "true"),
                "deduplication_cache_size": raw_config.get("DEDUPLICATION_CACHE_SIZE", "200"),
                "auto_run_enabled": panel_settings.auto_run_enabled,
                "auto_run_interval_minutes": panel_settings.auto_run_interval_minutes,
                "total_timeout_seconds": panel_settings.total_timeout_seconds,
                "test_mode_enabled": panel_settings.test_mode_enabled,
            },
        }
    )
    return templates.TemplateResponse("dashboard.html", context)


@app.post("/run")
async def run_now(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    started = await runner.trigger(trigger="manual")
    if not started:
        return redirect_with_message("/", "当前已有转发任务在运行。", "warn")
    return redirect_with_message("/", "转发任务已在后台启动。", "success")


@app.post("/run/stop")
async def force_stop_run(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    stopped = await runner.abort_current_run()
    if not stopped:
        return redirect_with_message("/", "当前没有可中止的运行任务。", "warn")
    return redirect_with_message("/", "已发送强制中止指令。", "success")


@app.get("/setup")
async def setup_page(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    context = common_context(request, "初始化接入")
    context.update(
        {
            "config": config_store.load_raw_config(),
            **collect_session_view_data(),
        }
    )
    return templates.TemplateResponse("setup.html", context)


@app.post("/setup/save")
async def setup_save(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    current = config_store.load_raw_config()

    keys = [
        "PANEL_LOGIN_MAX_FAILURES",
        "PANEL_LOGIN_WINDOW_SECONDS",
        "PANEL_LOGIN_LOCK_SECONDS",
        "API_ID",
        "API_HASH",
        "PHONE",
        "PASSWORD",
        "PANEL_RSS_ENABLED",
        "PANEL_RSS_ITEM_LIMIT",
    ]
    payload = collect_form_payload(form, current, keys, bool_keys={"PANEL_RSS_ENABLED"})

    # 敏感字段保存后不回显：页面提交为空时默认保持原值不变。
    sensitive_keys = {"API_ID", "API_HASH", "PHONE", "PASSWORD"}
    for key in sensitive_keys:
        if str(payload.get(key, "")).strip() == "":
            payload[key] = current.get(key, "")

    payload["PANEL_SESSION_SECRET"] = current.get("PANEL_SESSION_SECRET", "")
    payload["PANEL_ADMIN_PASSWORD"] = current.get("PANEL_ADMIN_PASSWORD", "")
    payload["PANEL_ADMIN_PASSWORD_HASH"] = current.get("PANEL_ADMIN_PASSWORD_HASH", "")
    payload["PANEL_ADMIN_USERNAME"] = current.get("PANEL_ADMIN_USERNAME", "admin")
    payload["PANEL_RSS_TOKEN"] = current.get("PANEL_RSS_TOKEN", "")
    payload["PANEL_RSS_ITEM_LIMIT"] = str(safe_rss_limit(payload.get("PANEL_RSS_ITEM_LIMIT", "500")))

    if not payload.get("PANEL_ADMIN_USERNAME", "").strip():
        payload["PANEL_ADMIN_USERNAME"] = "admin"

    config_store.save_raw_config(payload)
    return redirect_with_message("/setup", "初始化接入配置已保存。", "success")


@app.post("/setup/admin-credentials-save")
async def setup_admin_credentials_save(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    current = config_store.load_raw_config()

    old_password = str(form.get("PANEL_ADMIN_OLD_PASSWORD", "")).strip()
    new_username_input = str(form.get("PANEL_ADMIN_USERNAME", "")).strip()
    new_password = str(form.get("PANEL_ADMIN_NEW_PASSWORD", "")).strip()
    confirm_password = str(form.get("PANEL_ADMIN_NEW_PASSWORD_CONFIRM", "")).strip()

    current_username = str(current.get("PANEL_ADMIN_USERNAME", "admin")).strip() or "admin"
    current_hash = str(current.get("PANEL_ADMIN_PASSWORD_HASH", "")).strip()
    current_plain = str(current.get("PANEL_ADMIN_PASSWORD", "")).strip()

    if not old_password:
        return redirect_with_message("/setup", "请先输入当前密码，再修改用户名或密码。", "warn")
    if not verify_password(old_password, current_hash, current_plain):
        return redirect_with_message("/setup", "当前密码校验失败，未修改登录信息。", "warn")

    next_username = new_username_input or current_username
    if not next_username:
        return redirect_with_message("/setup", "管理员用户名不能为空。", "warn")
    if any(ch.isspace() for ch in next_username):
        return redirect_with_message("/setup", "管理员用户名不能包含空白字符。", "warn")

    username_changed = next_username != current_username
    password_changed = False

    payload = dict(current)
    payload["PANEL_ADMIN_USERNAME"] = next_username

    if new_password or confirm_password:
        if new_password != confirm_password:
            return redirect_with_message("/setup", "管理员新密码与确认密码不一致。", "warn")
        if len(new_password) < 8:
            return redirect_with_message("/setup", "管理员密码长度至少 8 位。", "warn")
        payload["PANEL_ADMIN_PASSWORD_HASH"] = build_password_hash(new_password)
        payload["PANEL_ADMIN_PASSWORD"] = ""
        password_changed = True

    if not username_changed and not password_changed:
        return redirect_with_message("/setup", "未检测到用户名或密码变更。", "info")

    config_store.save_raw_config(payload)
    if request.session.get("username") == current_username:
        request.session["username"] = next_username

    if username_changed and password_changed:
        return redirect_with_message("/setup", "管理员用户名和密码已更新。", "success")
    if username_changed:
        return redirect_with_message("/setup", "管理员用户名已更新。", "success")
    return redirect_with_message("/setup", "管理员密码已更新。", "success")


@app.get("/forward-settings")
async def forward_settings_page(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    raw_config = config_store.load_raw_config()
    context = build_forward_settings_context(request, raw_config)
    return templates.TemplateResponse("forward_settings.html", context)


@app.post("/forward-settings/save")
async def forward_settings_save(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    current = config_store.load_raw_config()
    current_source_items = load_source_items_from_config(current)

    destination_channel = str(form.get("DESTINATION_CHANNEL", "")).strip()
    if not destination_channel:
        return redirect_with_message("/forward-settings", "请先填写目标频道。", "warn")

    # 以服务器当前配置为基准：本页初始快照中不存在的来源，是其他页面/请求新加入的，必须保留。
    snapshot_sources = {normalize_source_token(value) for value in form.getlist("source_snapshot") if value}
    posted_sources = {normalize_source_token(value) for value in form.getlist("row_source") if value}
    enabled_source_set = {normalize_source_token(value) for value in form.getlist("row_enabled_source") if value}
    source_items: list[Dict[str, Any]] = []
    for item in current_source_items:
        source = normalize_source_token(item.get("source", ""))
        if not source:
            continue
        if source not in snapshot_sources:
            source_items.append(dict(item))
            continue
        if source not in posted_sources:
            # 仅删除“本页初始快照”中被删除按钮移除的行。
            continue
        updated = dict(item)
        updated["enabled"] = source in enabled_source_set and isinstance(updated.get("cid"), int)
        source_items.append(updated)

    enabled_cids = sorted({item["cid"] for item in source_items if isinstance(item.get("cid"), int) and item.get("enabled")})

    all_resolved_cids = sorted({item["cid"] for item in source_items if isinstance(item.get("cid"), int)})

    # B3: 检测重复来源（同一 cid 对应多行），提示用户避免重复转发。
    cid_counts: Dict[int, int] = {}
    for item in source_items:
        cid_val = item.get("cid")
        if isinstance(cid_val, int):
            cid_counts[cid_val] = cid_counts.get(cid_val, 0) + 1
    duplicate_cids = sorted(cid for cid, count in cid_counts.items() if count > 1)
    duplicate_hint = ""
    if duplicate_cids:
        duplicate_hint = "；检测到重复来源频道 ID：" + "、".join(str(cid) for cid in duplicate_cids) + "（建议删除重复行）"

    keys = [
        "KEYWORD_BLACKLIST",
        "TEXT_REPLACEMENT_TERMS",
        "TEXT_REPLACEMENT_REGEX",
        "USER_ID_BLACKLIST",
        "DEDUPLICATION_ENABLED",
        "DEDUPLICATION_115_ENABLED",
        "DEDUPLICATION_CACHE_SIZE",
    ]
    payload = collect_form_payload(form, current, keys, bool_keys={"DEDUPLICATION_ENABLED", "DEDUPLICATION_115_ENABLED"})

    payload["DESTINATION_CHANNEL"] = destination_channel
    payload["CHANNEL_IDS"] = ",".join(str(cid) for cid in enabled_cids)
    payload["CHANNEL_IDENTIFIERS"] = ",".join(item["source"] for item in source_items)
    payload["CHANNEL_SOURCES_JSON"] = json.dumps(source_items, ensure_ascii=False, separators=(",", ":"))

    config_store.save_raw_config(payload)
    created_count = ensure_channel_checkpoints(all_resolved_cids)
    disabled_count = len([item for item in source_items if not item.get("enabled")])

    if not source_items:
        return redirect_with_message("/forward-settings", "已保存：来源频道列表为空。", "success")

    result_level = "warn" if duplicate_cids else "success"
    if created_count > 0:
        return redirect_with_message(
            "/forward-settings",
            (
                f"已保存：启用来源 {len(enabled_cids)} 个，关闭来源 {disabled_count} 个，"
                f"自动新增断点 {created_count} 条。{duplicate_hint}"
            ),
            result_level,
        )
    if len(enabled_cids) == 0:
        return redirect_with_message("/forward-settings", f"已保存：当前没有启用的来源频道。{duplicate_hint}", "warn")
    return redirect_with_message(
        "/forward-settings",
        f"已保存：启用来源 {len(enabled_cids)} 个，关闭来源 {disabled_count} 个。{duplicate_hint}",
        result_level,
    )


@app.post("/settings/resolve")
@app.post("/forward-settings/resolve")
async def resolve_identifiers(request: Request):
    """解析仅用于“新增来源”暂存区，绝不触碰已保存的来源配置或断点。"""
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    raw_input = str(form.get("pending_sources_input", "")).strip()
    if not raw_input:
        raw_input = str(form.get("identifiers_input", "")).strip()
    identifiers = parse_sources_input(raw_input)
    raw_config = config_store.load_raw_config()
    existing_items = load_source_items_from_config(raw_config)
    existing_sources = {normalize_source_token(item.get("source", "")) for item in existing_items}
    existing_cids = {item.get("cid") for item in existing_items if isinstance(item.get("cid"), int)}

    if not identifiers:
        context = build_forward_settings_context(request, raw_config, pending_sources_input=raw_input)
        context["msg"] = "请至少输入一个待新增并解析的来源频道。"
        context["level"] = "warn"
        return templates.TemplateResponse("forward_settings.html", context)

    try:
        resolved_rows = await resolve_identifiers_preview(config_store, identifiers, logger)
        pending_items: list[Dict[str, Any]] = []
        for resolved in resolved_rows:
            source = normalize_source_token(resolved.get("identifier", ""))
            cid_value = resolved.get("channel_id")
            cid: int | None = None
            if bool(resolved.get("ok")) and cid_value not in {None, ""}:
                try:
                    cid = int(str(cid_value).strip())
                except ValueError:
                    cid = None

            duplicate_reason = ""
            if source in existing_sources:
                duplicate_reason = "该来源已在已保存列表中"
            elif cid is not None and cid in existing_cids:
                duplicate_reason = "该 CID 已在已保存列表中"

            pending_items.append(
                {
                    "source": source,
                    "cid": cid,
                    "latest_message_id": int(resolved.get("latest_message_id", 0) or 0),
                    "enabled": cid is not None and not duplicate_reason,
                    "status": "duplicate" if duplicate_reason else ("ok" if cid is not None else "failed"),
                    "error": duplicate_reason or ("" if cid is not None else str(resolved.get("error", "解析失败"))),
                }
            )

        context = build_forward_settings_context(
            request,
            raw_config,
            pending_source_items=pending_items,
            pending_sources_input="\n".join(identifiers),
        )
        context["msg"] = "新增来源已解析，确认勾选后点击“加入已保存来源”。解析阶段不会改动现有来源或断点。"
        context["level"] = "success"
    except Exception as exc:
        context = build_forward_settings_context(request, raw_config, pending_sources_input="\n".join(identifiers))
        context["msg"] = f"新增来源解析失败：{exc}"
        context["level"] = "error"

    return templates.TemplateResponse("forward_settings.html", context)


@app.post("/forward-settings/sources/add")
async def add_resolved_sources(request: Request):
    """将用户确认的暂存来源合并入当前配置；始终以服务器最新配置为基准。"""
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    current = config_store.load_raw_config()
    source_items = load_source_items_from_config(current)
    selected_sources = []
    seen_selected_sources: set[str] = set()
    for value in form.getlist("pending_selected_source"):
        source = normalize_source_token(value)
        if source and source not in seen_selected_sources:
            selected_sources.append(source)
            seen_selected_sources.add(source)

    if not selected_sources:
        return redirect_with_message("/forward-settings", "请至少勾选一个解析成功的新来源。", "warn")

    # 不信任页面隐藏字段的 CID / last_id：加入前重新解析所选来源，避免过期或被篡改的暂存结果写入配置。
    try:
        resolved_rows = await resolve_identifiers_preview(config_store, selected_sources, logger)
    except Exception as exc:
        return redirect_with_message("/forward-settings", f"加入前重新解析来源失败：{exc}", "error")

    existing_sources = {normalize_source_token(item.get("source", "")) for item in source_items}
    existing_cids = {item.get("cid") for item in source_items if isinstance(item.get("cid"), int)}
    latest_id_map: Dict[int, int] = {}
    added_count = 0
    skipped_duplicates = 0
    failed_sources: list[str] = []

    for resolved in resolved_rows:
        source = normalize_source_token(resolved.get("identifier", ""))
        try:
            cid = int(str(resolved.get("channel_id", "")).strip())
        except ValueError:
            failed_sources.append(source or "未知来源")
            continue
        if not bool(resolved.get("ok")) or source in existing_sources or cid in existing_cids:
            skipped_duplicates += 1
            continue

        try:
            latest_id = max(0, int(resolved.get("latest_message_id", 0) or 0))
        except (TypeError, ValueError):
            latest_id = 0
        source_items.append({"source": source, "cid": cid, "enabled": True, "status": "ok", "error": ""})
        existing_sources.add(source)
        existing_cids.add(cid)
        latest_id_map[cid] = latest_id
        added_count += 1

    if not added_count:
        message = "没有可加入的新来源。"
        if skipped_duplicates:
            message += f" 已跳过 {skipped_duplicates} 个与当前配置重复的来源。"
        return redirect_with_message("/forward-settings", message, "warn")

    enabled_cids = sorted({item["cid"] for item in source_items if isinstance(item.get("cid"), int) and item.get("enabled")})
    config_store.save_raw_config(
        {
            "CHANNEL_IDS": ",".join(str(cid) for cid in enabled_cids),
            "CHANNEL_IDENTIFIERS": ",".join(item["source"] for item in source_items),
            "CHANNEL_SOURCES_JSON": json.dumps(source_items, ensure_ascii=False, separators=(",", ":")),
        }
    )
    checkpoint_count = ensure_channel_checkpoints(list(latest_id_map), latest_id_map)
    message = f"已加入 {added_count} 个新来源，并创建 {checkpoint_count} 条从当前最新消息开始的断点。"
    if skipped_duplicates:
        message += f" 已跳过 {skipped_duplicates} 个与当前配置重复的来源。"
    if failed_sources:
        message += " 未加入：" + "、".join(failed_sources) + "。"
    return redirect_with_message("/forward-settings", message, "success")


@app.post("/settings/add-cid")
@app.post("/forward-settings/add-cid")
async def add_cid_to_channel_ids(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    return redirect_with_message("/forward-settings", "流程已升级，请在流程图中解析后直接保存通道配置。", "info")


@app.post("/checkpoints/upsert")
@app.post("/forward-settings/checkpoints/upsert")
async def upsert_checkpoint(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    raw_channel_id = str(form.get("channel_id", "")).strip()
    raw_last_id = str(form.get("last_id", "")).strip()

    if not raw_channel_id:
        return redirect_with_message("/forward-settings", "保存失败：频道 ID 不能为空。", "warn")

    try:
        channel_id = int(raw_channel_id)
    except ValueError:
        return redirect_with_message("/forward-settings", "保存失败：频道 ID 必须是整数。", "warn")

    try:
        last_id = int(raw_last_id)
    except ValueError:
        return redirect_with_message("/forward-settings", "保存失败：last_id 必须是整数。", "warn")

    if last_id < 0:
        return redirect_with_message("/forward-settings", "保存失败：last_id 不能小于 0。", "warn")

    checkpoint_store.set_last_id(channel_id, last_id)
    return redirect_with_message("/forward-settings", "断点保存成功。", "success")


@app.post("/forward-settings/checkpoints/batch-save")
async def batch_save_checkpoints(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    channel_ids_raw = form.getlist("batch_channel_id")
    last_ids_raw = form.getlist("batch_last_id")

    total = min(len(channel_ids_raw), len(last_ids_raw))
    saved_count = 0
    for idx in range(total):
        raw_channel_id = str(channel_ids_raw[idx]).strip()
        raw_last_id = str(last_ids_raw[idx]).strip()

        try:
            channel_id = int(raw_channel_id)
            last_id = int(raw_last_id)
        except ValueError:
            continue

        if last_id < 0:
            continue

        checkpoint_store.set_last_id(channel_id, last_id)
        saved_count += 1

    if saved_count == 0:
        return redirect_with_message("/forward-settings", "未保存任何断点（请检查输入）。", "warn")
    return redirect_with_message("/forward-settings", f"断点批量保存成功（{saved_count} 条）。", "success")


@app.get("/plan-backup")
async def plan_backup_page(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    context = common_context(request, "计划与备份")
    context.update(
        {
            "config": config_store.load_raw_config(),
            "backups": backup_manager.list_backups(),
        }
    )
    return templates.TemplateResponse("plan_backup.html", context)


@app.post("/plan-backup/save")
async def plan_backup_save(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    current = config_store.load_raw_config()

    keys = [
        "PANEL_TEST_MODE_ENABLED",
        "PANEL_TOTAL_TIMEOUT_SECONDS",
        "PANEL_AUTO_RUN_ENABLED",
        "PANEL_AUTO_RUN_INTERVAL_MINUTES",
        "PANEL_HISTORY_RETENTION_DAYS",
    ]
    payload = collect_form_payload(
        form,
        current,
        keys,
        bool_keys={"PANEL_TEST_MODE_ENABLED", "PANEL_AUTO_RUN_ENABLED"},
    )
    config_store.save_raw_config(payload)
    return redirect_with_message("/plan-backup", "计划与调度配置已保存。", "success")


@app.post("/plan-backup/cleanup")
async def cleanup_runtime_files(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    removed_files = 0
    removed_dirs = 0
    recovered_bytes = 0

    def remove_file(path: Path) -> None:
        nonlocal removed_files, recovered_bytes
        if not path.exists() or not path.is_file():
            return
        try:
            recovered_bytes += path.stat().st_size
        except OSError:
            pass
        path.unlink(missing_ok=True)
        removed_files += 1

    def remove_tree(path: Path) -> None:
        nonlocal removed_dirs, recovered_bytes
        if not path.exists() or not path.is_dir():
            return
        try:
            for item in path.rglob("*"):
                if item.is_file():
                    try:
                        recovered_bytes += item.stat().st_size
                    except OSError:
                        pass
            shutil.rmtree(path, ignore_errors=True)
            removed_dirs += 1
        except OSError:
            shutil.rmtree(path, ignore_errors=True)

    downloads_dir = config_store.download_dir
    if downloads_dir.exists():
        for entry in downloads_dir.iterdir():
            if entry.is_dir():
                remove_tree(entry)
            else:
                remove_file(entry)

    if config_store.state_dir.exists():
        for entry in config_store.state_dir.iterdir():
            if entry.name == "downloads":
                continue
            if entry.name.startswith("tmp_") and entry.is_dir():
                remove_tree(entry)

    if not runner.is_running and config_store.lock_file.exists():
        remove_file(config_store.lock_file)

    session_sidecars = [
        Path(f"{config_store.session_base_path}.session-journal"),
        Path(f"{config_store.session_base_path}.session-shm"),
        Path(f"{config_store.session_base_path}.session-wal"),
        Path(f"{config_store.legacy_session_base_path}.session-journal"),
        Path(f"{config_store.legacy_session_base_path}.session-shm"),
        Path(f"{config_store.legacy_session_base_path}.session-wal"),
    ]
    for sidecar in session_sidecars:
        if sidecar.exists():
            remove_file(sidecar)

    for tmp_backup in backup_manager.backups_dir.glob("uploaded_restore_*.zip"):
        remove_file(tmp_backup)

    recovered_mb = recovered_bytes / (1024 * 1024)
    return redirect_with_message(
        "/plan-backup",
        f"清理完成：删除文件 {removed_files} 个，删除目录 {removed_dirs} 个，释放约 {recovered_mb:.2f} MB。",
        "success",
    )


@app.post("/backups/create")
@app.post("/plan-backup/backups/create")
async def create_backup(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    try:
        backup_file = backup_manager.create_backup()
        return redirect_with_message("/plan-backup", f"备份创建成功：{backup_file.name}", "success")
    except Exception as exc:
        return redirect_with_message("/plan-backup", f"备份创建失败：{exc}", "error")


@app.get("/backups/download/{backup_name}")
@app.get("/plan-backup/backups/download/{backup_name}")
async def download_backup(request: Request, backup_name: str):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    backup_file = backup_manager.resolve_backup(backup_name)
    if backup_file is None:
        raise HTTPException(status_code=404, detail="未找到备份文件")
    return FileResponse(path=str(backup_file), filename=backup_file.name, media_type="application/zip")


@app.post("/backups/delete/{backup_name}")
@app.post("/plan-backup/backups/delete/{backup_name}")
async def delete_backup(request: Request, backup_name: str):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    deleted = backup_manager.delete_backup(backup_name)
    if deleted:
        return redirect_with_message("/plan-backup", f"备份已删除：{backup_name}", "success")
    return redirect_with_message("/plan-backup", "备份不存在或备份名无效。", "warn")


@app.post("/plan-backup/backups/restore/{backup_name}")
async def restore_backup(request: Request, backup_name: str):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    if runner.is_running:
        return redirect_with_message("/plan-backup", "当前有转发任务运行中，请先停止后再恢复备份。", "warn")

    backup_file = backup_manager.resolve_backup(backup_name)
    if backup_file is None:
        return redirect_with_message("/plan-backup", "待恢复备份不存在。", "warn")

    try:
        rollback_backup = backup_manager.create_backup_with_prefix("pre_restore_auto")
        result = backup_manager.restore_from_backup(backup_file)
        rebind_count = rebind_logger_file_handler(logger, config_store.log_file)
        logger.info("日志文件句柄已重绑，已替换 file handler: %s", rebind_count)
        logger.warning("♻️ 已从备份恢复数据: %s", backup_file.name)
        return redirect_with_message(
            "/plan-backup",
            (
                f"恢复成功（删除 {result['deleted_count']} 项，恢复 {result['copied_count']} 项）。"
                f"已自动创建回滚备份：{rollback_backup.name}。建议重启容器。"
            ),
            "success",
        )
    except Exception as exc:
        return redirect_with_message("/plan-backup", f"恢复失败：{exc}", "error")


@app.post("/plan-backup/backups/restore-upload")
async def restore_backup_upload(request: Request, file: UploadFile = File(...)):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    if runner.is_running:
        return redirect_with_message("/plan-backup", "当前有转发任务运行中，请先停止后再恢复备份。", "warn")

    if not file.filename or not file.filename.lower().endswith(".zip"):
        return redirect_with_message("/plan-backup", "请上传 .zip 备份文件。", "warn")

    payload = await file.read()
    if not payload:
        return redirect_with_message("/plan-backup", "上传的备份文件为空。", "warn")

    backup_manager.ensure_directory()
    stamp = now_shanghai_iso().replace(" ", "_").replace(":", "")
    upload_backup = backup_manager.backups_dir / f"uploaded_restore_{stamp}.zip"
    upload_backup.write_bytes(payload)

    try:
        rollback_backup = backup_manager.create_backup_with_prefix("pre_restore_auto")
        result = backup_manager.restore_from_backup(upload_backup)
        rebind_count = rebind_logger_file_handler(logger, config_store.log_file)
        logger.info("日志文件句柄已重绑，已替换 file handler: %s", rebind_count)
        logger.warning("♻️ 已从上传备份恢复数据: %s", upload_backup.name)
        return redirect_with_message(
            "/plan-backup",
            (
                f"上传备份恢复成功（删除 {result['deleted_count']} 项，恢复 {result['copied_count']} 项）。"
                f"已自动创建回滚备份：{rollback_backup.name}。建议重启容器。"
            ),
            "success",
        )
    except Exception as exc:
        return redirect_with_message("/plan-backup", f"上传备份恢复失败：{exc}", "error")


@app.get("/settings")
async def settings_redirect(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect
    return RedirectResponse(url="/setup", status_code=303)


@app.post("/settings")
async def settings_save_redirect(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect
    return RedirectResponse(url="/setup", status_code=303)


@app.get("/checkpoints")
async def checkpoints_redirect(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect
    return RedirectResponse(url="/forward-settings", status_code=303)


@app.get("/backups")
async def backups_redirect(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect
    return RedirectResponse(url="/plan-backup", status_code=303)


@app.get("/session")
async def session_page(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect
    return RedirectResponse(url="/setup", status_code=303)


@app.post("/session/upload")
async def upload_session(request: Request, file: UploadFile = File(...)):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    if not file.filename or not file.filename.lower().endswith(".session"):
        return redirect_with_message("/setup", "请上传有效的 .session 文件。", "error")

    payload = await file.read()
    if not payload:
        return redirect_with_message("/setup", "上传文件为空。", "error")

    config_store.session_dir.mkdir(parents=True, exist_ok=True)
    cleanup_candidates = [
        Path(f"{config_store.session_base_path}.session"),
        Path(f"{config_store.session_base_path}.session-journal"),
        Path(f"{config_store.session_base_path}.session-shm"),
        Path(f"{config_store.session_base_path}.session-wal"),
        Path(f"{config_store.legacy_session_base_path}.session"),
        Path(f"{config_store.legacy_session_base_path}.session-journal"),
        Path(f"{config_store.legacy_session_base_path}.session-shm"),
        Path(f"{config_store.legacy_session_base_path}.session-wal"),
    ]
    for candidate in cleanup_candidates:
        if candidate.exists():
            candidate.unlink(missing_ok=True)

    config_store.session_file.write_bytes(payload)
    return redirect_with_message("/setup", "会话文件上传成功，已保存为 t2rss.session。", "success")


@app.post("/checkpoints/delete")
@app.post("/forward-settings/checkpoints/delete")
async def delete_checkpoint(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    raw_channel_id = str(form.get("channel_id", "") or form.get("delete_channel_id", "")).strip()
    if not raw_channel_id:
        return redirect_with_message("/forward-settings", "删除失败：频道 ID 不能为空。", "warn")

    try:
        channel_id = int(raw_channel_id)
    except ValueError:
        return redirect_with_message("/forward-settings", "删除失败：频道 ID 必须是整数。", "warn")

    deleted = checkpoint_store.delete_last_id(channel_id)
    if deleted:
        return redirect_with_message("/forward-settings", f"断点已删除：{channel_id}", "success")
    return redirect_with_message("/forward-settings", f"未找到断点记录：{channel_id}", "warn")


@app.post("/session/delete")
async def delete_session_file(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    candidates = [
        Path(f"{config_store.session_base_path}.session"),
        Path(f"{config_store.session_base_path}.session-journal"),
        Path(f"{config_store.session_base_path}.session-shm"),
        Path(f"{config_store.session_base_path}.session-wal"),
        Path(f"{config_store.legacy_session_base_path}.session"),
        Path(f"{config_store.legacy_session_base_path}.session-journal"),
        Path(f"{config_store.legacy_session_base_path}.session-shm"),
        Path(f"{config_store.legacy_session_base_path}.session-wal"),
    ]

    deleted_any = False
    for candidate in candidates:
        if candidate.exists():
            candidate.unlink(missing_ok=True)
            deleted_any = True

    if deleted_any:
        return redirect_with_message("/setup", "会话文件已删除。", "success")
    return redirect_with_message("/setup", "未发现可删除的会话文件。", "warn")


@app.get("/api/status")
async def api_status(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    return JSONResponse(runner.status_payload())


@app.get("/api/checkpoints")
async def api_checkpoints(request: Request):
    if request.session.get("authenticated") is not True:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    raw_config = config_store.load_raw_config()
    rows = build_checkpoint_rows(raw_config)
    return JSONResponse({"items": rows, "updated_at": now_shanghai_iso()})


@app.get("/api/logs/tail")
async def api_logs_tail(request: Request):
    if request.session.get("authenticated") is not True:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    raw_lines = str(request.query_params.get("lines", "300"))
    try:
        line_limit = int(raw_lines)
    except ValueError:
        line_limit = 300

    line_limit = max(20, min(line_limit, 2000))
    log_text = read_panel_log_tail(config_store.log_file, line_limit=line_limit)

    return JSONResponse(
        {
            "log_text": log_text,
            "line_limit": line_limit,
            "updated_at": now_shanghai_iso(),
        }
    )


@app.post("/api/logs/clear")
async def api_logs_clear(request: Request):
    if request.session.get("authenticated") is not True:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    clear_panel_log(config_store.log_file)
    return JSONResponse(
        {
            "ok": True,
            "message": "日志已清空。",
            "updated_at": now_shanghai_iso(),
        }
    )
