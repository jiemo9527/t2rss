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

### 方式 A：本地构建（仓库源码）

在仓库根目录执行：

```bash
cd web_panel
docker compose up -d --build
```

默认访问地址：`http://127.0.0.1:8080`

健康检查：

```bash
curl http://127.0.0.1:8080/health
```

### 方式 B：Docker Hub 镜像（推荐快速部署）

当前公开镜像：

- `wanxve0000/t2rss-web-panel:latest`
- `wanxve0000/t2rss-web-panel:20260414`

```bash
docker pull wanxve0000/t2rss-web-panel:latest
mkdir -p /opt/t2rss-web-panel/data
docker run -d --name t2rss-web-panel \
  --restart unless-stopped \
  -p 8080:8000 \
  -v /opt/t2rss-web-panel/data:/app/data \
  wanxve0000/t2rss-web-panel:latest
```

也可以把 `web_panel/docker-compose.yml` 改成直接用镜像：

```yaml
services:
  t2rss-web:
    image: wanxve0000/t2rss-web-panel:latest
    container_name: t2rss-web-panel
    restart: unless-stopped
    ports:
      - "8080:8000"
    volumes:
      - ./data:/app/data
```

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
- 夸克链接去重（目标历史预清理 + 本轮去重 + 历史比对）
- 场景 7 支持：消息含“点击获取夸克链接”时，先跳转 Bot 解析链接，再按最终夸克链接去重，并将“点击获取夸克链接”替换为解析出的链接后转发
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
- RSS 条目正文里的 `http://` 与 `https://` 链接会输出为可点击链接。

## 7. 重要数据目录

`web_panel/data/` 下的关键文件：

- `config.env`：面板配置
- `panel.db`：断点、运行历史、登录防爆破
- `session/t2rss.session`：Telegram 会话
- `state/forwarder.lock`：运行锁
- `state/downloads/`：媒体临时目录
- `state/rss_feed.xml`：RSS 上一次成功刷新缓存
- `state/rss_session/`：RSS 刷新时创建的临时会话副本目录
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
