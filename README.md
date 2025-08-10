Telegram 多频道消息转发机器人

✨ 主要功能
多源转发: 支持从一个或多个源频道同时抓取消息。

类型支持: 完美支持公开频道（通过用户名）和私密频道（通过邀请链接）。

配置简单: 所有配置（API凭证、源频道、目标频道）均在 .env 文件中完成，代码无需任何改动。

媒体处理: 能够转发文本消息、图片、视频和其他媒体文件。

断点续传: 自动记录每个频道已转发的最后一条消息ID，避免重复转发和消息丢失。

异步高效: 基于 asyncio 实现异步处理，资源占用少，运行效率高。
-.  结构清晰: 自动管理缓存文件，保持项目根目录整洁。

📂 项目结构

```
├── .env                  <-- 您的所有配置和机密信息
├── msgForward.py         <-- 主程序执行脚本
├── requirements.txt      <-- 项目依赖库
├── .gitignore            <-- 忽略不必要的文件
└── cache/                <-- (首次运行后自动生成)
    ├── downloads/        <-- 临时存放下载的媒体文件
    ├── last_ids/         <-- 存放每个频道的转发记录
    │   └── ...
    └── session_name.session  <-- (首次运行后在根目录生成)
```



🚀 安装与设置
请按照以下步骤来设置和运行此项目。

1. 克隆仓库
git clone https://github.com/jiemo9527/t2rss.git


2. 安装依赖
项目依赖 telethon 和 python-dotenv。
```
pip install telethon
pip install python-dotenv
```
3. 创建并配置 .env 文件
这是最关键的一步。在项目根目录创建一个名为 .env 的文件，然后将以下模板内容复制进去，并修改为您自己的信息。

# .env 文件模板
```
API_ID=12345678
API_HASH=your_api_hash_string

# 您的Telegram账号信息
PHONE=+1234567890
# 如果您开启了二次验证（Two-Factor Authentication），请填写此项，否则留空
PASSWORD=your_2fa_password

# --- 频道配置 ---
# 源频道：填写您想转发的频道的标识符，多个频道用英文逗号(,)分隔
# - 公开频道: 直接填写用户名 (例如: durov)
# - 私密频道: 填写邀请链接中 't.me/' 后面的部分 (例如: +Jc37JCr1diEzNDMx)
CHANNEL_IDENTIFIERS=+Jc37JCr1diEzNDMx,durov

# 目标频道：填写您要将消息转发到的频道的用户名
DESTINATION_CHANNEL=my_destination_channel_username
```
4. 首次运行与登录
第一次运行脚本时，telethon 需要登录您的Telegram账号来生成一个 .session 会话文件。

在终端中运行主脚本：

```python get_session.py```

程序会提示您输入发送到Telegram的验证码。输入后，如果需要，还会要求输入二次验证密码。成功登录后，会在根目录生成 session_name.session 文件，未来的运行将通过此文件自动登录。

🛠️ 使用
完成设置后，每次需要转发消息时，只需运行主脚本即可：

```python msgForward.py```

脚本会自动完成以下所有工作：

读取 .env 配置。

使用 .session 文件登录。

解析所有源频道标识符，获取其内部ID。

检查每个源频道的最新消息。

将新消息转发到目标频道。

更新已转发的消息ID记录。

📄 许可证
该项目采用 MIT License 授权。
