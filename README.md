# T2RSS Web Panel 使用指南

本仓库主分支 (`main`) 现已聚焦 `web_panel`，用于通过网页管理 Telegram 多源转发。

如果你需要旧版命令行脚本（`msgForward.py` / `get_session.py` / `getCIDTEST.py`），请切换到 `cli` 分支：

```bash
git checkout cli
```

## 1. 环境要求

- Docker + Docker Compose
- 可用的 Telegram API 凭据：`API_ID`、`API_HASH`
- 一个可登录 Telegram 的账号（首次会话创建用）

## 2. 启动服务

### 方式 A：GHCR 镜像部署（推荐）

已弃用 Docker Hub，公开镜像发布到 GitHub Container Registry：

```text
ghcr.io/jiemo9527/t2rss:<tag>
```

镜像同时提供 `linux/amd64` 与 `linux/arm64`；服务器可匿名拉取，无需 `docker login`。推荐固定到提交短 SHA（例如 `704f0f5`），避免 `latest` 自动变化带来不可预期升级。

```bash
git clone https://github.com/jiemo9527/t2rss.git /opt/t2rss
cd /opt/t2rss/web_panel

# 首次部署：固定版本；也可替换为 latest
export T2RSS_IMAGE_TAG=704f0f5
docker compose pull
docker compose up -d

# 访问与健康检查（默认仅监听本机，适合由 Nginx/Caddy 反代）
curl http://127.0.0.1:8877/health
docker compose ps
```

`data/` 是持久化目录，包含配置、Telegram 会话、断点、日志和备份；升级镜像不会删除它。

### 方式 B：本地构建（仓库源码）

适合开发或验证未发布改动：

```bash
cd web_panel
docker compose up -d --build
curl http://127.0.0.1:8877/health
```

当前 compose 默认绑定 `127.0.0.1:8877`，不会直接暴露到公网。如需从其他主机访问，请配置反向代理；不要直接将管理面板端口暴露到公网。

### 更新与回滚

```bash
cd /opt/t2rss/web_panel

# 更新到指定已发布版本
export T2RSS_IMAGE_TAG=<新的短 SHA>
docker compose pull
docker compose up -d
curl http://127.0.0.1:8877/health

# 回滚：把 tag 改回上一个已验证版本，再执行相同命令
export T2RSS_IMAGE_TAG=<上一个短 SHA>
docker compose pull
docker compose up -d
```

GitHub Actions 会在 `main` 分支的 `web_panel/` 改动推送后自动构建并发布 `latest` 和对应提交短 SHA 标签。

## 3. 首次登录

- 首次启动如果未配置管理员密码，系统会自动生成随机初始密码并写入容器日志。
- 查看日志获取初始密码：

```bash
docker logs t2rss-web-panel
```

- 登录后请立刻在 **初始化接入** 页面修改管理员用户名/密码（需校验当前密码）。

## 4. 首次配置流程（推荐顺序）

1. 打开 **初始化接入** 页面，填写 `API_ID` / `API_HASH` / `PHONE` / `PASSWORD`（如有二步验证）。
2. 在 **会话管理** 上传 `.session` 文件，或在容器内创建会话：

   ```bash
   docker exec -it t2rss-web-panel python tools/create_session.py
   ```

   上传任意名称 `.session` 后，系统会统一保存为 `t2rss.session`。

3. 打开 **转发设置** 页面：
   - 左侧填写来源（`t.me` 邀请链接/用户名）
   - 点击“解析来源 -> CID”
   - 在中间表格启用需要的来源并保存
   - 填写目标频道 `DESTINATION_CHANNEL`
4. 在页面下方检查断点（`last_id`）并按需创建/修改/删除。
5. 回到 **仪表盘** 点击“立即执行转发”。

## 5. 核心功能说明

- 多源频道合并抓取 + 时间排序
- 关键词黑名单过滤
- 择词替换（发送前将命中的文本替换为空，支持词条列表与正则）
- 用户 ID 黑名单过滤
- 夸克 / 115 链接去重（目标历史预清理 + 本轮去重 + 历史比对）
- 场景 7 支持：消息含“点击获取夸克链接”时，先跳转 Bot 解析链接，再按最终夸克链接去重，并将“点击获取夸克链接”替换为解析出的链接后转发
- 去重链接优先级：同一条消息只要存在夸克链接，就使用夸克链接作为去重依据；没有夸克链接时才使用 115 链接
- 115 去重可在“转发设置”中单独勾选开启；开启后支持 `https://115cdn.com/s/<token>` 与 `https://hdhive.com/resource/115/<token>`，`115cdn.com` 会忽略 `password` 参数与访问码片段；正文链接、蓝字超链接和按钮链接都会参与识别
- 单实例锁（防止并发重入）
- 断点存储在 SQLite（`channel_last_id`）
- 测试模式（仅模拟，不真实发送、不更新断点）
- 自动运行、总超时、强制中止
- 备份创建/下载/删除/恢复（恢复前自动创建回滚备份）
- 计划与备份页支持一键清理垃圾/缓存/无用临时文件
- 生成带 token 的 RSS 订阅地址，输出目标频道最近消息
- RSS 刷新使用临时会话副本，实时刷新失败时自动返回上一次缓存，避免影响订阅器抓取

## 6. RSS 订阅

首页“转发配置快照”会显示 RSS 订阅地址，格式类似：

```text
http://你的域名或IP:端口/rss/<token>.xml
```

说明：

- RSS 地址带随机 token，适合复制到 RSS 阅读器订阅。
- RSS 内容来自 `DESTINATION_CHANNEL` 目标频道最近消息。
- 可在“初始化接入”页面开启/关闭 RSS，并调整 `PANEL_RSS_ITEM_LIMIT`（默认 500，范围 50-2000）。
- RSS 有缓存时会立即返回，并在后台刷新缓存；如果 Telegram 会话被转发任务占用、网络异常或临时失败，会返回上一次成功缓存的 XML；没有缓存时也会返回可订阅的空 RSS XML。
- RSS 条目正文里的明文 `http://` / `https://` 链接，以及 Telegram 蓝字超链接实体，都会输出为可点击链接。
- RSS 会缓存 Telegram 消息主图，并在条目正文中输出 `<img>`，同时附带 `enclosure` 图片字段。
- RSS 正文图片会生成最大 450x450 的等比优化 JPEG 缩略图，避免阅读器中图片过大撑开版面。
- RSS 条目会同时输出完整 `description` 与 `content:encoded`，不主动截断正文内容。

## 7. 重要数据目录

`web_panel/data/` 下的关键文件：

- `config.env`：面板配置
- `panel.db`：断点、运行历史、登录防爆破
- `session/t2rss.session`：Telegram 会话
- `state/forwarder.lock`：运行锁
- `state/downloads/`：媒体临时目录
- `state/rss_feed.xml`：RSS 上一次成功刷新缓存
- `state/rss_session/`：RSS 刷新时创建的临时会话副本目录
- `state/rss_media/`：RSS 条目主图缓存目录
- `logs/panel.log`：面板日志
- `backups/*.zip`：备份文件

## 8. 常用运维命令

重建并启动：

```bash
cd web_panel
docker compose up -d --build
```

查看服务状态：

```bash
cd web_panel
docker compose ps
```

查看实时日志：

```bash
docker logs -f t2rss-web-panel
```

停止服务：

```bash
cd web_panel
docker compose down
```

## 9. systemd 自启服务

仓库已提供 systemd 服务模板：`deploy/systemd/t2rss-panel.service`

适用于你的部署目录为 `/root/t2rss`（即 `docker-compose.yml` 在 `/root/t2rss/docker-compose.yml`）。

安装与启用：

```bash
sudo cp deploy/systemd/t2rss-panel.service /etc/systemd/system/t2rss-panel.service
sudo systemctl daemon-reload
sudo systemctl enable --now t2rss-panel
```

常用操作：

```bash
sudo systemctl status t2rss-panel
sudo systemctl restart t2rss-panel
sudo journalctl -u t2rss-panel -f
```

## 10. 常见问题

- 登录被锁：等待 `PANEL_LOGIN_LOCK_SECONDS` 到期，或在配置中调整锁定策略。
- 提示会话缺失：重新上传会话或在容器里运行 `tools/create_session.py`。
- 没有转发：检查来源是否已解析到 CID 且处于启用状态，目标频道是否可访问。
- 去重看起来不生效：确认 `DEDUPLICATION_ENABLED=true`，并适当增大 `DEDUPLICATION_CACHE_SIZE`。

## 11. 分支说明

- `main`：Web 管理面板版本（当前主线）
- `cli`：旧版 CLI 脚本版本
