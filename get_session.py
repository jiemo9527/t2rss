import os
from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.errors import SessionPasswordNeededError

# 从 .env 文件加载环境变量
load_dotenv()

# 会话文件直接保存在根目录
SESSION_NAME = 'session_name'

def create_telegram_session():
    """
    通过读取 .env 文件中的配置连接到Telegram，并在项目根目录创建会话文件。
    """
    # 从环境中获取配置
    api_id = os.environ.get('API_ID')
    api_hash = os.environ.get('API_HASH')
    phone = os.environ.get('PHONE')
    password = os.environ.get('PASSWORD')

    if not all([api_id, api_hash, phone]):
        print("错误：请确保 .env 文件中已配置 API_ID, API_HASH, 和 PHONE。")
        return

    # 使用根目录路径创建客户端
    with TelegramClient(SESSION_NAME, api_id, api_hash) as client:
        if not client.is_user_authorized():
            client.send_code_request(phone)
            try:
                client.sign_in(phone, input('请输入您收到的Telegram验证码: '))
            except SessionPasswordNeededError:
                if password:
                    client.sign_in(password=password)
                else:
                    print("错误：检测到二次验证，请在 .env 文件中配置 PASSWORD。")
                    return

        if client.is_user_authorized():
            print(f"会话文件 '{SESSION_NAME}.session' 在根目录创建成功。")
        else:
            print("会话文件创建失败。")

if __name__ == "__main__":
    create_telegram_session()