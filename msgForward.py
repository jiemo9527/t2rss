import os
import asyncio
from dotenv import load_dotenv
from telethon.sync import TelegramClient

# 从 .env 文件加载环境变量
load_dotenv()

# --- 路径定义 ---
SESSION_NAME = 'session_name'
CACHE_DIR = 'cache'
LAST_ID_DIR = os.path.join(CACHE_DIR, 'last_ids')
DOWNLOADS_DIR = os.path.join(CACHE_DIR, 'downloads')


# =================================================================
#  获取频道ID的内部函数
# =================================================================
async def get_channel_id_by_identifier(client, identifier):
    """
    （异步）通过标识符（用户名或私密链接ID）获取单个频道的ID。
    此函数现在会自动将标识符转换为可被 Telethon 识别的格式。
    """
    entity_to_get = identifier
    # 如果标识符是私密频道的邀请码 (以 '+' 开头),
    # 将其拼接成一个完整的 URL，Telethon 才能正确解析。
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


# =================================================================
#  核心转发功能函数
# =================================================================
def get_last_id(channel_id):
    """为指定频道获取最后转发的消息ID"""
    file_path = os.path.join(LAST_ID_DIR, f"{channel_id}.txt")
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            try:
                return int(f.read().strip())
            except (ValueError, IndexError):
                return 0
    return 0


def save_last_id(channel_id, message_id):
    """为指定频道保存最后转发的消息ID"""
    os.makedirs(LAST_ID_DIR, exist_ok=True)
    file_path = os.path.join(LAST_ID_DIR, f"{channel_id}.txt")
    with open(file_path, 'w') as f:
        f.write(str(message_id))


async def forward_message_task(client, message, destination_channel, semaphore):
    """处理单条消息的转发任务"""
    media_path = None
    async with semaphore:
        try:
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


async def forward_messages_from_channel(client, source_channel_id, destination_channel, semaphore):
    """从单个源频道转发新消息"""
    last_id = get_last_id(source_channel_id)
    print(f"正在检查频道 {source_channel_id} 中自消息 ID {last_id + 1} 以来的新消息...")
    messages_to_forward = [msg async for msg in client.iter_messages(source_channel_id, min_id=last_id, reverse=True)]
    if not messages_to_forward:
        print(f"频道 {source_channel_id} 中没有找到新消息。")
        return
    print(f"在频道 {source_channel_id} 中找到 {len(messages_to_forward)} 条新消息，准备转发。")
    tasks = [forward_message_task(client, msg, destination_channel, semaphore) for msg in messages_to_forward]
    if tasks:
        results = await asyncio.gather(*tasks)
        successful_ids = [r for r in results if r is not None]
        if successful_ids:
            max_id = max(successful_ids)
            save_last_id(source_channel_id, max_id)
            print(f"\n🎉 频道 {source_channel_id} 处理完毕。已保存最新消息 ID：{max_id}")


async def main():
    """运行消息转发脚本的主函数"""
    # --- 从 .env 文件加载所有配置 ---
    api_id = os.environ.get('API_ID')
    api_hash = os.environ.get('API_HASH')
    destination_channel = os.environ.get('DESTINATION_CHANNEL')
    identifiers_string = os.environ.get('CHANNEL_IDENTIFIERS')

    # --- 检查关键配置是否存在 ---
    if not all([api_id, api_hash, destination_channel, identifiers_string]):
        print("错误：请确保 .env 文件中已完整配置 API_ID, API_HASH, DESTINATION_CHANNEL, 和 CHANNEL_IDENTIFIERS。")
        return

    # 解析逗号分隔的字符串为标识符列表
    channel_identifiers_to_forward = [identifier.strip() for identifier in identifiers_string.split(',') if
                                      identifier.strip()]

    async with TelegramClient(SESSION_NAME, api_id, api_hash) as client:
        print("已通过会话文件成功登录。")

        # 动态获取源频道ID
        source_channel_ids = await get_channel_ids_from_identifiers(client, channel_identifiers_to_forward)

        if not source_channel_ids:
            print("未能从 .env 中配置的标识符获取任何有效频道ID，程序退出。")
            return

        print(f"程序将从以下源频道ID进行转发: {source_channel_ids}")
        print(f"目标频道: {destination_channel}")

        semaphore = asyncio.Semaphore(4)
        forwarding_tasks = [
            forward_messages_from_channel(client, channel_id, destination_channel, semaphore)
            for channel_id in source_channel_ids
        ]
        await asyncio.gather(*forwarding_tasks)

    print("\n所有任务已完成。")


if __name__ == '__main__':
    asyncio.run(main())
