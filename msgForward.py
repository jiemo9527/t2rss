import os
import asyncio
import sys
import time
import re
import collections
from dotenv import load_dotenv
from telethon.sync import TelegramClient

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
async def forward_message_task(client, message, destination_channel, blacklist):
    """处理单条消息的转发任务（只包含关键词过滤和实际发送）。"""
    media_path = None
    try:
        full_text = (message.text or message.caption or "").lower()

        # 关键词过滤
        if blacklist and full_text:
            if any(keyword in full_text for keyword in blacklist):
                print(f"🤫 消息 ID {message.id} (关键词过滤)，已跳过。")
                return None

        if not message.text and not message.media: return None

        print(f"➡️ 正在转发来自频道 {message.chat_id} 的消息 ID: {message.id}")
        if message.media:
            os.makedirs(DOWNLOADS_DIR, exist_ok=True)
            media_path = await message.download_media(file=DOWNLOADS_DIR)

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
            link = extract_quark_link(message.text or message.caption)
            if link:
                # 按链接分组，并按消息ID排序（大→小，即新→旧）
                link_groups[link].append(message)
                link_groups[link].sort(key=lambda m: m.id, reverse=True)

        ids_to_delete = []
        final_links = set()
        for link, messages in link_groups.items():
            final_links.add(link)  # 保留这个链接
            if len(messages) > 1:
                # 保留最新的消息 (messages[0])，删除其余的
                messages_to_delete = messages[1:]
                delete_ids = [msg.id for msg in messages_to_delete]
                ids_to_delete.extend(delete_ids)
                print(f"  - 发现重复链接: {link}")
                print(f"    - 保留最新消息 ID: {messages[0].id}")
                print(f"    - 准备删除旧消息: {delete_ids}")

        if ids_to_delete:
            await client.delete_messages(destination_channel, ids_to_delete)
            print(f"\n预清理完毕，共删除了 {len(ids_to_delete)} 条重复消息。")
        else:
            print("预清理完成，没有发现需要删除的重复消息。")

        return final_links

    except Exception as e:
        print(f"❌ 清理目标频道时发生错误: {e}")
        return set()  # 出错时返回空集合，避免影响后续流程
    finally:
        print("--- 目标频道预清理结束 ---\n")


async def main():
    """运行消息转发脚本的主函数"""
    # --- 从 .env 文件加载所有配置 ---
    config = {
        'api_id': os.environ.get('API_ID'),
        'api_hash': os.environ.get('API_HASH'),
        'destination_channel': os.environ.get('DESTINATION_CHANNEL'),
        'identifiers_string': os.environ.get('CHANNEL_IDENTIFIERS'),
        'ids_string': os.environ.get('CHANNEL_IDS'),
        'blacklist_string': os.environ.get('KEYWORD_BLACKLIST'),
        'dedup_enabled': os.environ.get('DEDUPLICATION_ENABLED', 'false').lower() == 'true',
        'dedup_cache_size': int(os.environ.get('DEDUPLICATION_CACHE_SIZE', 200))
    }

    config['blacklist'] = [k.strip().lower() for k in config['blacklist_string'].split(',') if k.strip()] if config[
        'blacklist_string'] else []
    if config['blacklist']:
        print(f"已加载关键词黑名单: {config['blacklist']}")

    if not all([config['api_id'], config['api_hash'], config['destination_channel']]):
        print("错误：请确保 .env 文件中已配置 API_ID, API_HASH, 和 DESTINATION_CHANNEL。")
        return

    async with TelegramClient(SESSION_NAME, config['api_id'], config['api_hash']) as client:
        print("已通过会话文件成功登录。")
        print("正在预热会话缓存...")
        await client.get_dialogs()
        print("缓存预热完毕。")

        # --- 【新逻辑】第一步：预清理目标频道并获取历史链接 ---
        historical_links = await cleanup_and_get_historical_links(client, config)

        # --- 获取源频道ID ---
        source_channel_ids = []
        if config['ids_string']:
            print("检测到 CHANNEL_IDS 配置，将直接使用提供的ID。")
            try:
                source_channel_ids = [int(id_str.strip()) for id_str in config['ids_string'].split(',') if
                                      id_str.strip()]
            except ValueError:
                print("错误：CHANNEL_IDS 格式不正确。")
                return
        elif config['identifiers_string']:
            print("未配置 CHANNEL_IDS，将使用 CHANNEL_IDENTIFIERS。")
            identifiers = [i.strip() for i in config['identifiers_string'].split(',') if i.strip()]
            source_channel_ids = await get_channel_ids_from_identifiers(client, identifiers)
        else:
            print("错误：必须配置 CHANNEL_IDS 或 CHANNEL_IDENTIFIERS。")
            return

        if not source_channel_ids:
            print("未能获取任何有效的源频道ID，程序退出。")
            return

        print(f"程序将从以下源频道ID进行转发: {source_channel_ids}")

        # --- 【新逻辑】第二步：从所有源频道收集新消息 ---
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
            print("所有源频道都没有找到新消息。")
            # 即使没有新消息，也需要更新 last_id，以防有被删除的消息
            for channel_id in source_channel_ids:
                if channel_id not in latest_ids_map:
                    # 尝试获取频道的最新消息ID
                    try:
                        async for last_msg in client.iter_messages(channel_id, limit=1):
                            save_last_id(channel_id, last_msg.id)
                            print(f"频道 {channel_id} 无新消息，但将 last_id 更新至 {last_msg.id}")
                    except Exception:
                        pass  # 如果频道无法访问，则跳过
            return

        all_new_messages.sort(key=lambda m: m.date)
        print(f"\n从所有频道共收集到 {len(all_new_messages)} 条新消息，开始统一过滤...")

        final_messages = all_new_messages
        dedup_enabled = config['dedup_enabled']

        # --- 【新逻辑】第三步：对合并后的消息列表进行过滤 ---
        if dedup_enabled:
            # --- 阶段一：本次运行内部去重 ---
            print("  - 阶段一：处理本次运行内的重复链接...")
            link_map = {}
            messages_without_link_stage1 = []
            for msg in all_new_messages:
                link = extract_quark_link(msg.text or msg.caption)
                if link:
                    if link not in link_map or msg.id > link_map[link].id:
                        link_map[link] = msg
                else:
                    messages_without_link_stage1.append(msg)

            messages_after_stage1 = list(link_map.values()) + messages_without_link_stage1
            messages_after_stage1.sort(key=lambda m: m.date)
            print(f"  - 阶段一后剩余 {len(messages_after_stage1)} 条消息。")

            # --- 阶段二：与目标频道历史记录比对去重 ---
            print(f"  - 阶段二：与目标频道历史链接比对...")
            messages_after_stage2 = []
            for msg in messages_after_stage1:
                link = extract_quark_link(msg.text or msg.caption)
                # 如果消息没有链接，直接通过
                if not link:
                    messages_after_stage2.append(msg)
                    continue
                # 如果有链接，且链接不存在于历史记录中，则通过
                if link not in historical_links:
                    messages_after_stage2.append(msg)
                else:
                    print(f"🤫 消息 ID {msg.id} (链接已存在于目标频道)，已跳过。")

            final_messages = messages_after_stage2
            print(f"  - 阶段二后剩余 {len(final_messages)} 条消息。")

        # --- 【新逻辑】第四步：顺序处理最终筛选出的消息 ---
        print(f"过滤完成，最终有 {len(final_messages)} 条消息准备转发。")
        for message in final_messages:
            await forward_message_task(client, message, config['destination_channel'], config['blacklist'])

        # --- 【新逻辑】第五步：更新所有频道的 last_id ---
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
