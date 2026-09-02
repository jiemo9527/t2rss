# Project Agent Guide

This document is the working handbook for coding agents and maintainers in this repository.
It is updated for the current codebase status (`main` keeps `web_panel`; legacy scripts moved to `cli` branch).

## 1) Scope and Source of Truth

- This repo is organized by branch:
  - `main`: Web panel implementation under `web_panel/` (FastAPI + Docker)
  - `cli`: Legacy CLI scripts (`msgForward.py`, `get_session.py`, `getCIDTEST.py`)
- For ongoing feature work, **`main` + `web_panel/` is the source of truth**.
- Legacy CLI mode is maintained in `cli` branch for compatibility/manual runs.

## 2) Repository Layout

- Root (`main`):
  - `README.md`: primary usage guide (Web panel focused)
  - `AGENTS.md`: agent/developer handbook
  - `.gitignore`: repo ignore rules
- Web panel (`main`):
  - `web_panel/app/main.py`: FastAPI entrypoint, routes, page orchestration
  - `web_panel/app/forwarder_service.py`: forwarding pipeline + runner
  - `web_panel/app/config_store.py`: config parsing/persistence, session path conventions
  - `web_panel/app/checkpoint_store.py`: checkpoint DB table and operations
  - `web_panel/app/auth_security.py`: password hashing and login lockout
  - `web_panel/app/history_store.py`: run history DB
  - `web_panel/app/backup_manager.py`: backup create/delete/restore
  - `web_panel/app/templates/`: dashboard/setup/forward-settings/plan-backup pages
  - `web_panel/app/static/style.css`: panel styles
  - `web_panel/config_presets/text_replacement_rules.json`: versioned
    `TEXT_REPLACEMENT_REGEX` rule set (data/ is gitignored, so this is the only
    history the rules have)
  - `web_panel/scripts/restore_text_rules.py`: restore those rules into a running
    container, refusing to write unless they compile and survive the config
    encode/decode round-trip
  - `web_panel/tests/`: standalone regression scripts (no pytest; run each with
    `python <file>` inside the container) — `test_config_roundtrip.py`,
    `test_persistence_audit.py`, `test_forwarder_resilience.py`,
    `test_lock_staleness.py`
  - `web_panel/tools/create_session.py`: creates `t2rss.session` in container data dir
  - `web_panel/docker-compose.yml`, `web_panel/Dockerfile`: container runtime
  - `web_panel/data/`: runtime state (config, db, logs, session, backups)

## 3) Runtime Modes

### 3.1 Legacy mode (`cli` branch)

- Session filename: `session_name.session`
- Checkpoints: `cache/last_ids/*.txt`
- Lock file: `cache/forwarder.lock`

### 3.2 Web panel mode (`main` -> `web_panel`)

- Session filename: `data/session/t2rss.session`
- Legacy session migration on startup:
  - `session_name.session` -> `t2rss.session`
  - also migrates SQLite side files (`-journal`, `-shm`, `-wal`)
- Checkpoints in SQLite table `channel_last_id` (`data/panel.db`)
- Lock file: `data/state/forwarder.lock`

## 4) Web Panel Data Model

Primary persisted artifacts under `web_panel/data/`:

- `config.env`: panel and forwarder config
- `panel.db`:
  - `channel_last_id` (checkpoint store)
  - `run_history` (execution records)
  - `login_guard` (anti-bruteforce state)
- `state/forwarder.lock`: single-run lock
- `state/downloads/`: temporary media files
- `state/rss_feed.xml`: last successful RSS XML cache
- `state/rss_session/`: temporary copied Telethon sessions for RSS refresh
- `state/rss_media/`: cached images exposed through tokenized RSS media URLs
- `logs/panel.log`: rotating app logs
- `backups/*.zip`: snapshots and rollback artifacts

## 5) Authoritative Environment Keys (web panel)

Forwarding-related keys:

- `API_ID`, `API_HASH`, `PHONE`, `PASSWORD`
- `DESTINATION_CHANNEL`
- `CHANNEL_IDS`
- `CHANNEL_IDENTIFIERS`
- `CHANNEL_SOURCES_JSON` (authoritative for flow UI source rows)
- `KEYWORD_BLACKLIST`
- `USER_ID_BLACKLIST`
- `DEDUPLICATION_ENABLED`
- `DEDUPLICATION_115_ENABLED`
- `DEDUPLICATION_BAIDU_ENABLED`
- `DEDUPLICATION_UC_ENABLED`
- `DEDUPLICATION_CACHE_SIZE`
- `MAX_VIDEO_SIZE_MB` (default 10; 0 = unlimited)
- `ALLOW_XUNLEI_ENABLED`
- `ALLOW_PAN123_ENABLED`
- `ALLOW_CAIYUN_ENABLED`
- `ALLOW_GUANGYA_ENABLED`
- `ALLOW_ALIYUN_ENABLED`

Panel/security/scheduler keys:

- `PANEL_AUTO_RUN_ENABLED`
- `PANEL_AUTO_RUN_INTERVAL_MINUTES`
- `PANEL_TOTAL_TIMEOUT_SECONDS`
- `PANEL_TEST_MODE_ENABLED`
- `PANEL_SESSION_SECRET`
- `PANEL_ADMIN_USERNAME`
- `PANEL_ADMIN_PASSWORD` (legacy plain-text fallback)
- `PANEL_ADMIN_PASSWORD_HASH` (PBKDF2-SHA256 primary)
- `PANEL_LOGIN_MAX_FAILURES`
- `PANEL_LOGIN_WINDOW_SECONDS`
- `PANEL_LOGIN_LOCK_SECONDS`
- `PANEL_RSS_ENABLED`
- `PANEL_RSS_TOKEN`
- `PANEL_RSS_ITEM_LIMIT`

## 6) Web Panel Route Map (high-level)

- Auth and session:
  - `GET/POST /login`, `GET /logout`
- Main pages:
  - `GET /` dashboard
  - `GET /setup` initialization and session management
  - `GET /forward-settings` source/target/filter/checkpoint settings
  - `GET /plan-backup` scheduler + backups
- Operations:
  - `POST /run`, `POST /run/stop`
  - `POST /setup/save`
  - `POST /setup/admin-credentials-save` (requires current password)
  - `POST /forward-settings/resolve`
  - `POST /forward-settings/save`
  - `POST /forward-settings/checkpoints/upsert`
  - `POST /forward-settings/checkpoints/batch-save`
  - `POST /forward-settings/checkpoints/delete`
  - `POST /session/upload`, `POST /session/delete`
  - backup create/download/delete/restore endpoints
- APIs:
  - `GET /rss/{token}.xml` tokenized RSS feed; returns valid XML from live refresh, cached XML, or an empty fallback
  - `GET /rss-media/{token}/{filename}` tokenized cached RSS media file
  - `GET /api/status`
  - `GET /api/logs/tail`
  - `POST /api/logs/clear`
  - `GET /health`

## 7) Forwarding Pipeline (`web_panel/app/forwarder_service.py`)

`run_forwarder_once()` flow:

1. Validate required config and active source CID list
2. Enforce lock file (`forwarder.lock`), which stores `<boot_id>:<pid>`; a stale
   lock (other instance, legacy bare PID, malformed) is cleared and the run
   proceeds
3. Open Telethon client with `t2rss.session`
4. If dedup enabled, pre-clean destination recent messages by Quark link
5. Pull new messages from each source by DB checkpoint (`min_id=last_id`). A
   source that fails to fetch is logged, recorded in `fetch_failed_channels` and
   skipped — it never aborts the remaining sources
6. Merge and sort by message date
7. If dedup enabled:
   - Optional pre-resolve via Bot for trigger messages (see section 8)
   - Stage 1: dedup repeated links within current batch
   - Stage 2: skip links already found in destination history cache
8. Forward remaining messages. Per-message gates run in this order:
   - keyword / user blacklist
   - restricted netdisk providers (see section 8) -> `skipped_restricted_provider`
   - `MAX_VIDEO_SIZE_MB` check, read from Telegram metadata **before** any
     download -> `skipped_large_video`
   - media download, wrapped in a 180s `asyncio.wait_for`
     (`MEDIA_DOWNLOAD_TIMEOUT_SECONDS`) -> `skipped_media_timeout`
   - send with retry, then delete the temp file in `finally`
9. Checkpoint update behavior:
   - Normal success: update to `latest_ids_map` (max fetched per source)
   - Cancel/timeout/error: partial update to `forwarded_ids_map`
   - `forwarded_ids_map` advances for **every outcome except `error`** — a
     deliberately skipped message counts as progress. Only genuine send failures
     hold position so they are retried. Test mode advances nothing.
   - Every write is then clamped by `_clamp_checkpoints_below_failures()` to
     `oldest_failed_id - 1` per channel, so a later success in the same batch
     cannot drag the checkpoint past an earlier failure. Held-back channels are
     reported in `stats.checkpoint_held_back_channels`.
10. Remove lock in `finally`

## 8) Dedup + Bot Link Expansion Rules

Current dedup key target priority: **quark > 115 > baidu > uc**.

A message yields exactly ONE dedup key — the highest-priority provider present.
Links are read from message text, `MessageEntityTextUrl` blue hyperlinks, button
URLs, and bot-resolved URLs. Query strings and fragments (`?pwd=`, `?password=`,
`?public=1`, `#访问码：...`) are stripped from the key.

| Priority | Provider | Matched | Normalized key | Toggle |
|---|---|---|---|---|
| 1 | Quark | `pan.quark.cn/s/<token>` | unchanged | always on |
| 2 | 115 | `115cdn.com/s/<token>`, `hdhive.com/resource/115/<token>` | kept as separate keys | `DEDUPLICATION_115_ENABLED` |
| 3 | Baidu | `pan.baidu.com/s/<token>`, `pan.baidu.com/share/init?surl=<token>` | `/s/<token>`; `surl=X` folds to `/s/1X` | `DEDUPLICATION_BAIDU_ENABLED` |
| 4 | UC | `drive.uc.cn/s/<token>`, `fast.uc.cn/s/<token>` | collapsed to `drive.uc.cn/s/<token>` | `DEDUPLICATION_UC_ENABLED` |

### Restricted netdisk providers (not forwarded by default)

These are a *forwarding* filter, independent of dedup. All default to `false`:

| Provider | Matched | Toggle |
|---|---|---|
| 迅雷 | `pan.xunlei.com/s/` | `ALLOW_XUNLEI_ENABLED` |
| 123网盘 | `123pan.com/s/`, `123<3 digits>.com/s/` (domain rotates: 123684/123865/123912) | `ALLOW_PAN123_ENABLED` |
| 移动云盘 | `yun.139.com/shareweb/#/w/i/` | `ALLOW_CAIYUN_ENABLED` |
| 光鸭云盘 | `guangyapan.com/s/` | `ALLOW_GUANGYA_ENABLED` |
| 阿里云盘 | `alipan.com/s/`, `aliyundrive.com/s/` | `ALLOW_ALIYUN_ENABLED` |

A message is skipped (`skipped_restricted_provider`) **only when every netdisk
link it carries belongs to a disabled restricted provider**. Messages that also
contain a quark/115/baidu/uc link — or a link from an enabled restricted
provider — still forward, and link-free messages are unaffected.

`has_allowed_netdisk_link()` deliberately evaluates with all dedup toggles
forced ON, so narrowing *dedup* scope can never change *forwarding* eligibility.

When `DEDUPLICATION_ENABLED=true`:

- Destination pre-clean dedup runs on last `DEDUPLICATION_CACHE_SIZE` destination messages.
- Intra-run dedup and destination-history dedup both apply.
- Dedup link extraction checks message text, `MessageEntityTextUrl` blue hyperlinks, and button URLs.
- For messages containing trigger phrase `点击获取夸克链接`:
  - System extracts bot jump links from text/entities/buttons (`t.me` or `tg://resolve`)
  - Sends `/start` (with `start`/`startapp` payload if present) in a conversation
  - Extracts URL from bot reply text/buttons (prefers Quark URL)
  - Uses that resolved Quark link **before dedup filtering** (scenario-7 fix)
  - Replaces `点击获取夸克链接` in outbound text with resolved URL
  - Caches bot result by `bot + start payload` inside the run

Important behavior notes:

- In test mode, no real forwarding and no checkpoint updates.
- Bot expansion is not pre-run dedup in test mode (no real side-effect interactions are executed).
- Dedup scope still depends on `DEDUPLICATION_CACHE_SIZE` for destination history visibility.

## 9) Forward Settings UI Semantics

- Source-of-truth for source rows after save is `sources_input` + parsed tokens.
- CID table row deletion in UI removes row and synchronizes textarea token list.
- If a source is removed from textarea and saved, it should not reappear.
- `CHANNEL_SOURCES_JSON` stores row-level source/cid/enabled/status/error metadata.
- Enabled source channels derive `CHANNEL_IDS` for runtime forwarding.

## 10) Setup Page Semantics

- Admin credentials are edited through a separate dialog and endpoint:
  - `POST /setup/admin-credentials-save`
  - Requires `PANEL_ADMIN_OLD_PASSWORD`
  - Supports username-only, password-only, or both
- General setup save (`/setup/save`) does not modify admin credentials.
- Session upload accepts any `.session` filename and always saves as `t2rss.session`.

## 11) Security and Secrets

- Never print or persist plaintext secrets to logs.
- `PANEL_ADMIN_PASSWORD_HASH` is preferred; plaintext `PANEL_ADMIN_PASSWORD` is legacy fallback only.
- `PANEL_SESSION_SECRET` should be stable in production (avoid temporary process-only secret).
- Login lockout controlled by:
  - `PANEL_LOGIN_MAX_FAILURES`
  - `PANEL_LOGIN_WINDOW_SECONDS`
  - `PANEL_LOGIN_LOCK_SECONDS`

## 12) Backup/Restore Safety

- Restore is blocked when runner is active.
- Restore auto-creates rollback backup (`pre_restore_auto_*`) first.
- Backup path validation rejects traversal and non-zip invalid names.
- Restore keeps backups directory itself out of destructive overwrite path.

## 13) Development Invariants

When editing code, preserve these guarantees unless intentionally migrating behavior:

- Single-instance lock behavior. The lock stores `<boot_id>:<pid>`; a lock from
  another process instance (or the legacy bare-PID format) is stale and is
  cleared automatically. Never reduce the guard to a bare `lock_file.exists()`:
  inside a container the app is always PID 1, so a lock left by a container that
  died mid-run wedges the forwarder forever.
- One unreachable source must never abort the cycle. The per-channel fetch is
  wrapped in try/except (`CancelledError` still propagates) and failures are
  recorded in `stats.fetch_failed_channels`.
- Checkpoint consistency and monotonic progression semantics
- Skipped messages advance the checkpoint; only `error` holds position. Reverting
  this reintroduces the self-perpetuating loop where a permanently-skipped
  message is re-fetched on every run forever.
- Every checkpoint write goes through `_clamp_checkpoints_below_failures()`, so
  no write can advance past a message that failed to send. Writing
  `latest_ids_map` (or any bare high-water mark) directly makes a failed message
  be skipped forever, which defeats the rule above.
- Every network await that can block indefinitely (notably `download_media`)
  stays bounded by a timeout, so one stalled item cannot consume the whole
  `PANEL_TOTAL_TIMEOUT_SECONDS` budget.
- Media temp file cleanup in `finally`
- Non-blocking async path in forwarding loops
- Clear skip-reason metrics and logs
- Timeout/cancel `run_history` records carry the run's real stats (via the
  `stats_sink` shared dict), not a bare status marker
- Backward-safe startup migrations (legacy session / txt checkpoints)

If changing dedup/filter behavior:

- Explicitly document stage order impacts
- Verify stats counters remain meaningful (`skipped_*`, `after_*`)
- Verify checkpoint update semantics on success vs cancel/error

## 14) Local Validation Checklist (no test suite exists)

Run these sanity checks after non-trivial edits:

```bash
python -m compileall web_panel/app web_panel/tools
python -c "from pathlib import Path; from jinja2 import Environment, FileSystemLoader; env=Environment(loader=FileSystemLoader('web_panel/app/templates')); [env.get_template(p.name) for p in Path('web_panel/app/templates').glob('*.html')]; print('ok')"
```

For container verification:

```bash
cd web_panel
docker compose up -d --build
curl http://127.0.0.1:8080/health
```

## 15) Known Drift / Compatibility Notes

### `TEXT_REPLACEMENT_REGEX` is fragile — read before touching it

**Fixed in `save_raw_config` (config_store.py).** `load_raw_config()` decodes an
escaped `\n` into a real newline, but the old `save_raw_config()` re-encoded it
only for keys present in the submitted dict. A multiline value that was merely
carried over unchanged got written as several physical lines in `config.env`,
and everything after the first line was silently dropped on the next read. So
**any** save — setup/RSS, scheduler, admin password — destroyed all but the
first regex rule, even though those forms never touch the field. Encoding now
happens for every key on the way out; see
`web_panel/tests/test_config_roundtrip.py`.

Remaining constraints:

- `web_panel/data/` is gitignored, so the live rules have **no history**.
  `web_panel/config_presets/text_replacement_rules.json` is the source of truth;
  update it whenever rules change, and restore with
  `web_panel/scripts/restore_text_rules.py` (verifies before writing).
- Never put a literal `\n` inside a rule. Rules are persisted joined on `\n`, so
  a literal `\n` decodes into a real line break and splits one rule into invalid
  fragments. Use `\s*` / `\s+` for whitespace and newlines.
- Rules must be verified **after a save/reload round-trip**, not just after
  compiling — a rule can compile fine and still not survive persistence.
- Promo footers vary segment by segment (`来自` / `频道` / `群组` / `投稿` /
  `资源搜索` / `反馈合作` appear in different combinations). Prefer standalone
  per-label rules over one rule that hard-codes a fixed multi-segment sequence,
  and require an `@handle` so ordinary prose mentioning 频道/群组 is not deleted.
- Some promo URLs are only visible **after entity materialization** (a 1-char
  hidden hyperlink becomes a bare URL glued to the text). The end-anchored
  `t.me|telegram.me|link3.cc` rule must stay last in the list.
- `POST /plan-backup/cleanup` does **not** touch `config.env` (it only clears
  downloads, `tmp_*` dirs, a stale lock, session sidecars and uploaded restore
  zips).
- A backup taken while the rules are broken will faithfully capture the broken
  state; restoring it re-applies the damage. Check the rule count before
  trusting a restore.

- `cli` branch legacy scripts still use `session_name.session` and text-file checkpoints.
- `main` web panel uses `t2rss.session` and SQLite checkpoints.
- Root `README.md` on `main` documents Web panel mode; legacy usage lives in `cli` branch.

## 16) Suggested Next Engineering Steps

- Add automated tests for:
  - source parsing and save semantics
  - dedup stages (including bot-expanded link cases)
  - checkpoint update paths (success/cancel/error)
- Add structured metric endpoints for observability.
- Consider isolating bot-conversation logic behind a dedicated adapter for easier mocking.
