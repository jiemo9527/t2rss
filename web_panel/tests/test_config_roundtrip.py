r"""Regression test: an unrelated save must never destroy multiline config values.

Reproduces the real incident: the user opened the setup page, changed only
PANEL_RSS_ITEM_LIMIT and saved. That form does not submit
TEXT_REPLACEMENT_REGEX, yet 19 regex rules collapsed to 1.

Cause: load_raw_config() decodes escaped "\n" into real newlines, but
save_raw_config() re-encoded them only for keys present in the submitted dict.
A carried-over multiline value was therefore written as several physical lines
in config.env; dotenv keeps only the first on the next read and silently drops
the rest.

Run inside the container (Telethon is not installed locally):
    docker cp web_panel/tests/test_config_roundtrip.py t2rss-web-panel:/app/t.py
    docker exec -w /app t2rss-web-panel python t.py
"""
import tempfile
from pathlib import Path

from app.config_store import ConfigStore

failures = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'} {label}: {actual!r}")
    if not ok:
        failures.append(f"{label}: got {actual!r}, want {expected!r}")


def fresh_store(rules):
    d = Path(tempfile.mkdtemp())
    store = ConfigStore(d)
    store.save_raw_config(
        {
            "TEXT_REPLACEMENT_REGEX": "\n".join(rules),
            "TEXT_REPLACEMENT_TERMS": "#剧集,#动漫",
            "PANEL_RSS_ITEM_LIMIT": "500",
        }
    )
    return store


def rules_of(store):
    value = store.load_raw_config().get("TEXT_REPLACEMENT_REGEX", "")
    return [line for line in value.splitlines() if line.strip()]


RULES = [r"\s*🎉\s*来自：\S+\s*", r"\s*👥\s*群组：@\S+\s*", r"\s*🤖\s*投稿：@\S+\s*"]

print("=== 1. a save that never mentions the field ===")
store = fresh_store(RULES)
check("initial rule count", len(rules_of(store)), 3)
store.save_raw_config({"PANEL_RSS_ITEM_LIMIT": "600"})
check("after RSS-only save", len(rules_of(store)), 3)
check("rules unchanged", rules_of(store), RULES)
check("RSS value applied", store.load_raw_config()["PANEL_RSS_ITEM_LIMIT"], "600")

print("\n=== 2. repeated unrelated saves do not erode the list ===")
for i in range(5):
    store.save_raw_config({"PANEL_AUTO_RUN_INTERVAL_MINUTES": str(2 + i)})
check("after 5 more saves", len(rules_of(store)), 3)

print("\n=== 3. config.env stays one physical line per key ===")
text = (store.data_dir / "config.env").read_text(encoding="utf-8")
stray = [ln for ln in text.splitlines() if ln and "=" not in ln]
check("lines without '='", stray, [])
key_lines = [ln for ln in text.splitlines() if ln.startswith("TEXT_REPLACEMENT_REGEX")]
check("regex key appears once", len(key_lines), 1)
check("stored with escaped separators", key_lines[0].count("\\n"), 2)

print("\n=== 4. the field itself can still be edited ===")
store.save_raw_config({"TEXT_REPLACEMENT_REGEX": "\n".join(RULES + [r"\s*新规则\s*"])})
check("grew to 4", len(rules_of(store)), 4)
store.save_raw_config({"TEXT_REPLACEMENT_REGEX": RULES[0]})
check("shrink is still honoured", len(rules_of(store)), 1)

print("\n=== 5. single-line values are untouched ===")
store2 = fresh_store(RULES)
store2.save_raw_config({"KEYWORD_BLACKLIST": "广告,推广"})
check("blacklist saved", store2.load_raw_config()["KEYWORD_BLACKLIST"], "广告,推广")
check("rules survived", len(rules_of(store2)), 3)

print("\n=== 6. CRLF input is normalized, not split ===")
store3 = fresh_store([])
store3.save_raw_config({"TEXT_REPLACEMENT_REGEX": "rule_A\r\nrule_B\rrule_C"})
check("CRLF/CR normalized", rules_of(store3), ["rule_A", "rule_B", "rule_C"])
store3.save_raw_config({"PANEL_RSS_ITEM_LIMIT": "700"})
check("still 3 after unrelated save", len(rules_of(store3)), 3)

print("\n=== 7. a newline inside a single-line key cannot inject a line ===")
store4 = fresh_store(RULES)
store4.save_raw_config({"KEYWORD_BLACKLIST": "aaa\nPANEL_ADMIN_USERNAME=hacked"})
text4 = (store4.data_dir / "config.env").read_text(encoding="utf-8")
injected = [ln for ln in text4.splitlines() if ln.startswith("PANEL_ADMIN_USERNAME=hacked")]
check("no injected line", injected, [])
check("admin username intact", store4.load_raw_config()["PANEL_ADMIN_USERNAME"], "admin")

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
