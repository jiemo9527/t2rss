import os
import asyncio
import sys
import time
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
DEDUP_CACHE_FILE = os.path.join(CACHE_DIR, 'dedup_cache.txt') # 内容去重缓存文件


# =================================================================
#  辅助函数 (ID获取、缓存读写)
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
            try: return int(f.read().strip())
            except (ValueError, IndexError): return 0
    return 0

def save_last_id(channel_id, message_id):
    """为指定频道保存最后转发的消息ID"""
    os.makedirs(LAST_ID_DIR, exist_ok=True)
    file_path = os.path.join(LAST_ID_DIR, f"{channel_id}.txt")
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(str(message_id))

def load_dedup_cache(file_path):
    """从文件加载去重缓存，现在返回一个集合(set)以实现哈希比对。"""
    if not os.path.exists(file_path):
        return set()
    fingerprints = set()
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            stripped_line = line.strip()
            # 解析 {fingerprint} 格式
            if stripped_line.startswith('{') and stripped_line.endswith('}'):
                fingerprints.add(stripped_line[1:-1])
    return fingerprints

def save_dedup_cache(file_path, cache_set, max_size):
    """将去重缓存写入文件，现在接收一个集合(set)并处理新格式。"""
    # 将集合转换为列表以便进行切片
    cache_list = list(cache_set)
    
    # 通过只保留列表末尾的部分来近似实现“保留最新”
    if len(cache_list) > max_size:
        items_to_save = cache_list[-max_size:]
    else:
        items_to_save = cache_list

    with open(file_path, 'w', encoding='utf-8') as f:
        # 将每个指纹用 {} 包裹后写入文件
        for fingerprint in items_to_save:
            f.write(f"{{{fingerprint}}}\n")


# =================================================================
#  核心转发功能函数
# =================================================================
async def forward_message_task(client, message, config):
    """处理单条消息的转发任务"""
    media_path = None
    destination_channel = config['destination_channel']
    blacklist = config['blacklist']
    dedup_enabled = config['dedup_enabled']
    dedup_char_count = config['dedup_char_count']
    dedup_cache = config['dedup_cache']
    processed_in_run = config['processed_in_run'] # 获取即时去重集合

    async with config['semaphore']:
        try:
            full_text = (message.text or message.caption or "").lower()

            # 1. 关键词过滤
            if blacklist and full_text:
                if any(keyword in full_text for keyword in blacklist):
                    print(f"🤫 消息 ID {message.id} 包含关键词，已跳过。")
                    return None

            # 2. 内容去重
            if dedup_enabled and dedup_char_count > 0 and full_text:
                fingerprint = full_text[:dedup_char_count]
                # 现在两个比对都是高效的哈希查找
                if fingerprint in dedup_cache or fingerprint in processed_in_run:
                    print(f"🤫 消息 ID {message.id} 内容重复，已跳过。")
                    return None
                # 如果不重复，立刻将指纹加入即时缓存
                processed_in_run.add(fingerprint)


            if not message.text and not message.media: return None
            
            print(f"➡️ 正在转发来自频道 {message.chat_id} 的消息 ID: {message.id}")
            if message.media:
                os.makedirs(DOWNLOADS_DIR, exist_ok=True)
                media_path = await message.download_media(file=DOWNLOADS_DIR)
            
            await client.send_message(destination_channel, message.text, file=media_path)
            
            # 成功转发后，更新持久化去重缓存
            if dedup_enabled and dedup_char_count > 0 and full_text:
                dedup_cache.add(full_text[:dedup_char_count])

            print(f"✅ 已成功转发消息 ID {message.id} 到 {destination_channel}")
            return message.id
        except Exception as e:
            print(f"❌ 转发消息 ID {message.id} 时出错: {e}")
            return None
        finally:
            if media_path and os.path.exists(media_path):
                os.remove(media_path)


async def forward_messages_from_channel(client, source_channel_id, config):
    """从单个源频道转发新消息"""
    try:
        last_id = get_last_id(source_channel_id)
        print(f"正在检查频道 {source_channel_id} 中自消息 ID {last_id + 1} 以来的新消息...")

        messages_to_forward = [msg async for msg in client.iter_messages(source_channel_id, min_id=last_id, reverse=True)]

        if not messages_to_forward:
            print(f"频道 {source_channel_id} 中没有找到新消息。")
            return

        print(f"在频道 {source_channel_id} 中找到 {len(messages_to_forward)} 条新消息，准备转发。")
        tasks = [forward_message_task(client, msg, config) for msg in messages_to_forward]
        if tasks:
            results = await asyncio.gather(*tasks)
            successful_ids = [r for r in results if r is not None]
            if successful_ids:
                max_id = max(successful_ids)
                save_last_id(source_channel_id, max_id)
                print(f"\n🎉 频道 {source_channel_id} 处理完毕。已保存最新消息 ID：{max_id}")

    except ValueError as e:
        print(f"\n❌ 处理频道 {source_channel_id} 时发生错误: {e}")
        print(f"   这通常意味着您没有加入该频道/群组，或者提供的ID不正确。将跳过此频道。\n")
    except Exception as e:
        print(f"\n❌ 处理频道 {source_channel_id} 时发生未知错误: {e}\n")


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
        'dedup_char_count': int(os.environ.get('DEDUPLICATION_CHAR_COUNT', 30)),
        'dedup_cache_size': int(os.environ.get('DEDUPLICATION_CACHE_SIZE', 500))
    }
    
    # --- 准备关键词黑名单 ---
    config['blacklist'] = [k.strip().lower() for k in config['blacklist_string'].split(',') if k.strip()] if config['blacklist_string'] else []
    if config['blacklist']:
        print(f"已加载关键词黑名单: {config['blacklist']}")

    # --- 准备内容去重缓存 ---
    if config['dedup_enabled']:
        # 现在直接加载为 set
        config['dedup_cache'] = load_dedup_cache(DEDUP_CACHE_FILE)
        print(f"内容去重功能已开启，缓存 {len(config['dedup_cache'])} 条指纹。")
    else:
        config['dedup_cache'] = set()
    
    # 初始化本次运行的即时去重集合
    config['processed_in_run'] = set()


    # --- 检查关键配置是否存在 ---
    if not all([config['api_id'], config['api_hash'], config['destination_channel']]):
        print("错误：请确保 .env 文件中已配置 API_ID, API_HASH, 和 DESTINATION_CHANNEL。")
        return

    # --- 登录客户端 ---
    async with TelegramClient(SESSION_NAME, config['api_id'], config['api_hash']) as client:
        print("已通过会话文件成功登录。")
        print("正在预热会话缓存...")
        await client.get_dialogs()
        print("缓存预热完毕。")

        # --- 获取源频道ID ---
        source_channel_ids = []
        if config['ids_string']:
            print("检测到 CHANNEL_IDS 配置，将直接使用提供的ID。")
            try:
                source_channel_ids = [int(id_str.strip()) for id_str in config['ids_string'].split(',') if id_str.strip()]
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
        print(f"目标频道: {config['destination_channel']}")
        
        config['semaphore'] = asyncio.Semaphore(4)
        forwarding_tasks = [
            forward_messages_from_channel(client, channel_id, config)
            for channel_id in source_channel_ids
        ]
        await asyncio.gather(*forwarding_tasks)

    # --- 保存去重缓存 ---
    if config['dedup_enabled']:
        save_dedup_cache(DEDUP_CACHE_FILE, config['dedup_cache'], config['dedup_cache_size'])
        print("内容去重缓存已保存。")

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
