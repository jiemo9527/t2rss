Telegram 多频道消息转发机器人

✨ 主要功能
多源转发: 支持从一个或多个源频道同时抓取消息。

灵活配置: 支持通过频道ID（推荐）或频道标识符（备用）两种方式指定源频道。

类型支持: 完美支持公开频道（通过用户名）和私密频道（通过邀请链接）。

关键词过滤: 可自定义黑名单，自动跳过包含指定关键词的消息。

内容去重: 智能识别并跳过近期已转发过的重复内容，避免信息冗余。

媒体处理: 能够转发文本消息、图片、视频和其他媒体文件。

断点续传: 自动记录每个频道已转发的最后一条消息ID，避免重复转发和消息丢失。

防止重复运行: 内置锁文件机制，确保在任何时候只有一个实例在运行，完美适用于高频率的定时任务。

异步高效: 基于 asyncio 实现异步处理，资源占用少，运行效率高。

结构清晰: 自动管理缓存文件，保持项目根目录整洁。


📂 项目结构

```
├── .env                  <-- 您的所有配置和机密信息
├── msgForward.py         <-- 主程序执行脚本
├── requirements.txt      <-- 项目依赖库
├── .gitignore            <-- (可选) 忽略不必要的文件
└── cache/                <-- (首次运行后自动生成)
    ├── downloads/        <-- 临时存放下载的媒体文件
    ├── last_ids/         <-- 存放每个频道的转发记录
    │   └── ...
    ├── dedup_cache.txt   <-- (开启去重后生成) 内容指纹缓存
    └── forwarder.lock    <-- (程序运行时生成) 防重复运行锁
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
# - 私密频道: 填写邀请链接CHANNEL_IDENTIFIERS或频道idCHANNEL_IDS
#CHANNEL_IDENTIFIERS=+Jxxxxxxxxxxxxxxxxx
# 公开频道填用户名，私密频道填邀请链接中 't.me/' 后面的部分
CHANNEL_IDS==12346578

# 目标频道：填写您要将消息转发到的频道的用户名
DESTINATION_CHANNEL=my_destination_channel_username
# --- 关键词过滤配置 ---
# 如果消息包含以下任一关键词，将被忽略。多个关键词用英文逗号(,)分隔
KEYWORD_BLACKLIST=#短剧,#综艺,#真人秀,黑马程序员,综艺,短剧,epub,#带货

# --- 内容去重配置 ---
# 是否开启内容去重功能 (true/false)
DEDUPLICATION_ENABLED=true
# 用于生成内容指纹的字符数 (如果消息开头N个字符相同则视为重复)
DEDUPLICATION_CHAR_COUNT=30
# 保留多少条最近消息的指纹用于比对
DEDUPLICATION_CACHE_SIZE=500

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

解析源频道（优先使用ID，其次是标识符）。

检查每条新消息是否包含黑名单关键词。

检查每条新消息是否与近期内容重复。

将通过所有过滤的新消息转发到目标频道。

更新已转发的消息ID和内容指纹记录。


🤖 自动化部署 (定时任务)
得益于内置的防重复运行机制，您可以放心地设置一个高频率的定时任务来执行此脚本。



📄 许可证
该项目采用 MIT License 授权。
