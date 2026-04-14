import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlencode

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth_security import LoginGuardStore, build_password_hash, ensure_auth_baseline, verify_password
from .backup_manager import BackupManager
from .checkpoint_store import ChannelCheckpointStore
from .config_store import ConfigStore, parse_channel_sources, parse_csv, parse_int_csv
from .forwarder_service import ForwarderRunner, resolve_identifiers_preview
from .history_store import RunHistoryStore
from .logging_utils import create_logger, rebind_logger_file_handler
from .time_utils import now_shanghai_iso, timestamp_to_shanghai_iso


BASE_DIR = Path(__file__).resolve().parent

config_store = ConfigStore()
config_store.ensure_directories()

logger = create_logger(config_store.log_file)
history_store = RunHistoryStore(config_store.db_path)
login_guard_store = LoginGuardStore(config_store.db_path)
checkpoint_store = ChannelCheckpointStore(config_store.db_path)
backup_manager = BackupManager(config_store.data_dir, config_store.backups_dir)
runner = ForwarderRunner(config_store, checkpoint_store, history_store, logger)

bootstrap_updates, bootstrap_password = ensure_auth_baseline(config_store.load_raw_config())
if bootstrap_updates:
    config_store.save_raw_config(bootstrap_updates)
if bootstrap_password:
    bootstrap_username = bootstrap_updates.get("PANEL_ADMIN_USERNAME", "admin")
    logger.warning(
        "首次启动已自动生成管理员账户，用户名: %s，初始密码: %s，请登录后立即修改。",
        bootstrap_username,
        bootstrap_password,
    )

raw_for_secret = config_store.load_raw_config()
session_secret = str(raw_for_secret.get("PANEL_SESSION_SECRET", "")).strip() or os.environ.get("PANEL_SESSION_SECRET", "")
if not session_secret:
    session_secret = secrets.token_urlsafe(48)
    logger.warning("未配置 PANEL_SESSION_SECRET，当前进程使用临时会话密钥。建议在配置页中设置固定值。")

app = FastAPI(title="T2RSS 管理面板", version="1.0.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    session_cookie="t2rss_panel_session",
    max_age=60 * 60 * 12,
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.on_event("startup")
async def on_startup() -> None:
    config_store.ensure_directories()
    moved_sessions = config_store.migrate_legacy_session_files()
    if moved_sessions > 0:
        logger.info("已将旧会话文件迁移到 t2rss.session，迁移文件数: %s", moved_sessions)
    backup_manager.ensure_directory()
    history_store.init_db()
    login_guard_store.init_db()
    checkpoint_store.init_db()
    migrated = checkpoint_store.migrate_from_files(config_store.last_id_dir)
    if migrated > 0:
        logger.info("已将旧版 last_id 文本记录迁移到数据库，共 %s 条。", migrated)
    await runner.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await runner.stop()


def redirect_with_message(path: str, message: str, level: str = "info") -> RedirectResponse:
    query = urlencode({"msg": message, "level": level})
    return RedirectResponse(url=f"{path}?{query}", status_code=303)


def common_context(request: Request, title: str) -> Dict[str, Any]:
    return {
        "request": request,
        "title": title,
        "msg": request.query_params.get("msg", ""),
        "level": request.query_params.get("level", "info"),
        "auth_user": request.session.get("username", ""),
    }


def read_panel_log_tail(log_file: Path, line_limit: int = 300) -> str:
    safe_limit = max(20, min(int(line_limit), 2000))
    if not log_file.exists():
        return "日志文件尚未生成。"

    try:
        content = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"读取日志失败: {exc}"

    lines = content.splitlines()
    if not lines:
        return "暂无日志输出。"

    return "\n".join(lines[-safe_limit:])


def clear_panel_log(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("", encoding="utf-8")


def ensure_channel_checkpoints(channel_ids: list[int]) -> int:
    """为新频道自动补齐断点记录，默认 last_id=0。"""
    created_count = 0
    for channel_id in sorted(set(channel_ids)):
        if checkpoint_store.get_record(channel_id) is None:
            checkpoint_store.set_last_id(channel_id, 0)
            created_count += 1
    return created_count


def collect_session_view_data() -> Dict[str, Any]:
    session_exists = config_store.session_file.exists()
    session_info: Dict[str, Any] = {}
    if session_exists:
        stat = config_store.session_file.stat()
        session_info = {
            "size_bytes": stat.st_size,
            "updated_at": timestamp_to_shanghai_iso(stat.st_mtime),
        }

    return {
        "session_exists": session_exists,
        "session_info": session_info,
        "session_path": str(config_store.session_file),
    }


def collect_form_payload(form, current: Dict[str, str], keys: list[str], bool_keys: set[str]) -> Dict[str, str]:
    payload: Dict[str, str] = {}
    for key in keys:
        if key in bool_keys:
            payload[key] = "true" if form.get(key) else "false"
        else:
            if key in form:
                payload[key] = str(form.get(key, "")).strip()
            else:
                payload[key] = current.get(key, "")
    return payload


def normalize_source_token(token: Any) -> str:
    raw = str(token or "").strip()
    if not raw:
        return ""

    if "#" in raw:
        raw = raw.split("#", 1)[0].strip()

    raw = raw.strip().strip(",")
    raw = raw.strip("[]")
    raw = raw.strip().strip("'\"")
    if raw.startswith("-"):
        raw = raw[1:].strip()

    return raw.strip()


def build_tme_link(channel_text: str) -> tuple[str, str]:
    raw = str(channel_text or "").strip()
    if not raw:
        return "", ""

    if raw.startswith("https://t.me/") or raw.startswith("http://t.me/"):
        url = raw.replace("http://", "https://", 1)
        display = url.replace("https://", "", 1)
        return display, url

    token = raw.lstrip("@")
    if not token:
        return "", ""

    url = f"https://t.me/{token}"
    display = f"t.me/{token}"
    return display, url


def parse_sources_input(text: str) -> list[str]:
    raw_text = str(text or "")
    normalized = raw_text.replace("，", ",")
    tokens = normalized.replace("\n", ",").split(",")

    items: list[str] = []
    seen = set()
    for token in tokens:
        cleaned = normalize_source_token(token)
        if not cleaned:
            continue
        if cleaned in seen:
            continue
        items.append(cleaned)
        seen.add(cleaned)
    return items


def load_source_items_from_config(raw_config: Dict[str, str]) -> list[Dict[str, Any]]:
    items = parse_channel_sources(raw_config.get("CHANNEL_SOURCES_JSON", "[]"))
    if items:
        return items

    fallback_identifiers = parse_csv(raw_config.get("CHANNEL_IDENTIFIERS", ""))
    fallback_ids: list[int] = []
    try:
        fallback_ids = parse_int_csv(raw_config.get("CHANNEL_IDS", ""), "CHANNEL_IDS")
    except ValueError:
        fallback_ids = []

    results: list[Dict[str, Any]] = []
    if fallback_identifiers:
        for source in fallback_identifiers:
            results.append(
                {
                    "source": source,
                    "cid": None,
                    "enabled": True,
                    "status": "pending",
                    "error": "",
                }
            )

    if fallback_ids and not results:
        for cid in fallback_ids:
            results.append(
                {
                    "source": str(cid),
                    "cid": cid,
                    "enabled": True,
                    "status": "ok",
                    "error": "",
                }
            )

    return results


def source_items_to_input_text(items: list[Dict[str, Any]]) -> str:
    sources = []
    for item in items:
        source = str(item.get("source", "")).strip()
        if source:
            sources.append(source)
    if not sources:
        return ""
    return "\n".join(sources)


def build_forward_settings_context(
    request: Request,
    raw_config: Dict[str, str],
    source_items: list[Dict[str, Any]] | None = None,
    override_destination: str | None = None,
) -> Dict[str, Any]:
    items = source_items if source_items is not None else load_source_items_from_config(raw_config)
    config_view = dict(raw_config)
    if override_destination is not None:
        config_view["DESTINATION_CHANNEL"] = override_destination

    return {
        **common_context(request, "转发设置"),
        "config": config_view,
        "source_items": items,
        "sources_input": source_items_to_input_text(items),
        "last_ids": checkpoint_store.list_last_ids(),
    }


def extract_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def safe_next_path(next_path: str) -> str:
    normalized = (next_path or "").strip()
    if not normalized.startswith("/"):
        return "/"
    if normalized.startswith("//"):
        return "/"
    return normalized


def auth_redirect_if_needed(request: Request) -> RedirectResponse | None:
    if request.session.get("authenticated") is True:
        return None

    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    query = urlencode({"next": next_path})
    return RedirectResponse(url=f"/login?{query}", status_code=303)


@app.get("/login")
async def login_page(request: Request):
    if request.session.get("authenticated") is True:
        return RedirectResponse(url="/", status_code=303)

    context = {
        "request": request,
        "title": "管理员登录",
        "msg": request.query_params.get("msg", ""),
        "level": request.query_params.get("level", "info"),
        "next": safe_next_path(request.query_params.get("next", "/")),
    }
    return templates.TemplateResponse("login.html", context)


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    next_path = safe_next_path(str(form.get("next", "/")))

    raw_config = config_store.load_raw_config()
    admin_username = str(raw_config.get("PANEL_ADMIN_USERNAME", "admin")).strip() or "admin"
    admin_password_hash = str(raw_config.get("PANEL_ADMIN_PASSWORD_HASH", "")).strip()
    admin_password_plain = str(raw_config.get("PANEL_ADMIN_PASSWORD", "")).strip()

    if not admin_password_hash and not admin_password_plain:
        query = urlencode(
            {
                "msg": "管理员密码未配置，请先在服务器配置后重试。",
                "level": "error",
                "next": next_path,
            }
        )
        return RedirectResponse(url=f"/login?{query}", status_code=303)

    ip = extract_client_ip(request)
    scope_username = username or admin_username
    locked_seconds = login_guard_store.get_lock_seconds(ip, scope_username)
    if locked_seconds > 0:
        query = urlencode(
            {
                "msg": f"登录已被临时锁定，请在 {locked_seconds} 秒后重试。",
                "level": "error",
                "next": next_path,
            }
        )
        return RedirectResponse(url=f"/login?{query}", status_code=303)

    is_valid_user = hmac_compare(username, admin_username)
    is_valid_password = verify_password(password, admin_password_hash, admin_password_plain)
    if is_valid_user and is_valid_password:
        login_guard_store.clear_failures(ip, admin_username)
        request.session.clear()
        request.session["authenticated"] = True
        request.session["username"] = admin_username
        return RedirectResponse(url=next_path, status_code=303)

    locked_after_failure = login_guard_store.record_failure(ip, scope_username, raw_config)
    await asyncio.sleep(0.8)

    if locked_after_failure > 0:
        msg = f"用户名或密码错误，已触发防爆破锁定 {locked_after_failure} 秒。"
    else:
        msg = "用户名或密码错误。"

    query = urlencode({"msg": msg, "level": "error", "next": next_path})
    return RedirectResponse(url=f"/login?{query}", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    query = urlencode({"msg": "已安全退出登录。", "level": "success"})
    return RedirectResponse(url=f"/login?{query}", status_code=303)


def hmac_compare(left: str, right: str) -> bool:
    return secrets.compare_digest(left or "", right or "")


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/")
async def dashboard(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    raw_config = config_store.load_raw_config()
    panel_settings = config_store.build_panel_settings()
    destination_display, destination_url = build_tme_link(raw_config.get("DESTINATION_CHANNEL", ""))

    keyword_blacklist = parse_csv(raw_config.get("KEYWORD_BLACKLIST", ""))
    user_id_blacklist = parse_csv(raw_config.get("USER_ID_BLACKLIST", ""))

    try:
        fallback_channel_ids = sorted(set(parse_int_csv(raw_config.get("CHANNEL_IDS", ""), "CHANNEL_IDS")))
    except ValueError:
        fallback_channel_ids = []

    source_items = parse_channel_sources(raw_config.get("CHANNEL_SOURCES_JSON", "[]"))
    resolved_cids_all = {int(item["cid"]) for item in source_items if isinstance(item.get("cid"), int)}
    resolved_cids_enabled = {
        int(item["cid"])
        for item in source_items
        if isinstance(item.get("cid"), int) and bool(item.get("enabled", True))
    }

    if source_items:
        total_source_count = len(source_items)
        enabled_source_count = len([item for item in source_items if bool(item.get("enabled", True))])
        channel_id_count = len(resolved_cids_enabled)
    else:
        total_source_count = len(fallback_channel_ids)
        enabled_source_count = len(fallback_channel_ids)
        channel_id_count = len(fallback_channel_ids)
        resolved_cids_enabled = set(fallback_channel_ids)
        resolved_cids_all = set(fallback_channel_ids)

    last_ids = checkpoint_store.list_last_ids()
    for row in last_ids:
        cid = int(row.get("channel_id", 0))
        if cid in resolved_cids_enabled:
            row["status_text"] = "开启"
        elif cid in resolved_cids_all:
            row["status_text"] = "停用"
        else:
            row["status_text"] = "停用"

    context = common_context(request, "仪表盘")
    context.update(
        {
            "session_exists": config_store.session_file.exists(),
            "lock_exists": config_store.lock_file.exists(),
            "last_ids": last_ids,
            "runner_status": runner.status_payload(),
            "config_preview": {
                "destination_channel": raw_config.get("DESTINATION_CHANNEL", ""),
                "destination_display": destination_display,
                "destination_url": destination_url,
                "source_summary": f"{enabled_source_count}/{total_source_count}",
                "keyword_blacklist_text": "，".join(keyword_blacklist),
                "keyword_blacklist_count": len(keyword_blacklist),
                "user_id_blacklist_text": "，".join(user_id_blacklist),
                "user_id_blacklist_count": len(user_id_blacklist),
                "deduplication_enabled": raw_config.get("DEDUPLICATION_ENABLED", "false"),
                "deduplication_cache_size": raw_config.get("DEDUPLICATION_CACHE_SIZE", "200"),
                "auto_run_enabled": panel_settings.auto_run_enabled,
                "auto_run_interval_minutes": panel_settings.auto_run_interval_minutes,
                "total_timeout_seconds": panel_settings.total_timeout_seconds,
                "test_mode_enabled": panel_settings.test_mode_enabled,
            },
        }
    )
    return templates.TemplateResponse("dashboard.html", context)


@app.post("/run")
async def run_now(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    started = await runner.trigger(trigger="manual")
    if not started:
        return redirect_with_message("/", "当前已有转发任务在运行。", "warn")
    return redirect_with_message("/", "转发任务已在后台启动。", "success")


@app.post("/run/stop")
async def force_stop_run(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    stopped = await runner.abort_current_run()
    if not stopped:
        return redirect_with_message("/", "当前没有可中止的运行任务。", "warn")
    return redirect_with_message("/", "已发送强制中止指令。", "success")


@app.get("/setup")
async def setup_page(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    context = common_context(request, "初始化接入")
    context.update(
        {
            "config": config_store.load_raw_config(),
            **collect_session_view_data(),
        }
    )
    return templates.TemplateResponse("setup.html", context)


@app.post("/setup/save")
async def setup_save(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    current = config_store.load_raw_config()

    keys = [
        "PANEL_LOGIN_MAX_FAILURES",
        "PANEL_LOGIN_WINDOW_SECONDS",
        "PANEL_LOGIN_LOCK_SECONDS",
        "API_ID",
        "API_HASH",
        "PHONE",
        "PASSWORD",
    ]
    payload = collect_form_payload(form, current, keys, bool_keys=set())

    # 敏感字段保存后不回显：页面提交为空时默认保持原值不变。
    sensitive_keys = {"API_ID", "API_HASH", "PHONE", "PASSWORD"}
    for key in sensitive_keys:
        if str(payload.get(key, "")).strip() == "":
            payload[key] = current.get(key, "")

    payload["PANEL_SESSION_SECRET"] = current.get("PANEL_SESSION_SECRET", "")
    payload["PANEL_ADMIN_PASSWORD"] = current.get("PANEL_ADMIN_PASSWORD", "")
    payload["PANEL_ADMIN_PASSWORD_HASH"] = current.get("PANEL_ADMIN_PASSWORD_HASH", "")
    payload["PANEL_ADMIN_USERNAME"] = current.get("PANEL_ADMIN_USERNAME", "admin")

    if not payload.get("PANEL_ADMIN_USERNAME", "").strip():
        payload["PANEL_ADMIN_USERNAME"] = "admin"

    config_store.save_raw_config(payload)
    return redirect_with_message("/setup", "初始化接入配置已保存。", "success")


@app.post("/setup/admin-credentials-save")
async def setup_admin_credentials_save(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    current = config_store.load_raw_config()

    old_password = str(form.get("PANEL_ADMIN_OLD_PASSWORD", "")).strip()
    new_username_input = str(form.get("PANEL_ADMIN_USERNAME", "")).strip()
    new_password = str(form.get("PANEL_ADMIN_NEW_PASSWORD", "")).strip()
    confirm_password = str(form.get("PANEL_ADMIN_NEW_PASSWORD_CONFIRM", "")).strip()

    current_username = str(current.get("PANEL_ADMIN_USERNAME", "admin")).strip() or "admin"
    current_hash = str(current.get("PANEL_ADMIN_PASSWORD_HASH", "")).strip()
    current_plain = str(current.get("PANEL_ADMIN_PASSWORD", "")).strip()

    if not old_password:
        return redirect_with_message("/setup", "请先输入当前密码，再修改用户名或密码。", "warn")
    if not verify_password(old_password, current_hash, current_plain):
        return redirect_with_message("/setup", "当前密码校验失败，未修改登录信息。", "warn")

    next_username = new_username_input or current_username
    if not next_username:
        return redirect_with_message("/setup", "管理员用户名不能为空。", "warn")
    if any(ch.isspace() for ch in next_username):
        return redirect_with_message("/setup", "管理员用户名不能包含空白字符。", "warn")

    username_changed = next_username != current_username
    password_changed = False

    payload = dict(current)
    payload["PANEL_ADMIN_USERNAME"] = next_username

    if new_password or confirm_password:
        if new_password != confirm_password:
            return redirect_with_message("/setup", "管理员新密码与确认密码不一致。", "warn")
        if len(new_password) < 8:
            return redirect_with_message("/setup", "管理员密码长度至少 8 位。", "warn")
        payload["PANEL_ADMIN_PASSWORD_HASH"] = build_password_hash(new_password)
        payload["PANEL_ADMIN_PASSWORD"] = ""
        password_changed = True

    if not username_changed and not password_changed:
        return redirect_with_message("/setup", "未检测到用户名或密码变更。", "info")

    config_store.save_raw_config(payload)
    if request.session.get("username") == current_username:
        request.session["username"] = next_username

    if username_changed and password_changed:
        return redirect_with_message("/setup", "管理员用户名和密码已更新。", "success")
    if username_changed:
        return redirect_with_message("/setup", "管理员用户名已更新。", "success")
    return redirect_with_message("/setup", "管理员密码已更新。", "success")


@app.get("/forward-settings")
async def forward_settings_page(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    raw_config = config_store.load_raw_config()
    context = build_forward_settings_context(request, raw_config)
    return templates.TemplateResponse("forward_settings.html", context)


@app.post("/forward-settings/save")
async def forward_settings_save(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    current = config_store.load_raw_config()
    sources_input_text = str(form.get("sources_input", "")).strip()

    destination_channel = str(form.get("DESTINATION_CHANNEL", "")).strip()
    if not destination_channel:
        return redirect_with_message("/forward-settings", "请先填写目标频道。", "warn")

    source_items: list[Dict[str, Any]] = []
    parsed_sources = parse_sources_input(sources_input_text)
    if parsed_sources:
        row_sources_raw = form.getlist("row_source")
        row_cids_raw = form.getlist("row_cid")
        row_status_raw = form.getlist("row_status")
        row_error_raw = form.getlist("row_error")
        enabled_source_set = {normalize_source_token(item) for item in form.getlist("row_enabled_source") if item}

        row_map: Dict[str, Dict[str, Any]] = {}
        for index, source_raw in enumerate(row_sources_raw):
            source = normalize_source_token(source_raw)
            if not source:
                continue

            cid_value = row_cids_raw[index] if index < len(row_cids_raw) else ""
            status_value = row_status_raw[index] if index < len(row_status_raw) else ""
            error_value = row_error_raw[index] if index < len(row_error_raw) else ""

            cid: int | None = None
            if str(cid_value).strip():
                try:
                    cid = int(str(cid_value).strip())
                except ValueError:
                    cid = None

            row_map[source] = {
                "cid": cid,
                "enabled": source in enabled_source_set and cid is not None,
                "status": str(status_value or ("ok" if cid is not None else "failed")),
                "error": str(error_value or ""),
            }

        for source in parsed_sources:
            row = row_map.get(source)
            if row is None:
                source_items.append(
                    {
                        "source": source,
                        "cid": None,
                        "enabled": False,
                        "status": "pending",
                        "error": "待解析",
                    }
                )
                continue

            source_items.append(
                {
                    "source": source,
                    "cid": row.get("cid"),
                    "enabled": bool(row.get("enabled", False)),
                    "status": str(row.get("status", "pending")),
                    "error": str(row.get("error", "")),
                }
            )

    enabled_cids = sorted({item["cid"] for item in source_items if isinstance(item.get("cid"), int) and item.get("enabled")})

    all_resolved_cids = sorted({item["cid"] for item in source_items if isinstance(item.get("cid"), int)})

    keys = [
        "KEYWORD_BLACKLIST",
        "USER_ID_BLACKLIST",
        "DEDUPLICATION_ENABLED",
        "DEDUPLICATION_CACHE_SIZE",
    ]
    payload = collect_form_payload(form, current, keys, bool_keys={"DEDUPLICATION_ENABLED"})

    payload["DESTINATION_CHANNEL"] = destination_channel
    payload["CHANNEL_IDS"] = ",".join(str(cid) for cid in enabled_cids)
    payload["CHANNEL_IDENTIFIERS"] = ",".join(item["source"] for item in source_items)
    payload["CHANNEL_SOURCES_JSON"] = json.dumps(source_items, ensure_ascii=False, separators=(",", ":"))

    config_store.save_raw_config(payload)
    created_count = ensure_channel_checkpoints(all_resolved_cids)
    disabled_count = len([item for item in source_items if not item.get("enabled")])

    if not source_items:
        return redirect_with_message("/forward-settings", "已保存：来源频道列表为空。", "success")

    if created_count > 0:
        return redirect_with_message(
            "/forward-settings",
            (
                f"已保存：启用来源 {len(enabled_cids)} 个，关闭来源 {disabled_count} 个，"
                f"自动新增断点 {created_count} 条。"
            ),
            "success",
        )
    if len(enabled_cids) == 0:
        return redirect_with_message("/forward-settings", "已保存：当前没有启用的来源频道。", "warn")
    return redirect_with_message(
        "/forward-settings",
        f"已保存：启用来源 {len(enabled_cids)} 个，关闭来源 {disabled_count} 个。",
        "success",
    )


@app.post("/settings/resolve")
@app.post("/forward-settings/resolve")
async def resolve_identifiers(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    raw_input = str(form.get("sources_input", "")).strip()
    if not raw_input:
        raw_input = str(form.get("identifiers_input", "")).strip()
    identifiers = parse_sources_input(raw_input)
    raw_config = config_store.load_raw_config()
    destination_channel = str(form.get("DESTINATION_CHANNEL", raw_config.get("DESTINATION_CHANNEL", ""))).strip()

    if not identifiers:
        context = build_forward_settings_context(request, raw_config, source_items=[], override_destination=destination_channel)
        context["sources_input"] = raw_input
        context["msg"] = "请至少输入一个待解析的来源频道。"
        context["level"] = "warn"
        return templates.TemplateResponse("forward_settings.html", context)

    old_source_rows = form.getlist("row_source")
    old_source_cids = form.getlist("row_cid")
    old_enabled = {normalize_source_token(item) for item in form.getlist("row_enabled_source") if item}

    old_map: Dict[str, Dict[str, Any]] = {}
    for idx, src in enumerate(old_source_rows):
        key = normalize_source_token(src)
        if not key:
            continue
        cid_raw = old_source_cids[idx] if idx < len(old_source_cids) else ""
        cid_value: int | None = None
        if str(cid_raw).strip():
            try:
                cid_value = int(str(cid_raw).strip())
            except ValueError:
                cid_value = None
        old_map[key] = {
            "cid": cid_value,
            "enabled": key in old_enabled,
        }

    source_items: list[Dict[str, Any]] = []
    try:
        resolved_rows = await resolve_identifiers_preview(config_store, identifiers, logger)
        resolved_map = {str(row.get("identifier", "")).strip(): row for row in resolved_rows}

        for source in identifiers:
            previous = old_map.get(source, {})
            resolved = resolved_map.get(source, {})
            ok = bool(resolved.get("ok", False))
            cid_val: int | None = None

            channel_id_value = resolved.get("channel_id")
            if ok and channel_id_value not in {None, ""}:
                try:
                    cid_val = int(str(channel_id_value).strip())
                except ValueError:
                    cid_val = None
            else:
                cid_val = previous.get("cid")

            enabled_default = previous.get("enabled", True)
            if cid_val is None:
                enabled_default = False

            source_items.append(
                {
                    "source": source,
                    "cid": cid_val,
                    "enabled": bool(enabled_default),
                    "status": "ok" if cid_val is not None else "failed",
                    "error": "" if cid_val is not None else str(resolved.get("error", "解析失败")),
                }
            )

        resolved_cids = sorted({item["cid"] for item in source_items if isinstance(item.get("cid"), int)})
        created_count = ensure_channel_checkpoints(resolved_cids)

        context = build_forward_settings_context(request, raw_config, source_items=source_items, override_destination=destination_channel)
        context["sources_input"] = "\n".join(identifiers)
        if created_count > 0:
            context["msg"] = (
                f"来源频道解析完成，已自动创建 {created_count} 条断点记录（默认 last_id=0）。"
                "请确认启用状态后保存通道配置。"
            )
        else:
            context["msg"] = "来源频道解析完成，断点记录已就绪。请确认启用状态后保存通道配置。"
        context["level"] = "success"
    except Exception as exc:
        source_items = []
        context = build_forward_settings_context(request, raw_config, source_items=source_items, override_destination=destination_channel)
        context["sources_input"] = "\n".join(identifiers)
        context["msg"] = f"来源频道解析失败：{exc}"
        context["level"] = "error"

    return templates.TemplateResponse("forward_settings.html", context)


@app.post("/settings/add-cid")
@app.post("/forward-settings/add-cid")
async def add_cid_to_channel_ids(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    return redirect_with_message("/forward-settings", "流程已升级，请在流程图中解析后直接保存通道配置。", "info")


@app.post("/checkpoints/upsert")
@app.post("/forward-settings/checkpoints/upsert")
async def upsert_checkpoint(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    raw_channel_id = str(form.get("channel_id", "")).strip()
    raw_last_id = str(form.get("last_id", "")).strip()

    if not raw_channel_id:
        return redirect_with_message("/forward-settings", "保存失败：频道 ID 不能为空。", "warn")

    try:
        channel_id = int(raw_channel_id)
    except ValueError:
        return redirect_with_message("/forward-settings", "保存失败：频道 ID 必须是整数。", "warn")

    try:
        last_id = int(raw_last_id)
    except ValueError:
        return redirect_with_message("/forward-settings", "保存失败：last_id 必须是整数。", "warn")

    if last_id < 0:
        return redirect_with_message("/forward-settings", "保存失败：last_id 不能小于 0。", "warn")

    checkpoint_store.set_last_id(channel_id, last_id)
    return redirect_with_message("/forward-settings", "断点保存成功。", "success")


@app.post("/forward-settings/checkpoints/batch-save")
async def batch_save_checkpoints(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    channel_ids_raw = form.getlist("batch_channel_id")
    last_ids_raw = form.getlist("batch_last_id")

    total = min(len(channel_ids_raw), len(last_ids_raw))
    saved_count = 0
    for idx in range(total):
        raw_channel_id = str(channel_ids_raw[idx]).strip()
        raw_last_id = str(last_ids_raw[idx]).strip()

        try:
            channel_id = int(raw_channel_id)
            last_id = int(raw_last_id)
        except ValueError:
            continue

        if last_id < 0:
            continue

        checkpoint_store.set_last_id(channel_id, last_id)
        saved_count += 1

    if saved_count == 0:
        return redirect_with_message("/forward-settings", "未保存任何断点（请检查输入）。", "warn")
    return redirect_with_message("/forward-settings", f"断点批量保存成功（{saved_count} 条）。", "success")


@app.get("/plan-backup")
async def plan_backup_page(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    context = common_context(request, "计划与备份")
    context.update(
        {
            "config": config_store.load_raw_config(),
            "backups": backup_manager.list_backups(),
        }
    )
    return templates.TemplateResponse("plan_backup.html", context)


@app.post("/plan-backup/save")
async def plan_backup_save(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    current = config_store.load_raw_config()

    keys = [
        "PANEL_TEST_MODE_ENABLED",
        "PANEL_TOTAL_TIMEOUT_SECONDS",
        "PANEL_AUTO_RUN_ENABLED",
        "PANEL_AUTO_RUN_INTERVAL_MINUTES",
    ]
    payload = collect_form_payload(
        form,
        current,
        keys,
        bool_keys={"PANEL_TEST_MODE_ENABLED", "PANEL_AUTO_RUN_ENABLED"},
    )
    config_store.save_raw_config(payload)
    return redirect_with_message("/plan-backup", "计划与调度配置已保存。", "success")


@app.post("/backups/create")
@app.post("/plan-backup/backups/create")
async def create_backup(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    try:
        backup_file = backup_manager.create_backup()
        return redirect_with_message("/plan-backup", f"备份创建成功：{backup_file.name}", "success")
    except Exception as exc:
        return redirect_with_message("/plan-backup", f"备份创建失败：{exc}", "error")


@app.get("/backups/download/{backup_name}")
@app.get("/plan-backup/backups/download/{backup_name}")
async def download_backup(request: Request, backup_name: str):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    backup_file = backup_manager.resolve_backup(backup_name)
    if backup_file is None:
        raise HTTPException(status_code=404, detail="未找到备份文件")
    return FileResponse(path=str(backup_file), filename=backup_file.name, media_type="application/zip")


@app.post("/backups/delete/{backup_name}")
@app.post("/plan-backup/backups/delete/{backup_name}")
async def delete_backup(request: Request, backup_name: str):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    deleted = backup_manager.delete_backup(backup_name)
    if deleted:
        return redirect_with_message("/plan-backup", f"备份已删除：{backup_name}", "success")
    return redirect_with_message("/plan-backup", "备份不存在或备份名无效。", "warn")


@app.post("/plan-backup/backups/restore/{backup_name}")
async def restore_backup(request: Request, backup_name: str):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    if runner.is_running:
        return redirect_with_message("/plan-backup", "当前有转发任务运行中，请先停止后再恢复备份。", "warn")

    backup_file = backup_manager.resolve_backup(backup_name)
    if backup_file is None:
        return redirect_with_message("/plan-backup", "待恢复备份不存在。", "warn")

    try:
        rollback_backup = backup_manager.create_backup_with_prefix("pre_restore_auto")
        result = backup_manager.restore_from_backup(backup_file)
        rebind_count = rebind_logger_file_handler(logger, config_store.log_file)
        logger.info("日志文件句柄已重绑，已替换 file handler: %s", rebind_count)
        logger.warning("♻️ 已从备份恢复数据: %s", backup_file.name)
        return redirect_with_message(
            "/plan-backup",
            (
                f"恢复成功（删除 {result['deleted_count']} 项，恢复 {result['copied_count']} 项）。"
                f"已自动创建回滚备份：{rollback_backup.name}。建议重启容器。"
            ),
            "success",
        )
    except Exception as exc:
        return redirect_with_message("/plan-backup", f"恢复失败：{exc}", "error")


@app.post("/plan-backup/backups/restore-upload")
async def restore_backup_upload(request: Request, file: UploadFile = File(...)):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    if runner.is_running:
        return redirect_with_message("/plan-backup", "当前有转发任务运行中，请先停止后再恢复备份。", "warn")

    if not file.filename or not file.filename.lower().endswith(".zip"):
        return redirect_with_message("/plan-backup", "请上传 .zip 备份文件。", "warn")

    payload = await file.read()
    if not payload:
        return redirect_with_message("/plan-backup", "上传的备份文件为空。", "warn")

    backup_manager.ensure_directory()
    stamp = now_shanghai_iso().replace(" ", "_").replace(":", "")
    upload_backup = backup_manager.backups_dir / f"uploaded_restore_{stamp}.zip"
    upload_backup.write_bytes(payload)

    try:
        rollback_backup = backup_manager.create_backup_with_prefix("pre_restore_auto")
        result = backup_manager.restore_from_backup(upload_backup)
        rebind_count = rebind_logger_file_handler(logger, config_store.log_file)
        logger.info("日志文件句柄已重绑，已替换 file handler: %s", rebind_count)
        logger.warning("♻️ 已从上传备份恢复数据: %s", upload_backup.name)
        return redirect_with_message(
            "/plan-backup",
            (
                f"上传备份恢复成功（删除 {result['deleted_count']} 项，恢复 {result['copied_count']} 项）。"
                f"已自动创建回滚备份：{rollback_backup.name}。建议重启容器。"
            ),
            "success",
        )
    except Exception as exc:
        return redirect_with_message("/plan-backup", f"上传备份恢复失败：{exc}", "error")


@app.get("/settings")
async def settings_redirect(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect
    return RedirectResponse(url="/setup", status_code=303)


@app.post("/settings")
async def settings_save_redirect(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect
    return RedirectResponse(url="/setup", status_code=303)


@app.get("/checkpoints")
async def checkpoints_redirect(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect
    return RedirectResponse(url="/forward-settings", status_code=303)


@app.get("/backups")
async def backups_redirect(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect
    return RedirectResponse(url="/plan-backup", status_code=303)


@app.get("/session")
async def session_page(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect
    return RedirectResponse(url="/setup", status_code=303)


@app.post("/session/upload")
async def upload_session(request: Request, file: UploadFile = File(...)):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    if not file.filename or not file.filename.lower().endswith(".session"):
        return redirect_with_message("/setup", "请上传有效的 .session 文件。", "error")

    payload = await file.read()
    if not payload:
        return redirect_with_message("/setup", "上传文件为空。", "error")

    config_store.session_dir.mkdir(parents=True, exist_ok=True)
    cleanup_candidates = [
        Path(f"{config_store.session_base_path}.session"),
        Path(f"{config_store.session_base_path}.session-journal"),
        Path(f"{config_store.session_base_path}.session-shm"),
        Path(f"{config_store.session_base_path}.session-wal"),
        Path(f"{config_store.legacy_session_base_path}.session"),
        Path(f"{config_store.legacy_session_base_path}.session-journal"),
        Path(f"{config_store.legacy_session_base_path}.session-shm"),
        Path(f"{config_store.legacy_session_base_path}.session-wal"),
    ]
    for candidate in cleanup_candidates:
        if candidate.exists():
            candidate.unlink(missing_ok=True)

    config_store.session_file.write_bytes(payload)
    return redirect_with_message("/setup", "会话文件上传成功，已保存为 t2rss.session。", "success")


@app.post("/checkpoints/delete")
@app.post("/forward-settings/checkpoints/delete")
async def delete_checkpoint(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    form = await request.form()
    raw_channel_id = str(form.get("channel_id", "") or form.get("delete_channel_id", "")).strip()
    if not raw_channel_id:
        return redirect_with_message("/forward-settings", "删除失败：频道 ID 不能为空。", "warn")

    try:
        channel_id = int(raw_channel_id)
    except ValueError:
        return redirect_with_message("/forward-settings", "删除失败：频道 ID 必须是整数。", "warn")

    deleted = checkpoint_store.delete_last_id(channel_id)
    if deleted:
        return redirect_with_message("/forward-settings", f"断点已删除：{channel_id}", "success")
    return redirect_with_message("/forward-settings", f"未找到断点记录：{channel_id}", "warn")


@app.post("/session/delete")
async def delete_session_file(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    candidates = [
        Path(f"{config_store.session_base_path}.session"),
        Path(f"{config_store.session_base_path}.session-journal"),
        Path(f"{config_store.session_base_path}.session-shm"),
        Path(f"{config_store.session_base_path}.session-wal"),
        Path(f"{config_store.legacy_session_base_path}.session"),
        Path(f"{config_store.legacy_session_base_path}.session-journal"),
        Path(f"{config_store.legacy_session_base_path}.session-shm"),
        Path(f"{config_store.legacy_session_base_path}.session-wal"),
    ]

    deleted_any = False
    for candidate in candidates:
        if candidate.exists():
            candidate.unlink(missing_ok=True)
            deleted_any = True

    if deleted_any:
        return redirect_with_message("/setup", "会话文件已删除。", "success")
    return redirect_with_message("/setup", "未发现可删除的会话文件。", "warn")


@app.get("/api/status")
async def api_status(request: Request):
    auth_redirect = auth_redirect_if_needed(request)
    if auth_redirect:
        return auth_redirect

    return JSONResponse(runner.status_payload())


@app.get("/api/logs/tail")
async def api_logs_tail(request: Request):
    if request.session.get("authenticated") is not True:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    raw_lines = str(request.query_params.get("lines", "300"))
    try:
        line_limit = int(raw_lines)
    except ValueError:
        line_limit = 300

    line_limit = max(20, min(line_limit, 2000))
    log_text = read_panel_log_tail(config_store.log_file, line_limit=line_limit)

    return JSONResponse(
        {
            "log_text": log_text,
            "line_limit": line_limit,
            "updated_at": now_shanghai_iso(),
        }
    )


@app.post("/api/logs/clear")
async def api_logs_clear(request: Request):
    if request.session.get("authenticated") is not True:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    clear_panel_log(config_store.log_file)
    return JSONResponse(
        {
            "ok": True,
            "message": "日志已清空。",
            "updated_at": now_shanghai_iso(),
        }
    )
