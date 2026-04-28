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
- `DEDUPLICATION_CACHE_SIZE`

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
  - `GET /api/status`
  - `GET /api/logs/tail`
  - `POST /api/logs/clear`
  - `GET /health`

## 7) Forwarding Pipeline (`web_panel/app/forwarder_service.py`)

`run_forwarder_once()` flow:

1. Validate required config and active source CID list
2. Enforce lock file (`forwarder.lock`)
3. Open Telethon client with `t2rss.session`
4. If dedup enabled, pre-clean destination recent messages by Quark link
5. Pull new messages from each source by DB checkpoint (`min_id=last_id`)
6. Merge and sort by message date
7. If dedup enabled:
   - Optional pre-resolve via Bot for trigger messages (see section 8)
   - Stage 1: dedup repeated links within current batch
   - Stage 2: skip links already found in destination history cache
8. Forward remaining messages (keyword/user filters, media handling, send)
9. Checkpoint update behavior:
   - Normal success: update to `latest_ids_map` (max fetched per source)
   - Cancel/error: partial update to `forwarded_ids_map` (max actually forwarded)
10. Remove lock in `finally`

## 8) Dedup + Bot Link Expansion Rules

Current dedup key target: first matching `https://pan.quark.cn/s/<token>`.

When `DEDUPLICATION_ENABLED=true`:

- Destination pre-clean dedup runs on last `DEDUPLICATION_CACHE_SIZE` destination messages.
- Intra-run dedup and destination-history dedup both apply.
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

- Single-instance lock behavior
- Checkpoint consistency and monotonic progression semantics
- Media temp file cleanup in `finally`
- Non-blocking async path in forwarding loops
- Clear skip-reason metrics and logs
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
