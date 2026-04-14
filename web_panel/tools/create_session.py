import os
from pathlib import Path

from dotenv import dotenv_values
from telethon.errors import SessionPasswordNeededError
from telethon.sync import TelegramClient


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT_DIR / "data"))).resolve()
ENV_FILE = DATA_DIR / "config.env"
SESSION_DIR = DATA_DIR / "session"
SESSION_BASE_PATH = SESSION_DIR / "t2rss"


def load_config() -> dict[str, str]:
    values = {}
    if ENV_FILE.exists():
        file_values = dotenv_values(ENV_FILE)
        for key, value in file_values.items():
            if value is None:
                continue
            values[key] = str(value)

    for key in ["API_ID", "API_HASH", "PHONE", "PASSWORD"]:
        if key not in values and os.environ.get(key):
            values[key] = str(os.environ.get(key))

    return values


def create_session() -> None:
    config = load_config()
    api_id = config.get("API_ID", "").strip()
    api_hash = config.get("API_HASH", "").strip()
    phone = config.get("PHONE", "").strip()
    password = config.get("PASSWORD", "").strip()

    if not api_id or not api_hash or not phone:
        print("缺少必要配置，请先在 data/config.env 中设置 API_ID、API_HASH、PHONE。")
        return

    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    with TelegramClient(str(SESSION_BASE_PATH), int(api_id), api_hash) as client:
        if not client.is_user_authorized():
            client.send_code_request(phone)
            code = input("请输入 Telegram 验证码：").strip()

            try:
                client.sign_in(phone, code)
            except SessionPasswordNeededError:
                if not password:
                    password = input("请输入 Telegram 两步验证密码：").strip()
                client.sign_in(password=password)

        if client.is_user_authorized():
            print(f"会话创建成功：{SESSION_BASE_PATH}.session")
        else:
            print("会话创建失败。")


if __name__ == "__main__":
    create_session()
