import asyncio
import collections
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any, Dict, List, Optional, Set

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageEntityMentionName, MessageEntityTextUrl, MessageService

from .checkpoint_store import ChannelCheckpointStore
from .config_store import ConfigStore, ForwarderConfig
from .time_utils import now_shanghai_iso


QUARK_LINK_PATTERN = re.compile(r"https://pan\.quark\.cn/s/[a-zA-Z0-9]+")
URL_PATTERN = re.compile(r'https?://[^\s<>"]+')
BOT_TRIGGER_PHRASE = "点击获取夸克链接"
SEND_RETRY_MAX_ATTEMPTS = 3
SEND_RETRY_BASE_DELAY_SECONDS = 2
SEND_INTERVAL_SECONDS = 3


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


def extract_quark_link(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    match = QUARK_LINK_PATTERN.search(text)
    return match.group(0) if match else None


def _extract_message_quark_link(message, resolved_url: Optional[str] = None) -> Optional[str]:
    message_text = getattr(message, "text", None) or getattr(message, "caption", None)
    link = extract_quark_link(message_text)
    if link:
        return link
    if resolved_url:
        return extract_quark_link(resolved_url)
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
                results.append(
                    {
                        "identifier": identifier,
                        "ok": True,
                        "channel_id": entity.id,
                        "error": "",
                    }
                )
            except Exception as exc:
                logger.warning("预解析失败 '%s': %s", identifier, exc)
                results.append(
                    {
                        "identifier": identifier,
                        "ok": False,
                        "channel_id": "",
                        "error": str(exc),
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

        message_text = getattr(message, "text", None) or getattr(message, "caption", None)
        if not message_text:
            continue

        link = extract_quark_link(message_text)
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
            media_path = await message.download_media(file=str(download_dir))

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
        "skipped_historical_link": 0,
        "skipped_intra_run_link": 0,
        "test_mode_enabled": False,
        "dedup_enabled": False,
        "dedup_cache_size": 0,
        "destination_duplicates_detected": 0,
        "destination_duplicates_deleted": 0,
        "checkpoint_updated": False,
        "partial_checkpoint_updated": False,
        "timeout_seconds": 0,
        "error_total": 0,
    }


async def run_forwarder_once(
    config_store: ConfigStore,
    checkpoint_store: ChannelCheckpointStore,
    logger,
) -> Dict[str, Any]:
    stats = _build_empty_stats()
    lock_created = False
    run_start_ts = time.time()
    test_mode_enabled = False
    source_channel_ids: List[int] = []
    latest_ids_map: Dict[int, int] = {}
    forwarded_ids_map: Dict[int, int] = {}
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

        if config_store.lock_file.exists():
            stats["duration_seconds"] = round(time.time() - run_start_ts, 2)
            return {
                "status": "skipped",
                "message": "检测到锁文件，可能已有任务正在运行。",
                "stats": stats,
            }

        config_store.lock_file.write_text(str(os.getpid()), encoding="utf-8")
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

                channel_messages = [msg async for msg in client.iter_messages(channel_id, min_id=last_id)]
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

                    message_text = getattr(message, "text", None) or getattr(message, "caption", None)
                    if not message_text:
                        pre_resolved = pre_resolved_url_by_message_obj.get(id(message))
                        link = _extract_message_quark_link(message, pre_resolved)
                        if not link:
                            messages_without_link_stage1.append(message)
                            continue
                    else:
                        pre_resolved = pre_resolved_url_by_message_obj.get(id(message))
                        link = _extract_message_quark_link(message, pre_resolved)
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
                    link = _extract_message_quark_link(message, pre_resolved)
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
                )

                if reason == "forwarded":
                    stats["forwarded_total"] += 1
                    logger.info("✅ 发送成功：源频道 %s，消息 %s", source_channel_id, message_id)
                    if isinstance(source_channel_id, int):
                        current_forwarded = forwarded_ids_map.get(source_channel_id, 0)
                        if message.id > current_forwarded:
                            forwarded_ids_map[source_channel_id] = message.id
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
                elif reason == "error":
                    stats["error_total"] += 1
                    logger.error("❌ 发送失败：源频道 %s，消息 %s", source_channel_id, message_id)

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
                stats["checkpoint_updated"] = False
                logger.info("🧪 测试模式开启：已跳过真实发送后的断点更新。")
            else:
                checkpoint_store.bulk_update(latest_ids_map)
                stats["checkpoint_updated"] = True
                logger.info("💾 --- 更新所有频道的 last_id 到数据库 ---")
                logger.info("✅ 断点已更新到数据库。")

            for channel_id in source_channel_ids:
                old_last_id = int(stats["per_channel_last_id_before"].get(str(channel_id), 0))
                if test_mode_enabled:
                    new_last_id = old_last_id
                else:
                    new_last_id = int(latest_ids_map.get(channel_id, old_last_id))
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
                "跳过关键词=%s，跳过用户=%s，跳过服务=%s，跳过空内容=%s。",
                stats["after_dedup_total"],
                stats["forwarded_total"],
                stats["error_total"],
                stats["skipped_keyword"],
                stats["skipped_user_blacklist"],
                stats["skipped_service"],
                stats["skipped_no_content"],
            )
            logger.info("✅ 所有任务已完成。")
            return {
                "status": "success",
                "message": summary,
                "stats": stats,
            }

    except asyncio.CancelledError:
        if not test_mode_enabled and forwarded_ids_map:
            checkpoint_store.bulk_update(forwarded_ids_map)
            stats["checkpoint_updated"] = True
            stats["partial_checkpoint_updated"] = True
            logger.warning("⚠️ 任务中止：已将断点更新到已转发的最后消息 ID。")

            for channel_id in source_channel_ids:
                old_last_id = int(stats["per_channel_last_id_before"].get(str(channel_id), 0))
                new_last_id = int(forwarded_ids_map.get(channel_id, old_last_id))
                stats["per_channel_last_id_after"][str(channel_id)] = new_last_id

        stats["duration_seconds"] = round(time.time() - run_start_ts, 2)
        raise

    except Exception as exc:
        if not test_mode_enabled and forwarded_ids_map:
            checkpoint_store.bulk_update(forwarded_ids_map)
            stats["checkpoint_updated"] = True
            stats["partial_checkpoint_updated"] = True
            logger.warning("⚠️ 任务异常中断：已将断点更新到已转发的最后消息 ID。")

            for channel_id in source_channel_ids:
                old_last_id = int(stats["per_channel_last_id_before"].get(str(channel_id), 0))
                new_last_id = int(forwarded_ids_map.get(channel_id, old_last_id))
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

        try:
            panel_settings = self.config_store.build_panel_settings()
            timeout_seconds = max(60, panel_settings.total_timeout_seconds)

            result = await asyncio.wait_for(
                run_forwarder_once(self.config_store, self.checkpoint_store, self.logger),
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
            payload = {
                "started_at": started_at,
                "finished_at": finished_at,
                "trigger": trigger,
                "status": "timeout",
                "message": message,
                "stats": {
                    "timeout_seconds": timeout_seconds,
                    "manual_stop_requested": False,
                },
            }
            self.last_result = payload
            self.history_store.add_record(payload)
            self.logger.error(message)
        except asyncio.CancelledError:
            finished_at = now_shanghai_iso()
            message = "🛑 转发任务已被强制中止。"
            payload = {
                "started_at": started_at,
                "finished_at": finished_at,
                "trigger": trigger,
                "status": "cancelled",
                "message": message,
                "stats": {
                    "cancelled": True,
                    "manual_stop_requested": self._manual_stop_requested,
                },
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
