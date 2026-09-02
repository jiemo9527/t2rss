r"""System-wide audit for data-loss bugs of the same family as the config one.

The fixed bug was an asymmetric encode/decode round-trip: load_raw_config()
decoded escaped newlines, save_raw_config() re-encoded only the submitted keys,
so a carried-over multiline value was written as several physical lines and all
but the first was dropped on the next read.

This exercises every persistence path with the same shape of question:
  "if I write X and read it back, do I get X — including when the value contains
   awkward characters, and including when an unrelated write happens in between?"

Run inside the container:
    docker cp web_panel/tests/test_persistence_audit.py t2rss-web-panel:/app/a.py
    docker exec -w /app t2rss-web-panel python a.py
"""
import json
import sqlite3
import tempfile
from pathlib import Path

from app.auth_security import LoginGuardStore, build_password_hash, verify_password
from app.checkpoint_store import ChannelCheckpointStore
from app.config_store import ALL_ENV_KEYS, ConfigStore, parse_channel_sources
from app.history_store import RunHistoryStore

failures = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"      got:  {actual!r}")
        print(f"      want: {expected!r}")
        failures.append(label)


def new_dir():
    return Path(tempfile.mkdtemp())


print("=" * 74)
print("1. config round-trip for EVERY key (the fixed bug's family)")
print("=" * 74)
# Every key gets an awkward value; then an unrelated save must preserve all.
store = ConfigStore(new_dir())
seed = {}
for i, key in enumerate(ALL_ENV_KEYS):
    seed[key] = f"val{i}_a"
seed["TEXT_REPLACEMENT_REGEX"] = "ruleA\nruleB\nruleC"
seed["CHANNEL_SOURCES_JSON"] = json.dumps(
    [{"source": "@a", "cid": 1, "enabled": True, "status": "ok", "error": ""}],
    ensure_ascii=False, separators=(",", ":"),
)
seed["KEYWORD_BLACKLIST"] = "广告,推广"
store.save_raw_config(seed)
before = store.load_raw_config()

store.save_raw_config({"PANEL_RSS_ITEM_LIMIT": "999"})
after = store.load_raw_config()

drifted = {k: (before[k], after[k]) for k in ALL_ENV_KEYS
           if k != "PANEL_RSS_ITEM_LIMIT" and before.get(k) != after.get(k)}
check("no key drifts after an unrelated save", drifted, {})
check("regex list intact", len([x for x in after["TEXT_REPLACEMENT_REGEX"].splitlines() if x]), 3)

# 10 consecutive unrelated saves must not erode anything.
for i in range(10):
    store.save_raw_config({"PANEL_AUTO_RUN_INTERVAL_MINUTES": str(i + 2)})
final = store.load_raw_config()
drifted2 = {k: (before[k], final[k]) for k in ALL_ENV_KEYS
            if k not in {"PANEL_RSS_ITEM_LIMIT", "PANEL_AUTO_RUN_INTERVAL_MINUTES"}
            and before.get(k) != final.get(k)}
check("no drift after 10 more saves", drifted2, {})

print()
print("=" * 74)
print("2. config.env stays parseable: one physical line per key")
print("=" * 74)
text = (store.data_dir / "config.env").read_text(encoding="utf-8")
stray = [ln for ln in text.splitlines() if ln and "=" not in ln]
check("no lines without '='", stray, [])
dupes = {}
for ln in text.splitlines():
    if "=" in ln:
        k = ln.split("=", 1)[0]
        dupes[k] = dupes.get(k, 0) + 1
check("no duplicated keys", [k for k, n in dupes.items() if n > 1], [])

print()
print("=" * 74)
print("3. injection: a newline in any value cannot forge a config line")
print("=" * 74)
s3 = ConfigStore(new_dir())
s3.save_raw_config({"PANEL_ADMIN_USERNAME": "admin"})
s3.save_raw_config({"KEYWORD_BLACKLIST": "x\nPANEL_ADMIN_PASSWORD_HASH=forged"})
check("admin hash not forged", s3.load_raw_config()["PANEL_ADMIN_PASSWORD_HASH"], "")
s3.save_raw_config({"DESTINATION_CHANNEL": "y\r\nPANEL_ADMIN_USERNAME=hacker"})
check("username not forged via CRLF", s3.load_raw_config()["PANEL_ADMIN_USERNAME"], "admin")

print()
print("=" * 74)
print("4. values with '=' and unicode survive verbatim")
print("=" * 74)
s4 = ConfigStore(new_dir())
tricky = "https://pan.baidu.com/s/1abc?pwd=9527&x=1"
s4.save_raw_config({"DESTINATION_CHANNEL": tricky, "TEXT_REPLACEMENT_TERMS": "#剧集,#动漫,🏷"})
c4 = s4.load_raw_config()
check("value containing '=' preserved", c4["DESTINATION_CHANNEL"], tricky)
check("unicode/emoji preserved", c4["TEXT_REPLACEMENT_TERMS"], "#剧集,#动漫,🏷")

print()
print("=" * 74)
print("5. CHANNEL_SOURCES_JSON survives unrelated saves (source rows)")
print("=" * 74)
s5 = ConfigStore(new_dir())
rows = [
    {"source": "@chan_a", "cid": 111, "enabled": True, "status": "ok", "error": ""},
    {"source": "@chan_b", "cid": 222, "enabled": False, "status": "ok", "error": "x"},
    {"source": "@中文频道", "cid": 333, "enabled": True, "status": "ok", "error": ""},
]
s5.save_raw_config({"CHANNEL_SOURCES_JSON": json.dumps(rows, ensure_ascii=False, separators=(",", ":"))})
s5.save_raw_config({"PANEL_RSS_ITEM_LIMIT": "600"})
parsed = parse_channel_sources(s5.load_raw_config()["CHANNEL_SOURCES_JSON"])
check("all 3 source rows survive", len(parsed), 3)
check("enabled flags survive", [r["enabled"] for r in parsed], [True, False, True])
check("unicode source survives", parsed[2]["source"], "@中文频道")

print()
print("=" * 74)
print("6. checkpoint store: monotonic semantics and isolation")
print("=" * 74)
d6 = new_dir()
cp = ChannelCheckpointStore(d6 / "panel.db")
cp.init_db()
cp.bulk_update({111: 500, 222: 900})
cp.set_last_id(111, 600)
check("set_last_id applied", cp.get_last_id(111), 600)
check("other channel untouched", cp.get_last_id(222), 900)
cp.bulk_update({111: 700})
check("bulk_update of one leaves others", (cp.get_last_id(111), cp.get_last_id(222)), (700, 900))
check("unknown channel reads 0", cp.get_last_id(999), 0)
check("delete works", (cp.delete_last_id(111), cp.get_last_id(111)), (True, 0))
try:
    cp.set_last_id(222, -1)
    check("negative last_id rejected", "accepted", "rejected")
except ValueError:
    check("negative last_id rejected", "rejected", "rejected")

print()
print("=" * 74)
print("7. run history: stats JSON round-trip, prune keeps recent")
print("=" * 74)
d7 = new_dir()
h = RunHistoryStore(d7 / "panel.db")
h.init_db()
stats = {"fetched_total": 5, "after_dedup_total": 3, "forwarded_total": 2, "error_total": 0,
         "per_channel_fetched": {"111": 4}, "note": "中文 emoji 🏷 quote\" backslash\\"}
h.add_record({"started_at": "2026-09-02 10:00:00", "finished_at": "2026-09-02 10:01:00",
              "trigger": "auto", "status": "success", "message": "ok", "stats": stats})
rec = h.list_records(limit=1)[0]
check("stats json round-trip", rec["stats"], stats)
check("denormalized counters match", (rec["fetched_total"], rec["forwarded_total"]), (5, 2))
removed = h.prune_old_records(retention_days=30)
check("recent record not pruned", (removed, len(h.list_records(limit=10))), (0, 1))

print()
print("=" * 74)
print("8. password hashing / verification")
print("=" * 74)
pw = "S3cretPassw0rd!"
ph = build_password_hash(pw)
check("correct password verifies", verify_password(pw, ph, ""), True)
check("wrong password rejected", verify_password("wrong", ph, ""), False)
check("empty password rejected", verify_password("", ph, ""), False)
check("no hash and no legacy rejects", verify_password(pw, "", ""), False)
check("malformed hash rejected", verify_password(pw, "garbage$$$", ""), False)

print()
print("=" * 74)
print("9. login guard: lock triggers, and clearing is scoped")
print("=" * 74)
d9 = new_dir()
g = LoginGuardStore(d9 / "panel.db")
g.init_db()
cfg = {"PANEL_LOGIN_MAX_FAILURES": "3", "PANEL_LOGIN_WINDOW_SECONDS": "600",
       "PANEL_LOGIN_LOCK_SECONDS": "900"}
check("starts unlocked", g.get_lock_seconds("1.2.3.4", "admin"), 0)
for _ in range(2):
    g.record_failure("1.2.3.4", "admin", cfg)
check("still unlocked below threshold", g.get_lock_seconds("1.2.3.4", "admin"), 0)
g.record_failure("1.2.3.4", "admin", cfg)
check("locked at threshold", g.get_lock_seconds("1.2.3.4", "admin") > 0, True)
check("different IP unaffected", g.get_lock_seconds("9.9.9.9", "admin"), 0)
g.clear_failures("1.2.3.4", "admin")
check("clear unlocks", g.get_lock_seconds("1.2.3.4", "admin"), 0)

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
