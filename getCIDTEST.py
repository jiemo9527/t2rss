import os
import asyncio
from dotenv import load_dotenv
from telethon.sync import TelegramClient

# 从 .env 文件加载环境变量
load_dotenv()

# 会话文件路径指向根目录
SESSION_NAME = 'session_name'


async def get_channel_id_by_link(client, channel_link):
    """
    （异步）使用提供的客户端获取单个频道的ID。

    Args:
        client: 一个已认证的 Telethon 客户端实例。
        channel_link (str): 频道的用户名或邀请链接。

    Returns:
        Integer or None: 成功则返回频道ID，失败则返回None。
    """
    try:
        print(f"正在解析频道链接: {channel_link}")
        entity = await client.get_entity(channel_link)
        print(f"✅ 链接 '{channel_link}' -> ID: {entity.id}")
        return entity.id
    except Exception as e:
        print(f"❌ 解析链接 '{channel_link}' 时发生错误: {e}")
        return None


async def get_channel_ids(client, channel_links):
    """
    （异步）接收一个链接列表，返回所有有效频道的ID列表。

    Args:
        client: 一个已认证的 Telethon 客户端实例。
        channel_links (list): 包含多个频道链接的列表。

    Returns:
        list: 包含所有成功解析出的频道ID的列表。
    """
    print("\n--- 开始批量获取频道ID ---")
    tasks = [get_channel_id_by_link(client, link) for link in channel_links]
    results = await asyncio.gather(*tasks)
    # 过滤掉所有失败的结果 (None)
    valid_ids = [res for res in results if res is not None]
    print(f"--- 批量获取完成，成功找到 {len(valid_ids)} 个有效ID ---\n")
    return valid_ids


# 当此脚本被直接运行时，用于测试目的
async def standalone_test():
    """用于独立运行测试的函数"""
    api_id = os.environ.get('API_ID')
    api_hash = os.environ.get('API_HASH')

    if not all([api_id, api_hash]):
        print("错误：请确保 .env 文件中已配置 API_ID 和 API_HASH。")
        return

    async with TelegramClient(SESSION_NAME, api_id, api_hash) as client:
        channel_ids = await get_channel_ids(client, test_channel_links)
        print("获取到的ID列表:", channel_ids)

if __name__ == '__main__':
    test_channel_links = [
        'https://t.me/11111111111111',
    ]
    # 直接运行此文件可以测试ID获取功能
    asyncio.run(standalone_test())