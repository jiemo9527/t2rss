import asyncio
import collections
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any, Dict, List, Optional, Set

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageEntityMentionName, MessageEntityTextUrl, MessageService

from .checkpoint_store import ChannelCheckpointStore
from .config_store import ConfigStore, ForwarderConfig
from .time_utils import now_shanghai_iso


QUARK_LINK_PATTERN = re.compile(r"https?://pan\.quark\.cn/s/[a-zA-Z0-9]+", re.IGNORECASE)
CLOUD115_LINK_PATTERN = re.compile(r"https?://(?:www\.)?115cdn\.com/s/[a-zA-Z0-9]+", re.IGNORECASE)
HDHIVE115_LINK_PATTERN = re.compile(r"https?://(?:www\.)?hdhive\.com/resource/115/[a-zA-Z0-9]+", re.IGNORECASE)
BAIDU_LINK_PATTERN = re.compile(r"https?://pan\.baidu\.com/s/[a-zA-Z0-9_-]+", re.IGNORECASE)
BAIDU_SHORT_LINK_PATTERN = re.compile(r"https?://pan\.baidu\.com/share/init\?surl=[a-zA-Z0-9_-]+", re.IGNORECASE)
UC_LINK_PATTERN = re.compile(r"https?://(?:drive|fast)\.uc\.cn/s/[a-zA-Z0-9]+", re.IGNORECASE)

# Restricted providers: when a message's ONLY netdisk links come from these,
# it is not forwarded unless that provider's toggle is on.
XUNLEI_LINK_PATTERN = re.compile(r"https?://pan\.xunlei\.com/s/[a-zA-Z0-9_-]+", re.IGNORECASE)
# 123 pan rotates its domain (123pan/123684/123865/123912/...).
PAN123_LINK_PATTERN = re.compile(
    r"https?://(?:www\.)?123(?:pan|[0-9]{3})\.com/s/[a-zA-Z0-9_-]+", re.IGNORECASE
)
CAIYUN_LINK_PATTERN = re.compile(
    r"https?://yun\.139\.com/shareweb/#/w/i/[a-zA-Z0-9_-]+", re.IGNORECASE
)
GUANGYA_LINK_PATTERN = re.compile(
    r"https?://(?:www\.)?guangyapan\.com/s/[a-zA-Z0-9_-]+", re.IGNORECASE
)
ALIYUN_LINK_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:alipan|aliyundrive)\.com/s/[a-zA-Z0-9_-]+", re.IGNORECASE
)
URL_PATTERN = re.compile(r'https?://[^\s<>"]+')
BOT_TRIGGER_PHRASE = "点击获取夸克链接"
SEND_RETRY_MAX_ATTEMPTS = 3
SEND_RETRY_BASE_DELAY_SECONDS = 2
SEND_INTERVAL_SECONDS = 3
MEDIA_DOWNLOAD_TIMEOUT_SECONDS = 180


def _video_media_size_bytes(message) -> Optional[int]:
    """Return the byte size when the message carries a real video, else None.

    Animated GIFs (DocumentAttributeAnimated) are excluded on purpose: they are
    mp4 containers but behave as stickers/emotes, not as the large videos we skip.
    """
    document = getattr(message, "video", None)
    if document is None:
        return None
    if getattr(message, "gif", None) is not None:
        return None
    size = getattr(document, "size", None)
    if not isinstance(size, int):
        return None
    return size


QUARK_TRIGGER_LINK_PAREN_PATTERN = re.compile(
    rf"{re.escape(BOT_TRIGGER_PHRASE)}\s*[（(]\s*(?P<url>(?:https?://t\.me/[^\s)）]+|tg://resolve[^\s)）]+))\s*[)）]"
)
QUARK_TRIGGER_LINK_INLINE_PATTERN = re.compile(
    rf"{re.escape(BOT_TRIGGER_PHRASE)}\s*(?P<url>(?:https?://t\.me/\S+|tg://resolve\S+))"
)
QUARK_TRIGGER_MARKDOWN_PATTERN = re.compile(
    rf"\[[^\]]*{re.escape(BOT_TRIGGER_PHRASE)}[^\]]*\]\((?P<url>(?:https?://t\.me/[^\s)]+|tg://resolve[^\s)]+))\)"
)
TME_JUMP_LINK_PATTERN = re.compile(r"(?:https?://t\.me/\S+|tg://resolve\S+)")


def _has_quark_trigger_phrase(text: Optional[str]) -> bool:
    return BOT_TRIGGER_PHRASE in str(text or "")


def _is_quark_jump_link(url: str) -> bool:
    lower = str(url or "").lower()
    if not lower:
        return False
    if "quark" in lower:
        return True
    if "start=" in lower and "_quark" in lower:
        return True
    return False


def _normalize_quark_link(raw_url: Optional[str]) -> Optional[str]:
    match = QUARK_LINK_PATTERN.search(str(raw_url or ""))
    if not match:
        return None

    parsed = urlparse(match.group(0))
    token = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if not token:
        return None
    return f"https://pan.quark.cn/s/{token}"


def _normalize_115_link(raw_url: Optional[str]) -> Optional[str]:
    content = str(raw_url or "")
    candidates: List[tuple[int, str]] = []

    for match in CLOUD115_LINK_PATTERN.finditer(content):
        parsed = urlparse(match.group(0))
        token = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if token:
            candidates.append((match.start(), f"https://115cdn.com/s/{token}"))

    for match in HDHIVE115_LINK_PATTERN.finditer(content):
        parsed = urlparse(match.group(0))
        token = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if token:
            candidates.append((match.start(), f"https://hdhive.com/resource/115/{token}"))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _normalize_baidu_link(raw_url: Optional[str]) -> Optional[str]:
    content = str(raw_url or "")
    candidates: List[tuple[int, str]] = []

    for match in BAIDU_LINK_PATTERN.finditer(content):
        parsed = urlparse(match.group(0))
        token = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if token:
            candidates.append((match.start(), f"https://pan.baidu.com/s/{token}"))

    # `share/init?surl=<token>` is the same share as `/s/1<token>`; normalize to one key.
    for match in BAIDU_SHORT_LINK_PATTERN.finditer(content):
        surl = parse_qs(urlparse(match.group(0)).query).get("surl", [""])[0]
        if surl:
            candidates.append((match.start(), f"https://pan.baidu.com/s/1{surl}"))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _normalize_uc_link(raw_url: Optional[str]) -> Optional[str]:
    match = UC_LINK_PATTERN.search(str(raw_url or ""))
    if not match:
        return None

    parsed = urlparse(match.group(0))
    token = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if not token:
        return None
    # drive.uc.cn and fast.uc.cn serve the same share token; collapse to one key.
    return f"https://drive.uc.cn/s/{token}"


def extract_quark_link(text: Optional[str]) -> Optional[str]:
    return _normalize_quark_link(text)


def extract_115_link(text: Optional[str]) -> Optional[str]:
    return _normalize_115_link(text)


def extract_baidu_link(text: Optional[str]) -> Optional[str]:
    return _normalize_baidu_link(text)


def extract_uc_link(text: Optional[str]) -> Optional[str]:
    return _normalize_uc_link(text)


# Dedup provider priority: quark > 115 > baidu > uc. Only the highest-priority
# link present in a message becomes its dedup key.
DEDUP_PROVIDERS: List[tuple[str, Any]] = [
    ("quark", extract_quark_link),
    ("115", extract_115_link),
    ("baidu", extract_baidu_link),
    ("uc", extract_uc_link),
]


def _enabled_dedup_providers(
    include_115: bool = True,
    include_baidu: bool = True,
    include_uc: bool = True,
) -> List[tuple[str, Any]]:
    toggles = {"quark": True, "115": include_115, "baidu": include_baidu, "uc": include_uc}
    return [(name, fn) for name, fn in DEDUP_PROVIDERS if toggles.get(name, False)]


# Restricted netdisk providers. A message whose ONLY netdisk links come from
# disabled providers here is skipped entirely; enabling a provider makes its
# links count as ordinary content again.
RESTRICTED_PROVIDERS: List[tuple[str, Any]] = [
    ("xunlei", XUNLEI_LINK_PATTERN),
    ("pan123", PAN123_LINK_PATTERN),
    ("caiyun", CAIYUN_LINK_PATTERN),
    ("guangya", GUANGYA_LINK_PATTERN),
    ("aliyun", ALIYUN_LINK_PATTERN),
]

RESTRICTED_PROVIDER_LABELS = {
    "xunlei": "迅雷",
    "pan123": "123网盘",
    "caiyun": "移动云盘",
    "guangya": "光鸭云盘",
    "aliyun": "阿里云盘",
}


def _message_link_sources(message, resolved_url: Optional[str] = None) -> List[str]:
    """All places a share link can hide: text, blue-link entities, buttons, bot result."""
    message_text = (
        getattr(message, "raw_text", None)
        or getattr(message, "message", None)
        or getattr(message, "text", None)
        or getattr(message, "caption", None)
    )

    sources: List[str] = []
    if message_text:
        sources.append(str(message_text))
    if resolved_url:
        sources.append(str(resolved_url))

    for entity in getattr(message, "entities", None) or []:
        if not isinstance(entity, MessageEntityTextUrl):
            continue
        entity_url = str(getattr(entity, "url", "") or "")
        if entity_url:
            sources.append(entity_url)

    for button_url in _extract_button_urls(message):
        if button_url:
            sources.append(button_url)

    return sources


def find_restricted_providers(
    message,
    disabled_providers: Set[str],
    resolved_url: Optional[str] = None,
) -> List[str]:
    """Names of DISABLED restricted providers whose links appear in the message."""
    if not disabled_providers:
        return []

    sources = _message_link_sources(message, resolved_url)
    if not sources:
        return []

    found: List[str] = []
    for name, pattern in RESTRICTED_PROVIDERS:
        if name not in disabled_providers:
            continue
        if any(pattern.search(source) for source in sources):
            found.append(name)
    return found


def has_allowed_netdisk_link(
    message,
    enabled_restricted: Optional[Set[str]] = None,
    resolved_url: Optional[str] = None,
) -> bool:
    """True if the message carries a link from any provider the user allows.

    Allowed = the four dedup providers (quark/115/baidu/uc) plus every restricted
    provider currently switched ON. Dedup toggles are forced ON for this check so
    that narrowing *dedup* scope never silently makes a message look
    'restricted-only' — dedup scope and forwarding eligibility stay independent.
    """
    sources = _message_link_sources(message, resolved_url)
    for source in sources:
        if extract_dedup_link(source, include_115=True, include_baidu=True, include_uc=True):
            return True

    for name, pattern in RESTRICTED_PROVIDERS:
        if name not in (enabled_restricted or set()):
            continue
        if any(pattern.search(source) for source in sources):
            return True
    return False


def extract_dedup_link(
    text: Optional[str],
    include_115: bool = True,
    include_baidu: bool = True,
    include_uc: bool = True,
) -> Optional[str]:
    content = str(text or "")
    if not content:
        return None

    for _, extractor in _enabled_dedup_providers(include_115, include_baidu, include_uc):
        link = extractor(content)
        if link:
            return link
    return None


def _extract_message_dedup_link(
    message,
    resolved_url: Optional[str] = None,
    include_115: bool = True,
    include_baidu: bool = True,
    include_uc: bool = True,
) -> Optional[str]:
    message_text = (
        getattr(message, "raw_text", None)
        or getattr(message, "message", None)
        or getattr(message, "text", None)
        or getattr(message, "caption", None)
    )

    link_sources: List[str] = []
    if message_text:
        link_sources.append(str(message_text))
    if resolved_url:
        link_sources.append(str(resolved_url))

    entities = getattr(message, "entities", None) or []
    for entity in entities:
        if not isinstance(entity, MessageEntityTextUrl):
            continue
        entity_url = str(getattr(entity, "url", "") or "")
        if entity_url:
            link_sources.append(entity_url)

    for button_url in _extract_button_urls(message):
        if button_url:
            link_sources.append(button_url)

    for _, extractor in _enabled_dedup_providers(include_115, include_baidu, include_uc):
        for source in link_sources:
            link = extractor(source)
            if link:
                return link

    return None


def _clean_url_token(url: str) -> str:
    return str(url or "").strip().rstrip(").,，。!！?？\"'")


def _extract_urls_from_text(text: Optional[str]) -> List[str]:
    if not text:
        return []

    urls: List[str] = []
    seen: Set[str] = set()
    for item in URL_PATTERN.findall(str(text)):
        normalized = _clean_url_token(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
    return urls


def _extract_button_urls(message) -> List[str]:
    urls: List[str] = []
    seen: Set[str] = set()
    rows = getattr(message, "buttons", None) or []

    for row in rows:
        if row is None:
            continue

        button_items = row if isinstance(row, (list, tuple)) else [row]
        for button in button_items:
            if button is None:
                continue

            url = getattr(button, "url", None)
            if not url:
                raw_button = getattr(button, "button", None)
                url = getattr(raw_button, "url", None)

            normalized = _clean_url_token(str(url or ""))
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            urls.append(normalized)

    return urls


def _parse_bot_command_from_link(raw_url: str) -> Optional[tuple[str, str, str]]:
    url = str(raw_url or "").strip()
    if not url:
        return None

    bot_username = ""
    start_payload = ""

    if url.startswith("tg://"):
        parsed = urlparse(url)
        if parsed.netloc.lower() != "resolve":
            return None
        query = parse_qs(parsed.query)
        bot_username = str((query.get("domain") or [""])[0]).strip().lstrip("@")
        start_payload = str((query.get("start") or query.get("startapp") or [""])[0]).strip()
    else:
        if url.startswith("http://"):
            url = "https://" + url[len("http://") :]

        parsed = urlparse(url)
        if parsed.netloc.lower() not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            return None

        path_token = parsed.path.strip("/")
        if not path_token:
            return None

        bot_username = path_token.split("/", 1)[0].strip().lstrip("@")
        query = parse_qs(parsed.query)
        start_payload = str((query.get("start") or query.get("startapp") or [""])[0]).strip()

    if not bot_username or not re.fullmatch(r"[A-Za-z0-9_]{3,}", bot_username):
        return None

    cache_key = bot_username
    command = "/start"
    if start_payload:
        cache_key = f"{bot_username}?start={start_payload}"
        command = f"/start {start_payload}"

    return cache_key, bot_username, command


def _extract_bot_links_from_message(message) -> List[str]:
    links: List[str] = []
    seen: Set[str] = set()

    message_text = getattr(message, "text", None) or getattr(message, "caption", None)
    for url in _extract_urls_from_text(message_text):
        if "t.me/" in url.lower() and url not in seen:
            seen.add(url)
            links.append(url)

    entities = getattr(message, "entities", None) or []
    for entity in entities:
        if not isinstance(entity, MessageEntityTextUrl):
            continue
        entity_url = _clean_url_token(str(getattr(entity, "url", "") or ""))
        if not entity_url:
            continue
        lower_url = entity_url.lower()
        if ("t.me/" in lower_url or lower_url.startswith("tg://")) and entity_url not in seen:
            seen.add(entity_url)
            links.append(entity_url)

    for button_url in _extract_button_urls(message):
        lower_url = button_url.lower()
        if ("t.me/" in lower_url or lower_url.startswith("tg://")) and button_url not in seen:
            seen.add(button_url)
            links.append(button_url)

    return links


def _extract_quark_trigger_bot_links_from_text(text: Optional[str]) -> List[str]:
    content = str(text or "")
    urls: List[str] = []
    seen: Set[str] = set()

    for match in QUARK_TRIGGER_LINK_PAREN_PATTERN.finditer(content):
        url = _clean_url_token(match.group("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)

    for match in QUARK_TRIGGER_LINK_INLINE_PATTERN.finditer(content):
        url = _clean_url_token(match.group("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)

    trigger_index = content.find(BOT_TRIGGER_PHRASE)
    if trigger_index >= 0:
        after_text = content[trigger_index + len(BOT_TRIGGER_PHRASE) :]
        next_match = TME_JUMP_LINK_PATTERN.search(after_text)
        if next_match:
            url = _clean_url_token(next_match.group(0))
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

    return urls


def _extract_quark_trigger_bot_links(message) -> List[str]:
    links: List[str] = []
    seen: Set[str] = set()
    message_text = (
        getattr(message, "raw_text", None)
        or getattr(message, "message", None)
        or getattr(message, "text", None)
        or getattr(message, "caption", None)
        or ""
    )

    for url in _extract_quark_trigger_bot_links_from_text(message_text):
        if url in seen:
            continue
        seen.add(url)
        links.append(url)

    entities = getattr(message, "entities", None) or []
    for entity in entities:
        if not isinstance(entity, MessageEntityTextUrl):
            continue

        start = int(getattr(entity, "offset", 0) or 0)
        length = int(getattr(entity, "length", 0) or 0)
        end = start + max(length, 0)
        entity_text = message_text[start:end] if length > 0 and start >= 0 and end <= len(message_text) else ""

        entity_url = _clean_url_token(str(getattr(entity, "url", "") or ""))
        lower_url = entity_url.lower()
        if "t.me/" not in lower_url and not lower_url.startswith("tg://"):
            continue

        if BOT_TRIGGER_PHRASE not in entity_text and not _is_quark_jump_link(entity_url):
            continue

        if not entity_url or entity_url in seen:
            continue
        seen.add(entity_url)
        links.append(entity_url)

    rows = getattr(message, "buttons", None) or []
    for row in rows:
        if row is None:
            continue
        button_items = row if isinstance(row, (list, tuple)) else [row]
        for button in button_items:
            if button is None:
                continue
            button_text = str(getattr(button, "text", "") or "")

            button_url = _clean_url_token(str(getattr(button, "url", "") or ""))
            if not button_url:
                raw_button = getattr(button, "button", None)
                button_url = _clean_url_token(str(getattr(raw_button, "url", "") or ""))

            lower_url = button_url.lower()
            if "t.me/" not in lower_url and not lower_url.startswith("tg://"):
                continue

            if BOT_TRIGGER_PHRASE not in button_text and not _is_quark_jump_link(button_url):
                continue

            if not button_url or button_url in seen:
                continue
            seen.add(button_url)
            links.append(button_url)
    return links


def _replace_quark_trigger_segment(text: str, resolved_url: str) -> str:
    content = str(text or "")
    replacement = str(resolved_url or "").strip()
    if not replacement:
        return content

    content = QUARK_TRIGGER_MARKDOWN_PATTERN.sub(replacement, content)

    content = QUARK_TRIGGER_LINK_PAREN_PATTERN.sub(f"{replacement} ({replacement})", content)
    content = QUARK_TRIGGER_LINK_INLINE_PATTERN.sub(f"{replacement} {replacement}", content)

    if BOT_TRIGGER_PHRASE in content:
        content = content.replace(BOT_TRIGGER_PHRASE, replacement)
    return content


def _materialize_text_url_entities(
    text: str,
    entities,
    skip_urls: Optional[Set[str]] = None,
    message=None,
) -> str:
    content = str(text or "")
    if not content or not entities:
        return content

    skip_link_set = {_clean_url_token(item) for item in (skip_urls or set()) if str(item).strip()}

    get_entities_text = getattr(message, "get_entities_text", None)
    if callable(get_entities_text):
        try:
            pair_candidates: List[tuple[str, str]] = []
            raw_pairs = get_entities_text(MessageEntityTextUrl)
            try:
                raw_pairs_iter = iter(raw_pairs)
            except TypeError:
                raw_pairs_iter = iter(())

            for pair in raw_pairs_iter:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    continue
                entity, entity_text = pair
                entity_url = _clean_url_token(str(getattr(entity, "url", "") or ""))
                anchor_text = str(entity_text or "").strip()
                if not entity_url or entity_url in skip_link_set:
                    continue
                if BOT_TRIGGER_PHRASE in anchor_text:
                    continue
                pair_candidates.append((anchor_text, entity_url))

            if pair_candidates:
                append_later: List[str] = []
                for anchor_text, entity_url in pair_candidates:
                    if entity_url in content:
                        continue

                    replacement = f"{anchor_text} ({entity_url})" if anchor_text else entity_url
                    if anchor_text and anchor_text in content:
                        content = content.replace(anchor_text, replacement, 1)
                    else:
                        append_later.append(replacement)

                for item in append_later:
                    normalized_item = str(item or "").strip()
                    if not normalized_item or normalized_item in content:
                        continue
                    content = f"{content}\n{normalized_item}" if content else normalized_item

                return content
        except Exception:
            pass

    candidates: List[tuple[int, int, str]] = []

    for entity in entities:
        if not isinstance(entity, MessageEntityTextUrl):
            continue

        entity_url = _clean_url_token(str(getattr(entity, "url", "") or ""))
        if not entity_url or entity_url in skip_link_set:
            continue

        start = int(getattr(entity, "offset", 0) or 0)
        length = int(getattr(entity, "length", 0) or 0)
        end = start + length
        if length <= 0 or start < 0 or end > len(content):
            continue

        candidates.append((start, length, entity_url))

    if not candidates:
        return content

    for start, length, entity_url in sorted(candidates, key=lambda item: item[0], reverse=True):
        anchor_text = content[start : start + length]
        if BOT_TRIGGER_PHRASE in anchor_text:
            continue

        if entity_url in anchor_text:
            replacement = anchor_text
        else:
            replacement = f"{anchor_text} ({entity_url})"
        content = content[:start] + replacement + content[start + length :]

    return content


def _extract_url_from_bot_message(message) -> Optional[str]:
    message_text = getattr(message, "text", None) or getattr(message, "caption", None) or getattr(message, "raw_text", None)
    quark_link = extract_quark_link(message_text)
    if quark_link:
        return quark_link

    urls = _extract_urls_from_text(message_text)
    for url in urls:
        if "pan.quark.cn/s/" in url:
            return url

    button_urls = _extract_button_urls(message)
    for url in button_urls:
        if "pan.quark.cn/s/" in url:
            return url

    if urls:
        return urls[0]
    if button_urls:
        return button_urls[0]
    return None


async def _resolve_link_via_bot(
    client: TelegramClient,
    message,
    logger,
    bot_link_cache: Dict[str, Optional[str]],
) -> Optional[str]:
    message_text = (
        getattr(message, "raw_text", None)
        or getattr(message, "message", None)
        or getattr(message, "text", None)
        or getattr(message, "caption", None)
        or ""
    )
    if not _has_quark_trigger_phrase(message_text):
        return None

    message_id = getattr(message, "id", "unknown")
    bot_links = _extract_quark_trigger_bot_links(message)
    if not bot_links:
        logger.info("消息 %s 含夸克触发词，但未找到关联的 Bot 跳转链接。", message_id)
        return None

    for bot_link in bot_links:
        parsed = _parse_bot_command_from_link(bot_link)
        if not parsed:
            continue

        cache_key, bot_username, command = parsed
        if cache_key in bot_link_cache:
            cached_value = bot_link_cache[cache_key]
            if cached_value:
                return cached_value
            continue

        resolved_url: Optional[str] = None
        try:
            async with client.conversation(bot_username, timeout=25) as conversation:
                await conversation.send_message(command)
                for _ in range(4):
                    response = await conversation.get_response(timeout=15)
                    resolved_url = _extract_url_from_bot_message(response)
                    if resolved_url:
                        break
        except Exception as exc:
            logger.warning("消息 %s 跳转 Bot %s 解析失败: %s", message_id, bot_username, exc)

        bot_link_cache[cache_key] = resolved_url
        if resolved_url:
            logger.info("消息 %s 已通过 Bot %s 解析得到链接。", message_id, bot_username)
            return resolved_url

    logger.info("消息 %s 触发 Bot 解析，但未获取到有效链接。", message_id)
    return None


async def _send_message_with_retry(
    client: TelegramClient,
    destination_channel: str,
    outbound_text: Optional[str],
    media_path: Optional[str],
    formatting_entities,
    logger,
    message_id: Any,
) -> bool:
    for attempt in range(1, SEND_RETRY_MAX_ATTEMPTS + 1):
        try:
            await client.send_message(
                destination_channel,
                outbound_text or None,
                file=media_path,
                parse_mode=None,
                formatting_entities=formatting_entities,
            )
            if attempt > 1:
                logger.info("消息 %s 重试后发送成功（第 %s 次）。", message_id, attempt)
            return True
        except FloodWaitError as exc:
            wait_seconds = int(getattr(exc, "seconds", 0) or 0)
            if attempt >= SEND_RETRY_MAX_ATTEMPTS:
                logger.error(
                    "消息 %s 发送失败：触发 FloodWait，重试已达上限（%s 次，需等待 %s 秒）。",
                    message_id,
                    SEND_RETRY_MAX_ATTEMPTS,
                    wait_seconds,
                )
                return False

            sleep_seconds = max(wait_seconds, SEND_RETRY_BASE_DELAY_SECONDS)
            logger.warning(
                "消息 %s 发送触发 FloodWait，将在 %s 秒后进行第 %s 次重试。",
                message_id,
                sleep_seconds,
                attempt + 1,
            )
            await asyncio.sleep(sleep_seconds)
        except Exception as exc:
            if attempt >= SEND_RETRY_MAX_ATTEMPTS:
                logger.exception("消息 %s 发送最终失败（已重试 %s 次）: %s", message_id, SEND_RETRY_MAX_ATTEMPTS, exc)
                return False

            sleep_seconds = min(SEND_RETRY_BASE_DELAY_SECONDS * attempt, 10)
            logger.warning(
                "消息 %s 发送失败（第 %s/%s 次）: %s；%s 秒后重试。",
                message_id,
                attempt,
                SEND_RETRY_MAX_ATTEMPTS,
                exc,
                sleep_seconds,
            )
            await asyncio.sleep(sleep_seconds)

    return False


def _compile_text_replacement_regex(patterns_text: str, logger) -> List[re.Pattern[str]]:
    compiled: List[re.Pattern[str]] = []
    if not patterns_text:
        return compiled

    for raw_pattern in str(patterns_text).splitlines():
        pattern = raw_pattern.strip()
        if not pattern:
            continue

        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            logger.warning("择词正则无效，已忽略：%s（%s）", pattern, exc)

    return compiled


def _apply_text_replacements(
    text: str,
    replacement_terms: List[str],
    replacement_regex_rules: List[re.Pattern[str]],
) -> tuple[str, int, int]:
    updated = str(text or "")
    term_hits = 0
    regex_hits = 0

    for term in replacement_terms:
        token = str(term or "")
        if not token:
            continue
        hit_count = updated.count(token)
        if hit_count > 0:
            updated = updated.replace(token, "")
            term_hits += hit_count

    for pattern in replacement_regex_rules:
        updated, hit_count = pattern.subn("", updated)
        regex_hits += int(hit_count)

    return updated, term_hits, regex_hits

async def _resolve_identifier(client: TelegramClient, identifier: str, logger) -> Optional[int]:
    entity_to_get = identifier
    if identifier.startswith("+"):
        entity_to_get = f"https://t.me/{identifier}"

    try:
        entity = await client.get_entity(entity_to_get)
        logger.info("标识符解析成功 '%s' -> %s", identifier, entity.id)
        return entity.id
    except Exception as exc:
        logger.warning("标识符解析失败 '%s': %s", identifier, exc)
        return None


async def resolve_identifiers_preview(config_store: ConfigStore, identifiers: List[str], logger) -> List[Dict[str, Any]]:
    raw_config = config_store.load_raw_config()
    api_id = raw_config.get("API_ID", "").strip()
    api_hash = raw_config.get("API_HASH", "").strip()

    if not api_id or not api_hash:
        raise ValueError("解析频道标识符前必须先配置 API_ID 和 API_HASH。")

    if not config_store.session_file.exists():
        raise FileNotFoundError("会话文件缺失，请先上传或创建 t2rss.session。")

    results: List[Dict[str, Any]] = []
    async with TelegramClient(str(config_store.session_base_path), int(api_id), api_hash) as client:
        if not await client.is_user_authorized():
            raise RuntimeError("当前会话未授权，请重新创建 Telegram 会话。")

        for identifier in identifiers:
            entity_to_get = identifier
            if identifier.startswith("+"):
                entity_to_get = f"https://t.me/{identifier}"

            try:
                entity = await client.get_entity(entity_to_get)
                latest_message_id = 0
                try:
                    latest_messages = await client.get_messages(entity, limit=1)
                    if latest_messages:
                        latest_message_id = int(getattr(latest_messages[0], "id", 0) or 0)
                except Exception as exc:
                    logger.warning("获取频道 '%s' 最新消息 ID 失败：%s", identifier, exc)
                results.append(
                    {
                        "identifier": identifier,
                        "ok": True,
                        "channel_id": entity.id,
                        "latest_message_id": latest_message_id,
                        "error": "",
                    }
                )
            except Exception as exc:
                logger.warning("预解析失败 '%s': %s", identifier, exc)
                error_text = str(exc)
                friendly_error = error_text
                is_invite_link = identifier.startswith("+") or "/+" in identifier or "joinchat" in identifier
                lowered = error_text.lower()
                if is_invite_link and (
                    "invitehash" in lowered
                    or "invite" in lowered
                    or "could not find" in lowered
                    or "no user has" in lowered
                    or "cannot find any entity" in lowered
                ):
                    friendly_error = (
                        "私有频道邀请链接无法直接解析：请先用当前会话账号点击该邀请链接加入频道，"
                        "加入后再回到本页重新解析。（原始错误：" + error_text + "）"
                    )
                results.append(
                    {
                        "identifier": identifier,
                        "ok": False,
                        "channel_id": "",
                        "latest_message_id": 0,
                        "error": friendly_error,
                    }
                )
    return results


async def _cleanup_and_get_historical_links(
    client: TelegramClient,
    config: ForwarderConfig,
    logger,
    stats: Dict[str, Any],
    test_mode_enabled: bool,
) -> Set[str]:
    if not config.deduplication_enabled:
        return set()

    logger.info("🧹 --- 开始预清理目标频道 ---")
    logger.info("🔍 正在加载目标频道最近的 %s 条消息进行预清理...", config.deduplication_cache_size)

    link_groups = collections.defaultdict(list)
    async for message in client.iter_messages(config.destination_channel, limit=config.deduplication_cache_size):
        if isinstance(message, MessageService):
            continue

        link = _extract_message_dedup_link(
            message,
            include_115=config.deduplication_115_enabled,
            include_baidu=config.deduplication_baidu_enabled,
            include_uc=config.deduplication_uc_enabled,
        )
        if link:
            link_groups[link].append(message)

    ids_to_delete: List[int] = []
    final_links: Set[str] = set()
    for link, messages in link_groups.items():
        messages.sort(key=lambda item: item.id, reverse=True)
        final_links.add(link)
        if len(messages) > 1:
            ids_to_delete.extend(msg.id for msg in messages[1:])

    if ids_to_delete:
        if test_mode_enabled:
            stats["destination_duplicates_detected"] += len(ids_to_delete)
            logger.info("🧪 测试模式：检测到目标频道可清理重复消息 %s 条（未执行删除）。", len(ids_to_delete))
        else:
            await client.delete_messages(config.destination_channel, ids_to_delete)
            stats["destination_duplicates_deleted"] += len(ids_to_delete)
            logger.info("✅ 目标频道预清理阶段删除重复消息 %s 条。", len(ids_to_delete))
    else:
        logger.info("ℹ️ 预清理完成，没有发现需要删除的重复消息。")

    logger.info("🧹 --- 目标频道预清理结束 ---")

    return final_links


async def _forward_single_message(
    client: TelegramClient,
    message,
    destination_channel: str,
    keyword_blacklist: List[str],
    user_blacklist: Set[int],
    download_dir: Path,
    logger,
    test_mode_enabled: bool,
    bot_link_cache: Dict[str, Optional[str]],
    text_replacement_terms: List[str],
    text_replacement_regex_rules: List[re.Pattern[str]],
    pre_resolved_url: Optional[str] = None,
    max_video_size_mb: int = 0,
    enabled_restricted_providers: Optional[Set[str]] = None,
) -> str:
    media_path = None
    try:
        if isinstance(message, MessageService):
            return "skipped_service"

        message_text = (
            getattr(message, "raw_text", None)
            or getattr(message, "message", None)
            or getattr(message, "text", None)
            or getattr(message, "caption", None)
        )
        original_text = message_text or ""
        outbound_text = message_text or ""
        full_text = (message_text or "").lower()
        original_entities = getattr(message, "entities", None)

        if keyword_blacklist and full_text:
            if any(keyword in full_text for keyword in keyword_blacklist):
                return "skipped_keyword"

        entities = getattr(message, "entities", None)
        if user_blacklist and entities:
            for entity in entities:
                if isinstance(entity, MessageEntityMentionName) and entity.user_id in user_blacklist:
                    return "skipped_user_blacklist"

        if not outbound_text and not message.media:
            return "skipped_no_content"

        # Restricted netdisk gate: skip only when EVERY netdisk link in the
        # message belongs to a disabled restricted provider. A message that also
        # carries an allowed link (quark/115/baidu/uc, or an enabled restricted
        # provider) still goes through, and link-free messages are unaffected.
        enabled_restricted = enabled_restricted_providers or set()
        disabled_restricted = {name for name, _ in RESTRICTED_PROVIDERS} - enabled_restricted
        hits = find_restricted_providers(message, disabled_restricted, pre_resolved_url)
        if hits and not has_allowed_netdisk_link(message, enabled_restricted, pre_resolved_url):
            logger.info(
                "⏭️ 跳过（受限网盘）：消息 %s 仅含 %s 链接。",
                getattr(message, "id", "unknown"),
                "、".join(RESTRICTED_PROVIDER_LABELS.get(name, name) for name in hits),
            )
            return "skipped_restricted_provider"

        if max_video_size_mb > 0:
            video_size = _video_media_size_bytes(message)
            if video_size is not None and video_size > max_video_size_mb * 1024 * 1024:
                logger.info(
                    "⏭️ 跳过（视频过大）：消息 %s，视频 %.1f MB 超过上限 %s MB。",
                    getattr(message, "id", "unknown"),
                    video_size / 1024 / 1024,
                    max_video_size_mb,
                )
                return "skipped_large_video"

        if test_mode_enabled:
            return "simulated_forwarded"

        resolved_url = pre_resolved_url
        if not resolved_url:
            resolved_url = await _resolve_link_via_bot(client, message, logger, bot_link_cache)
        if resolved_url and _has_quark_trigger_phrase(outbound_text):
            outbound_text = _replace_quark_trigger_segment(outbound_text, resolved_url)
        elif resolved_url:
            logger.info("消息 %s 获取到夸克链接，但正文无触发词，保持原文发送。", getattr(message, "id", "unknown"))

        if outbound_text:
            replaced_text, term_hits, regex_hits = _apply_text_replacements(
                outbound_text,
                text_replacement_terms,
                text_replacement_regex_rules,
            )
            outbound_text = replaced_text
            if term_hits > 0 or regex_hits > 0:
                logger.info(
                    "🧽 择词替换：消息 %s 命中关键词 %s 次，命中正则 %s 次。",
                    getattr(message, "id", "unknown"),
                    term_hits,
                    regex_hits,
                )
            outbound_text = outbound_text.strip()

        text_changed = outbound_text != original_text

        if text_changed and original_entities:
            skip_links = set(_extract_quark_trigger_bot_links(message))
            outbound_with_links = _materialize_text_url_entities(original_text, original_entities, skip_links, message)

            if resolved_url and _has_quark_trigger_phrase(outbound_with_links):
                outbound_with_links = _replace_quark_trigger_segment(outbound_with_links, resolved_url)

            outbound_with_links, _, _ = _apply_text_replacements(
                outbound_with_links,
                text_replacement_terms,
                text_replacement_regex_rules,
            )
            outbound_text = outbound_with_links.strip()

        if not outbound_text and not message.media:
            return "skipped_no_content"

        if message.media:
            download_dir.mkdir(parents=True, exist_ok=True)
            try:
                media_path = await asyncio.wait_for(
                    message.download_media(file=str(download_dir)),
                    timeout=MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "⏭️ 跳过（媒体下载超时）：消息 %s 超过 %s 秒未完成下载。",
                    getattr(message, "id", "unknown"),
                    MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
                )
                return "skipped_media_timeout"

        message_id = getattr(message, "id", "unknown")
        entities_for_send = None
        if not text_changed and outbound_text == original_text and original_entities:
            entities_for_send = list(original_entities)
        send_ok = await _send_message_with_retry(
            client=client,
            destination_channel=destination_channel,
            outbound_text=outbound_text,
            media_path=media_path,
            formatting_entities=entities_for_send,
            logger=logger,
            message_id=message_id,
        )
        if not send_ok:
            return "error"
        return "forwarded"
    except Exception:
        logger.exception("转发消息失败，消息 ID: %s", getattr(message, "id", "unknown"))
        return "error"
    finally:
        if media_path and os.path.exists(media_path):
            try:
                os.remove(media_path)
            except OSError:
                logger.warning("删除临时媒体文件失败: %s", media_path)


def _build_empty_stats() -> Dict[str, Any]:
    return {
        "cid_required": True,
        "source_channel_ids": [],
        "source_channel_count": 0,
        "per_channel_last_id_before": {},
        "per_channel_last_id_after": {},
        "per_channel_fetched": {},
        "fetch_failed_channels": {},
        "messages_collected_total": 0,
        "fetched_total": 0,
        "before_dedup_total": 0,
        "after_stage1_total": 0,
        "after_dedup_total": 0,
        "forwarded_total": 0,
        "simulated_forwarded_total": 0,
        "skipped_keyword": 0,
        "skipped_user_blacklist": 0,
        "skipped_service": 0,
        "skipped_no_content": 0,
        "skipped_large_video": 0,
        "skipped_media_timeout": 0,
        "skipped_restricted_provider": 0,
        "skipped_historical_link": 0,
        "skipped_intra_run_link": 0,
        "test_mode_enabled": False,
        "dedup_enabled": False,
        "dedup_115_enabled": True,
        "dedup_baidu_enabled": True,
        "dedup_uc_enabled": True,
        "max_video_size_mb": 0,
        "enabled_restricted_providers": [],
        "dedup_cache_size": 0,
        "destination_duplicates_detected": 0,
        "destination_duplicates_deleted": 0,
        "checkpoint_updated": False,
        "partial_checkpoint_updated": False,
        "timeout_seconds": 0,
        "error_total": 0,
    }


# Identifies THIS process instance. Written into the lock file so a lock left
# behind by a container that died mid-run can be told apart from a live one:
# PID alone is useless in a container, where the app is always PID 1.
_BOOT_ID = uuid.uuid4().hex


def _lock_payload() -> str:
    return f"{_BOOT_ID}:{os.getpid()}"


def _stale_lock_reason(lock_file: Path) -> Optional[str]:
    """Return why the lock is stale, or None if it belongs to this live process.

    Locks written by older builds hold a bare PID and carry no boot id; those
    are treated as stale, because the only process that could still legitimately
    hold one is this very process, which would have written the new format.
    """
    try:
        content = lock_file.read_text(encoding="utf-8").strip()
    except OSError:
        return "锁文件无法读取"

    if not content:
        return "锁文件为空"

    boot_id, _, pid_text = content.partition(":")
    if not pid_text:
        return f"旧格式锁（PID {boot_id}），来自已结束的进程"
    if boot_id != _BOOT_ID:
        return f"来自其他容器实例（{boot_id[:8]}…）"
    return None


def _clamp_checkpoints_below_failures(
    candidate_ids: Dict[int, int],
    failed_ids: Dict[int, int],
) -> Dict[int, int]:
    """Never let a checkpoint advance past a message that failed to send.

    `candidate_ids` is what we would like to store (max fetched, or the highest
    successfully handled id). For any channel that had a send failure, the
    checkpoint is pulled back to just below the OLDEST failed id, so that
    message is re-fetched and retried on the next run instead of being skipped
    forever. Channels without failures are unaffected.
    """
    if not failed_ids:
        return dict(candidate_ids)

    clamped: Dict[int, int] = {}
    for channel_id, last_id in candidate_ids.items():
        blocked_at = failed_ids.get(channel_id)
        if blocked_at is not None:
            last_id = min(last_id, blocked_at - 1)
        if last_id > 0:
            clamped[channel_id] = last_id
    return clamped


async def run_forwarder_once(
    config_store: ConfigStore,
    checkpoint_store: ChannelCheckpointStore,
    logger,
    stats_sink: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    stats = _build_empty_stats()
    if stats_sink is not None:
        # Share the live stats object so a caller that only sees TimeoutError
        # (asyncio.wait_for) can still record what the run actually did.
        stats_sink["stats"] = stats
    lock_created = False
    run_start_ts = time.time()
    test_mode_enabled = False
    source_channel_ids: List[int] = []
    latest_ids_map: Dict[int, int] = {}
    forwarded_ids_map: Dict[int, int] = {}
    failed_ids_map: Dict[int, int] = {}
    channel_by_message_obj: Dict[int, int] = {}
    bot_link_cache: Dict[str, Optional[str]] = {}
    pre_resolved_url_by_message_obj: Dict[int, str] = {}
    text_replacement_regex_rules: List[re.Pattern[str]] = []

    try:
        config = config_store.build_forwarder_config()
        panel_settings = config_store.build_panel_settings()
        test_mode_enabled = panel_settings.test_mode_enabled

        stats["test_mode_enabled"] = test_mode_enabled
        stats["dedup_enabled"] = config.deduplication_enabled
        stats["dedup_115_enabled"] = config.deduplication_115_enabled
        stats["dedup_baidu_enabled"] = config.deduplication_baidu_enabled
        stats["dedup_uc_enabled"] = config.deduplication_uc_enabled
        stats["max_video_size_mb"] = config.max_video_size_mb
        stats["enabled_restricted_providers"] = sorted(config.enabled_restricted_providers)
        stats["dedup_cache_size"] = config.deduplication_cache_size
        text_replacement_regex_rules = _compile_text_replacement_regex(config.text_replacement_regex, logger)

        logger.info(
            "🧹 文本清洗策略：择词 %s 条，正则 %s 条。",
            len(config.text_replacement_terms),
            len(text_replacement_regex_rules),
        )

        logger.info("🚀 程序开始运行...")
        logger.info("🧭 开始执行转发任务，测试模式: %s", "开启" if test_mode_enabled else "关闭")
        if test_mode_enabled:
            logger.info("🧪 测试模式开启：仅测试，不真实转发内容，不更新断点。")

        if not all([config.api_id, config.api_hash, config.destination_channel]):
            raise ValueError("API_ID、API_HASH 和 DESTINATION_CHANNEL 为必填项。")

        source_channel_ids: List[int] = []
        if config.channel_sources:
            for item in config.channel_sources:
                cid = item.get("cid")
                enabled = bool(item.get("enabled", True))
                if not enabled:
                    continue
                if isinstance(cid, int):
                    source_channel_ids.append(cid)

        if not source_channel_ids:
            source_channel_ids = list(config.channel_ids)

        if not source_channel_ids:
            raise ValueError("新增频道转发前必须先解析 CID 并写入来源列表（至少启用一个来源）。")

        if not config_store.session_file.exists():
            raise FileNotFoundError("会话文件缺失，请先上传或创建 t2rss.session。")

        source_channel_ids = sorted(set(source_channel_ids))
        stats["source_channel_ids"] = source_channel_ids
        stats["source_channel_count"] = len(source_channel_ids)
        logger.info("📡 程序将从以下源频道ID进行转发: %s", source_channel_ids)

        # The lock records this container's boot id, not just a PID. PID 1 is
        # reused by every container start, so a lock left behind by a container
        # that died mid-run would otherwise look permanently "held" and wedge
        # the forwarder forever (every subsequent run returns "skipped").
        # A lock whose boot id differs from ours is stale by definition.
        if config_store.lock_file.exists():
            stale_reason = _stale_lock_reason(config_store.lock_file)
            if stale_reason is None:
                stats["duration_seconds"] = round(time.time() - run_start_ts, 2)
                return {
                    "status": "skipped",
                    "message": "检测到锁文件，可能已有任务正在运行。",
                    "stats": stats,
                }
            logger.warning("🔓 检测到陈旧锁文件（%s），已自动清除并继续。", stale_reason)
            try:
                config_store.lock_file.unlink(missing_ok=True)
            except OSError:
                logger.warning("移除陈旧锁文件失败: %s", config_store.lock_file)

        config_store.lock_file.write_text(_lock_payload(), encoding="utf-8")
        lock_created = True

        async with TelegramClient(str(config_store.session_base_path), int(config.api_id), config.api_hash) as client:
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram 会话未授权，请重新创建 t2rss.session。")

            historical_links = await _cleanup_and_get_historical_links(
                client,
                config,
                logger,
                stats,
                test_mode_enabled,
            )

            all_new_messages = []

            for channel_id in source_channel_ids:
                last_id = checkpoint_store.get_last_id(channel_id)
                stats["per_channel_last_id_before"][str(channel_id)] = last_id

                logger.info("📥 正在从频道 %s 收集自 ID %s 以来的新消息...", channel_id, last_id + 1)

                # One unreachable source (private/banned/deleted, or a transient
                # network error) must not abort the whole cycle: without this
                # guard the exception propagates out of the loop and every
                # channel after it is never polled, so nothing is forwarded.
                try:
                    channel_messages = [msg async for msg in client.iter_messages(channel_id, min_id=last_id)]
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    stats["per_channel_fetched"][str(channel_id)] = 0
                    stats["fetch_failed_channels"][str(channel_id)] = str(exc)
                    logger.warning("⚠️ 频道 %s 拉取失败，已跳过该源：%s", channel_id, exc)
                    continue

                fetched_count = len(channel_messages)
                stats["per_channel_fetched"][str(channel_id)] = fetched_count
                stats["fetched_total"] += fetched_count
                logger.info("✅ 频道 %s 收集完成，新消息 %s 条（当前断点 last_id=%s）", channel_id, fetched_count, last_id)

                if channel_messages:
                    all_new_messages.extend(channel_messages)
                    latest_ids_map[channel_id] = max(msg.id for msg in channel_messages)
                    for msg in channel_messages:
                        channel_by_message_obj[id(msg)] = channel_id

            stats["messages_collected_total"] = len(all_new_messages)
            stats["before_dedup_total"] = len(all_new_messages)

            if not all_new_messages:
                for channel_id in source_channel_ids:
                    old_last_id = stats["per_channel_last_id_before"].get(str(channel_id), 0)
                    stats["per_channel_last_id_after"][str(channel_id)] = old_last_id
                stats["duration_seconds"] = round(time.time() - run_start_ts, 2)
                logger.info("ℹ️ 所有源频道都没有找到新消息。程序退出。")
                return {
                    "status": "success",
                    "message": "源频道暂无新消息。",
                    "stats": stats,
                }

            all_new_messages.sort(key=lambda item: item.date)
            final_messages = all_new_messages

            logger.info("📊 从所有频道共收集到 %s 条新消息，开始统一过滤...", len(all_new_messages))

            if config.deduplication_enabled and not test_mode_enabled:
                resolved_for_dedup = 0
                for message in all_new_messages:
                    resolved_url = await _resolve_link_via_bot(client, message, logger, bot_link_cache)
                    if not resolved_url:
                        continue
                    pre_resolved_url_by_message_obj[id(message)] = resolved_url
                    resolved_for_dedup += 1

                if resolved_for_dedup > 0:
                    logger.info("  - 预解析完成：%s 条消息通过 Bot 拿到夸克链接并纳入去重。", resolved_for_dedup)

            if config.deduplication_enabled:
                logger.info("  - 阶段一：处理本次运行内的重复链接...")
                link_map = {}
                messages_without_link_stage1 = []

                for message in all_new_messages:
                    if isinstance(message, MessageService):
                        continue

                    pre_resolved = pre_resolved_url_by_message_obj.get(id(message))
                    link = _extract_message_dedup_link(
                        message,
                        pre_resolved,
                        config.deduplication_115_enabled,
                        config.deduplication_baidu_enabled,
                        config.deduplication_uc_enabled,
                    )
                    if not link:
                        messages_without_link_stage1.append(message)
                        continue

                    existing = link_map.get(link)
                    if existing is None:
                        link_map[link] = message
                    elif message.id > existing.id:
                        link_map[link] = message
                        stats["skipped_intra_run_link"] += 1
                    else:
                        stats["skipped_intra_run_link"] += 1

                messages_after_stage1 = list(link_map.values()) + messages_without_link_stage1
                messages_after_stage1.sort(key=lambda item: item.date)
                stats["after_stage1_total"] = len(messages_after_stage1)
                logger.info("  - 阶段一后剩余 %s 条消息。", len(messages_after_stage1))

                logger.info("  - 阶段二：与目标频道历史链接比对...")
                messages_after_stage2 = []
                for message in messages_after_stage1:
                    if isinstance(message, MessageService):
                        continue

                    pre_resolved = pre_resolved_url_by_message_obj.get(id(message))
                    link = _extract_message_dedup_link(
                        message,
                        pre_resolved,
                        config.deduplication_115_enabled,
                        config.deduplication_baidu_enabled,
                        config.deduplication_uc_enabled,
                    )
                    if link and link in historical_links:
                        stats["skipped_historical_link"] += 1
                    else:
                        messages_after_stage2.append(message)

                final_messages = messages_after_stage2
                logger.info("  - 阶段二后剩余 %s 条消息。", len(final_messages))
            else:
                stats["after_stage1_total"] = len(all_new_messages)

            stats["after_dedup_total"] = len(final_messages)
            logger.info(
                "消息统计：抓取=%s，去重后=%s，站内去重跳过=%s，历史去重跳过=%s",
                stats["fetched_total"],
                stats["after_dedup_total"],
                stats["skipped_intra_run_link"],
                stats["skipped_historical_link"],
            )
            logger.info("✅ 过滤完成，最终有 %s 条消息准备处理。", len(final_messages))

            processed_count = 0
            for message in final_messages:
                source_channel_id = channel_by_message_obj.get(id(message), "unknown")
                message_id = getattr(message, "id", "unknown")
                reason = await _forward_single_message(
                    client=client,
                    message=message,
                    destination_channel=config.destination_channel,
                    keyword_blacklist=config.keyword_blacklist,
                    user_blacklist=config.user_id_blacklist,
                    download_dir=config_store.download_dir,
                    logger=logger,
                    test_mode_enabled=test_mode_enabled,
                    bot_link_cache=bot_link_cache,
                    text_replacement_terms=config.text_replacement_terms,
                    text_replacement_regex_rules=text_replacement_regex_rules,
                    pre_resolved_url=pre_resolved_url_by_message_obj.get(id(message)),
                    max_video_size_mb=config.max_video_size_mb,
                    enabled_restricted_providers=config.enabled_restricted_providers,
                )

                if reason == "forwarded":
                    stats["forwarded_total"] += 1
                    logger.info("✅ 发送成功：源频道 %s，消息 %s", source_channel_id, message_id)
                elif reason == "simulated_forwarded":
                    stats["simulated_forwarded_total"] += 1
                elif reason == "skipped_keyword":
                    stats["skipped_keyword"] += 1
                    logger.info("⏭️ 跳过（关键词黑名单）：源频道 %s，消息 %s", source_channel_id, message_id)
                elif reason == "skipped_user_blacklist":
                    stats["skipped_user_blacklist"] += 1
                    logger.info("⏭️ 跳过（用户黑名单）：源频道 %s，消息 %s", source_channel_id, message_id)
                elif reason == "skipped_service":
                    stats["skipped_service"] += 1
                    logger.info("⏭️ 跳过（服务消息）：源频道 %s，消息 %s", source_channel_id, message_id)
                elif reason == "skipped_no_content":
                    stats["skipped_no_content"] += 1
                    logger.info("⏭️ 跳过（空内容）：源频道 %s，消息 %s", source_channel_id, message_id)
                elif reason == "skipped_large_video":
                    stats["skipped_large_video"] += 1
                elif reason == "skipped_media_timeout":
                    stats["skipped_media_timeout"] += 1
                elif reason == "skipped_restricted_provider":
                    stats["skipped_restricted_provider"] += 1
                elif reason == "error":
                    stats["error_total"] += 1
                    logger.error("❌ 发送失败：源频道 %s，消息 %s", source_channel_id, message_id)

                # A message that was consciously dealt with — forwarded OR
                # deliberately skipped — counts as progress. Only genuine send
                # failures hold position, and only for their own channel: a
                # later success must not drag the checkpoint past an earlier
                # failure, so track the lowest failed id per channel and clamp
                # below it when writing.
                if not test_mode_enabled and isinstance(source_channel_id, int):
                    if reason == "error":
                        current_block = failed_ids_map.get(source_channel_id)
                        if current_block is None or message.id < current_block:
                            failed_ids_map[source_channel_id] = message.id
                    elif message.id > forwarded_ids_map.get(source_channel_id, 0):
                        forwarded_ids_map[source_channel_id] = message.id

                processed_count += 1
                if processed_count % 500 == 0 or processed_count == len(final_messages):
                    logger.info("⏳ 处理进度：%s/%s", processed_count, len(final_messages))

                if (
                    not test_mode_enabled
                    and reason in {"forwarded", "error"}
                    and processed_count < len(final_messages)
                ):
                    logger.info("⏱️ 发送间隔等待 %s 秒，避免风控。", SEND_INTERVAL_SECONDS)
                    await asyncio.sleep(SEND_INTERVAL_SECONDS)

            if test_mode_enabled:
                effective_ids_map: Dict[int, int] = {}
                stats["checkpoint_updated"] = False
                logger.info("🧪 测试模式开启：已跳过真实发送后的断点更新。")
            else:
                effective_ids_map = _clamp_checkpoints_below_failures(latest_ids_map, failed_ids_map)
                checkpoint_store.bulk_update(effective_ids_map)
                stats["checkpoint_updated"] = True
                if failed_ids_map:
                    stats["checkpoint_held_back_channels"] = {
                        str(cid): mid for cid, mid in sorted(failed_ids_map.items())
                    }
                    logger.warning(
                        "⚠️ %s 个来源存在发送失败，其断点已保持在失败消息之前以便重试。",
                        len(failed_ids_map),
                    )
                logger.info("💾 --- 更新所有频道的 last_id 到数据库 ---")
                logger.info("✅ 断点已更新到数据库。")

            for channel_id in source_channel_ids:
                old_last_id = int(stats["per_channel_last_id_before"].get(str(channel_id), 0))
                if test_mode_enabled:
                    new_last_id = old_last_id
                else:
                    new_last_id = int(effective_ids_map.get(channel_id, old_last_id))
                stats["per_channel_last_id_after"][str(channel_id)] = new_last_id

            stats["duration_seconds"] = round(time.time() - run_start_ts, 2)

            if test_mode_enabled:
                summary = (
                    f"测试模式执行完成：源频道 {stats['source_channel_count']} 个，"
                    f"抓取 {stats['fetched_total']} 条，"
                    f"去重后可转发 {stats['after_dedup_total']} 条，"
                    f"模拟转发 {stats['simulated_forwarded_total']} 条，"
                    f"未真实发送且未更新断点，"
                    f"耗时 {stats['duration_seconds']} 秒。"
                )
            else:
                summary = (
                    f"执行完成：源频道 {stats['source_channel_count']} 个，"
                    f"抓取 {stats['fetched_total']} 条，"
                    f"去重后待转发 {stats['after_dedup_total']} 条，"
                    f"实际转发 {stats['forwarded_total']} 条，"
                    f"错误 {stats['error_total']} 条，"
                    f"耗时 {stats['duration_seconds']} 秒。"
                )
            logger.info(
                "📦 最终统计：待处理=%s，成功发送=%s，失败=%s，"
                "跳过关键词=%s，跳过用户=%s，跳过服务=%s，跳过空内容=%s，"
                "跳过大视频=%s，跳过媒体超时=%s，跳过受限网盘=%s。",
                stats["after_dedup_total"],
                stats["forwarded_total"],
                stats["error_total"],
                stats["skipped_keyword"],
                stats["skipped_user_blacklist"],
                stats["skipped_service"],
                stats["skipped_no_content"],
                stats["skipped_large_video"],
                stats["skipped_media_timeout"],
                stats["skipped_restricted_provider"],
            )
            logger.info("✅ 所有任务已完成。")
            return {
                "status": "success",
                "message": summary,
                "stats": stats,
            }

    except asyncio.CancelledError:
        if not test_mode_enabled and forwarded_ids_map:
            partial_ids_map = _clamp_checkpoints_below_failures(forwarded_ids_map, failed_ids_map)
            checkpoint_store.bulk_update(partial_ids_map)
            stats["checkpoint_updated"] = True
            stats["partial_checkpoint_updated"] = True
            logger.warning("⚠️ 任务中止：已将断点更新到已转发的最后消息 ID。")

            for channel_id in source_channel_ids:
                old_last_id = int(stats["per_channel_last_id_before"].get(str(channel_id), 0))
                new_last_id = int(partial_ids_map.get(channel_id, old_last_id))
                stats["per_channel_last_id_after"][str(channel_id)] = new_last_id

        stats["duration_seconds"] = round(time.time() - run_start_ts, 2)
        raise

    except Exception as exc:
        if not test_mode_enabled and forwarded_ids_map:
            partial_ids_map = _clamp_checkpoints_below_failures(forwarded_ids_map, failed_ids_map)
            checkpoint_store.bulk_update(partial_ids_map)
            stats["checkpoint_updated"] = True
            stats["partial_checkpoint_updated"] = True
            logger.warning("⚠️ 任务异常中断：已将断点更新到已转发的最后消息 ID。")

            for channel_id in source_channel_ids:
                old_last_id = int(stats["per_channel_last_id_before"].get(str(channel_id), 0))
                new_last_id = int(partial_ids_map.get(channel_id, old_last_id))
                stats["per_channel_last_id_after"][str(channel_id)] = new_last_id

        logger.exception("❌ 转发任务执行失败: %s", exc)
        stats["error_total"] += 1
        stats["duration_seconds"] = round(time.time() - run_start_ts, 2)
        return {
            "status": "error",
            "message": str(exc),
            "stats": stats,
        }
    finally:
        if lock_created and config_store.lock_file.exists():
            try:
                config_store.lock_file.unlink()
            except OSError:
                logger.warning("移除锁文件失败: %s", config_store.lock_file)


class ForwarderRunner:
    def __init__(self, config_store: ConfigStore, checkpoint_store: ChannelCheckpointStore, history_store, logger):
        self.config_store = config_store
        self.checkpoint_store = checkpoint_store
        self.history_store = history_store
        self.logger = logger
        self._current_task: Optional[Any] = None
        self._auto_task: Optional[Any] = None
        self._stop_event = asyncio.Event()
        self._manual_stop_requested = False
        self._current_started_at: Optional[str] = None
        self.last_result: Optional[Dict[str, Any]] = None

    @property
    def is_running(self) -> bool:
        return self._current_task is not None and not self._current_task.done()

    def status_payload(self) -> Dict[str, Any]:
        return {
            "is_running": self.is_running,
            "current_started_at": self._current_started_at,
            "last_result": self.last_result,
        }

    async def trigger(self, trigger: str = "manual") -> bool:
        if self.is_running:
            return False

        self._manual_stop_requested = False
        self._current_task = asyncio.create_task(self._run_job(trigger))
        return True

    async def abort_current_run(self) -> bool:
        if not self.is_running:
            return False

        if self._current_task is None:
            return False

        self._manual_stop_requested = True
        self._current_task.cancel()
        try:
            await self._current_task
        except asyncio.CancelledError:
            pass
        return True

    async def _run_job(self, trigger: str) -> None:
        started_at = now_shanghai_iso()
        self._current_started_at = started_at
        timeout_seconds = 600
        stats_sink: Dict[str, Any] = {}

        try:
            panel_settings = self.config_store.build_panel_settings()
            timeout_seconds = max(60, panel_settings.total_timeout_seconds)

            result = await asyncio.wait_for(
                run_forwarder_once(
                    self.config_store,
                    self.checkpoint_store,
                    self.logger,
                    stats_sink=stats_sink,
                ),
                timeout=timeout_seconds,
            )
            finished_at = now_shanghai_iso()

            payload = {
                "started_at": started_at,
                "finished_at": finished_at,
                "trigger": trigger,
                "status": result.get("status", "error"),
                "message": result.get("message", ""),
                "stats": result.get("stats", {}),
            }

            self.last_result = payload
            self.history_store.add_record(payload)

            if payload["status"] == "success":
                self.logger.info("转发任务执行成功。")
            elif payload["status"] == "skipped":
                self.logger.info("转发任务已跳过: %s", payload["message"])
            else:
                self.logger.error("转发任务失败: %s", payload["message"])
        except asyncio.TimeoutError:
            finished_at = now_shanghai_iso()
            message = f"⏱️ 转发任务总超时: {timeout_seconds}s，已自动中止。"
            # The inner run received CancelledError from wait_for and already ran
            # its partial-checkpoint handler; surface its real stats instead of
            # discarding them, so run_history stays diagnosable.
            timeout_stats = dict(stats_sink.get("stats") or {})
            timeout_stats["timeout_seconds"] = timeout_seconds
            timeout_stats["manual_stop_requested"] = False
            payload = {
                "started_at": started_at,
                "finished_at": finished_at,
                "trigger": trigger,
                "status": "timeout",
                "message": message,
                "stats": timeout_stats,
            }
            self.last_result = payload
            self.history_store.add_record(payload)
            self.logger.error(message)
        except asyncio.CancelledError:
            finished_at = now_shanghai_iso()
            message = "🛑 转发任务已被强制中止。"
            cancel_stats = dict(stats_sink.get("stats") or {})
            cancel_stats["cancelled"] = True
            cancel_stats["manual_stop_requested"] = self._manual_stop_requested
            payload = {
                "started_at": started_at,
                "finished_at": finished_at,
                "trigger": trigger,
                "status": "cancelled",
                "message": message,
                "stats": cancel_stats,
            }
            self.last_result = payload
            self.history_store.add_record(payload)
            self.logger.warning(message)
        finally:
            self._current_task = None
            self._current_started_at = None
            self._manual_stop_requested = False

    async def start(self) -> None:
        if self._auto_task is None or self._auto_task.done():
            self._stop_event.clear()
            self._auto_task = asyncio.create_task(self._auto_loop())

    async def stop(self) -> None:
        self._stop_event.set()

        if self._auto_task and not self._auto_task.done():
            self._auto_task.cancel()
            try:
                await self._auto_task
            except asyncio.CancelledError:
                pass

        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            try:
                await self._current_task
            except asyncio.CancelledError:
                pass

    async def _auto_loop(self) -> None:
        while not self._stop_event.is_set():
            interval_minutes = 15
            try:
                panel_settings = self.config_store.build_panel_settings()
                interval_minutes = max(1, panel_settings.auto_run_interval_minutes)

                if panel_settings.auto_run_enabled and not self.is_running:
                    started = await self.trigger(trigger="auto")
                    if started:
                        self.logger.info("自动转发任务已启动。")

            except Exception as exc:
                self.logger.exception("自动运行循环异常: %s", exc)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_minutes * 60)
            except asyncio.TimeoutError:
                continue
