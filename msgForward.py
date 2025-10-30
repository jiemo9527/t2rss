import os
import asyncio
import sys
import time
import re
import collections
from dotenv import load_dotenv
from telethon.sync import TelegramClient
# --- 已修正：导入正确的类名 ---
from telethon.tl.types import MessageService, MessageEntityMentionName

# 从 .env 文件加载环境变量
load_dotenv()

# --- 路径定义 ---
SESSION_NAME = 'session_name'
CACHE_DIR = 'cache'
LAST_ID_DIR = os.path.join(CACHE_DIR, 'last_ids')
DOWNLOADS_DIR = os.path.join(CACHE_DIR, 'downloads')
LOCK_FILE = os.path.join(CACHE_DIR, 'forwarder.lock')


# =================================================================
#  辅助函数 (ID获取、缓存读写、链接提取)
# =================================================================
async def get_channel_id_by_identifier(client, identifier):
    """（异步）通过标识符获取单个频道的ID。"""
    entity_to_get = identifier
    if identifier.startswith('+'):
        entity_to_get = f"https://t.me/{identifier}"
    try:
        print(f"正在解析: {entity_to_get}")
        entity = await client.get_entity(entity_to_get)
        print(f"✅ 标识符 '{identifier}' -> ID: {entity.id}")
        return entity.id
    except Exception as e:
        print(f"❌ 解析标识符 '{identifier}' 时发生错误: {e}")
        return None


async def get_channel_ids_from_identifiers(client, identifiers):
    """（异步）接收一个标识符列表，返回所有有效频道的ID列表。"""
    print("\n--- 开始批量获取频道ID ---")
    tasks = [get_channel_id_by_identifier(client, identifier) for identifier in identifiers]
    results = await asyncio.gather(*tasks)
    valid_ids = [res for res in results if res is not None]
    print(f"--- 批量获取完成，成功找到 {len(valid_ids)} 个有效ID ---\n")
    return valid_ids


def get_last_id(channel_id):
    """为指定频道获取最后转发的消息ID"""
    file_path = os.path.join(LAST_ID_DIR, f"{channel_id}.txt")
    if os.path.exists(file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            try:
                return int(f.read().strip())
            except (ValueError, IndexError):
                return 0
    return 0


def save_last_id(channel_id, message_id):
    """为指定频道保存最后转发的消息ID"""
    os.makedirs(LAST_ID_DIR, exist_ok=True)
    file_path = os.path.join(LAST_ID_DIR, f"{channel_id}.txt")
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(str(message_id))


def extract_quark_link(text):
    """从文本中提取第一个夸克网盘链接。"""
    if not text:
        return None
    match = re.search(r"https://pan\.quark\.cn/s/[a-zA-Z0-9]+", text)
    return match.group(0) if match else None


# =================================================================
#  核心转发与清理功能函数
# =================================================================
async def forward_message_task(client, message, destination_channel, blacklist, user_blacklist):
    """处理单条消息的转发任务（包含关键词和用户ID过滤）。"""
    media_path = None
    try:
        if isinstance(message, MessageService):
            print(f"🤫 消息 ID {message.id} 是服务消息，已跳过。")
            return None

        # [修复] 使用 getattr 安全地获取文本和标题
        message_text = getattr(message, 'text', None)
        message_caption = getattr(message, 'caption', None)
        full_text = (message_text or message_caption or "").lower()

        # 1. 关键词过滤
        if blacklist and full_text:
            if any(keyword in full_text for keyword in blacklist):
                print(f"🤫 消息 ID {message.id} (关键词过滤)，已跳过。")
                return None

        # --- 2. 新增：用户ID黑名单过滤 ---
        # .entities 会自动返回 message.entities (文本) 或 message.caption_entities (媒体标题)
        if user_blacklist and message.entities:
            for entity in message.entities:
                if isinstance(entity, MessageEntityMentionName):

                    if entity.user_id in user_blacklist:
                        print(f"🤫 消息 ID {message.id} (用户ID {entity.user_id} 在黑名单中)，已跳过。")
                        return None
        # --- 过滤结束 ---

        # 确保有内容可转发 (使用原始的 text 和 media 判断)
        if not message.text and not message.media: return None

        print(f"➡️ 正在转发来自频道 {message.chat_id} 的消息 ID: {message.id}")
        if message.media:
            os.makedirs(DOWNLOADS_DIR, exist_ok=True)
            media_path = await message.download_media(file=DOWNLOADS_DIR)

        # 发送时，依然使用原始的 message.text，因为它包含了媒体的标题
        await client.send_message(destination_channel, message.text, file=media_path)

        print(f"✅ 已成功转发消息 ID {message.id} 到 {destination_channel}")
        return message.id
    except Exception as e:
        print(f"❌ 转发消息 ID {message.id} 时出错: {e}")
        return None
    finally:
        if media_path and os.path.exists(media_path):
            os.remove(media_path)


async def cleanup_and_get_historical_links(client, config):
    """【新功能】清理目标频道中的重复链接消息，并返回清理后的链接集合。"""
    if not config['dedup_enabled']:
        return set()

    print("\n--- 开始预清理目标频道 ---")
    destination_channel = config['destination_channel']
    limit = config['dedup_cache_size']

    try:
        print(f"正在加载目标频道最近的 {limit} 条消息进行预清理...")
        link_groups = collections.defaultdict(list)

        async for message in client.iter_messages(destination_channel, limit=limit):
            if isinstance(message, MessageService):
                continue

            # [修复] 使用 getattr 安全地获取文本和标题
            message_text = getattr(message, 'text', None) or getattr(message, 'caption', None)
            if not message_text:
                continue

            link = extract_quark_link(message_text)
            if link:
                link_groups[link].append(message)
                link_groups[link].sort(key=lambda m: m.id, reverse=True)

        ids_to_delete = []
        final_links = set()
        for link, messages in link_groups.items():
            final_links.add(link)
            if len(messages) > 1:
                messages_to_delete = messages[1:]
                delete_ids = [msg.id for msg in messages_to_delete]
                ids_to_delete.extend(delete_ids)
                print(f"  - 发现重复链接: {link}")
                print(f"  -   保留最新消息 ID: {messages[0].id}")
                print(f"  -   准备删除旧消息: {delete_ids}")

        if ids_to_delete:
            await client.delete_messages(destination_channel, ids_to_delete)
            print(f"\n预清理完毕，共删除了 {len(ids_to_delete)} 条重复消息。")
        else:
            print("预清理完成，没有发现需要删除的重复消息。")

        return final_links

    except Exception as e:
        print(f"❌ 清理目标频道时发生错误: {e}")
        return set()
    finally:
        print("--- 目标频道预清理结束 ---\n")


async def main():
    """运行消息转发脚本的主函数"""
    config = {
        'api_id': os.environ.get('API_ID'),
        'api_hash': os.environ.get('API_HASH'),
        'destination_channel': os.environ.get('DESTINATION_CHANNEL'),
        'identifiers_string': os.environ.get('CHANNEL_IDENTIFIERS'),
        'ids_string': os.environ.get('CHANNEL_IDS'),
        'blacklist_string': os.environ.get('KEYWORD_BLACKLIST'),
        # --- 新增：读取用户ID黑名单 ---
        'user_blacklist_string': os.environ.get('USER_ID_BLACKLIST'),
        'dedup_enabled': os.environ.get('DEDUPLICATION_ENABLED', 'false').lower() == 'true',
        'dedup_cache_size': int(os.environ.get('DEDUPLICATION_CACHE_SIZE', 200))
    }

    # 加载关键词黑名单
    config['blacklist'] = [k.strip().lower() for k in config['blacklist_string'].split(',') if k.strip()] if config[
        'blacklist_string'] else []
    if config['blacklist']:
        print(f"已加载关键词黑名单: {config['blacklist']}")

    # --- 新增：加载用户ID黑名单 ---
    config['user_blacklist'] = set()
    if config['user_blacklist_string']:
        try:
            # 将ID转换为整数并存入 set
            config['user_blacklist'] = {int(uid.strip()) for uid in config['user_blacklist_string'].split(',') if
                                        uid.strip()}
            print(f"已加载用户ID黑名单: {config['user_blacklist']}")
        except ValueError:
            print("警告：USER_ID_BLACKLIST 格式不正确，应为逗号分隔的数字ID。")
    # --- 加载结束 ---

    if not all([config['api_id'], config['api_hash'], config['destination_channel']]):
        print("错误：请确保 .env 文件中已配置 API_ID, API_HASH, 和 DESTINATION_CHANNEL。")
        return

    async with TelegramClient(SESSION_NAME, config['api_id'], config['api_hash']) as client:
        print("已通过会话文件成功登录。")

        historical_links = await cleanup_and_get_historical_links(client, config)

        source_channel_ids = []
        if config['ids_string']:
            try:
                source_channel_ids = [int(id_str.strip()) for id_str in config['ids_string'].split(',') if
                                      id_str.strip()]
            except ValueError:
                print("错误：CHANNEL_IDS 格式不正确。")
                return
        elif config['identifiers_string']:
            identifiers = [i.strip() for i in config['identifiers_string'].split(',') if i.strip()]
            source_channel_ids = await get_channel_ids_from_identifiers(client, identifiers)
        else:
            print("错误：必须配置 CHANNEL_IDS 或 CHANNEL_IDENTIFIERS。")
            return
        if not source_channel_ids:
            print("未能获取任何有效的源频道ID，程序退出。")
            return
        print(f"程序将从以下源频道ID进行转发: {source_channel_ids}")

        all_new_messages = []
        latest_ids_map = {}
        for channel_id in source_channel_ids:
            last_id = get_last_id(channel_id)
            print(f"正在从频道 {channel_id} 收集自 ID {last_id + 1} 以来的新消息...")
            channel_messages = [msg async for msg in client.iter_messages(channel_id, min_id=last_id)]
            if channel_messages:
                all_new_messages.extend(channel_messages)
                latest_ids_map[channel_id] = max(m.id for m in channel_messages)

        if not all_new_messages:
            print("所有源频道都没有找到新消息。程序退出。")
            return

        all_new_messages.sort(key=lambda m: m.date)
        print(f"\n从所有频道共收集到 {len(all_new_messages)} 条新消息，开始统一过滤...")

        final_messages = all_new_messages
        if config['dedup_enabled']:
            print("  - 阶段一：处理本次运行内的重复链接...")
            link_map = {}
            messages_without_link_stage1 = []
            for msg in all_new_messages:
                if isinstance(msg, MessageService):
                    continue

                # [修复] 使用 getattr 安全地获取文本和标题
                message_text = getattr(msg, 'text', None) or getattr(msg, 'caption', None)
                if not message_text:
                    messages_without_link_stage1.append(msg)
                    continue

                link = extract_quark_link(message_text)
                if link:
                    if link not in link_map or msg.id > link_map[link].id:
                        link_map[link] = msg
                else:
                    messages_without_link_stage1.append(msg)

            messages_after_stage1 = list(link_map.values()) + messages_without_link_stage1
            messages_after_stage1.sort(key=lambda m: m.date)
            print(f"  - 阶段一后剩余 {len(messages_after_stage1)} 条消息。")

            print(f"  - 阶段二：与目标频道历史链接比对...")
            messages_after_stage2 = []
            for msg in messages_after_stage1:
                if isinstance(msg, MessageService):
                    continue

                # [修复] 使用 getattr 安全地获取文本和标题
                message_text = getattr(msg, 'text', None) or getattr(msg, 'caption', None)
                if not message_text:
                    messages_after_stage2.append(msg)
                    continue

                link = extract_quark_link(message_text)
                if not link or link not in historical_links:
                    messages_after_stage2.append(msg)
                else:
                    print(f"🤫 消息 ID {msg.id} (链接已存在于目标频道)，已跳过。")

            final_messages = messages_after_stage2
            print(f"  - 阶段二后剩余 {len(final_messages)} 条消息。")

        print(f"过滤完成，最终有 {len(final_messages)} 条消息准备转发。")
        for message in final_messages:
            # --- 修改：在调用时传入 user_blacklist ---
            await forward_message_task(
                client,
                message,
                config['destination_channel'],
                config['blacklist'],
                config['user_blacklist']  # <-- 传入 User ID 黑名单
            )

        if latest_ids_map:
            print("\n--- 更新所有频道的 last_id ---")
            for channel_id, max_id in latest_ids_map.items():
                save_last_id(channel_id, max_id)
                print(f"  - 频道 {channel_id} 的最新消息 ID 已更新为: {max_id}")

    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 所有任务已完成。")


if __name__ == '__main__':
    os.makedirs(CACHE_DIR, exist_ok=True)
    if os.path.exists(LOCK_FILE):
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 检测到锁文件，另一个实例可能正在运行，本次任务跳过。")
        sys.exit()

    try:
        with open(LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))

        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 程序开始运行...")
        asyncio.run(main())

    except Exception as e:
        print(f"程序运行时发生未捕获的错误: {e}")
    finally:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 锁文件已移除。")