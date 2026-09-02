r"""Restore TEXT_REPLACEMENT_REGEX from the versioned preset, with verification.

The panel's forward-settings form overwrites the regex textarea wholesale, so a
save made with a partially-filled field wipes the rule set (this has happened
twice). web_panel/data/ is gitignored, so there is no history to recover from —
hence the checked-in preset at web_panel/config_presets/text_replacement_rules.json.

Usage (inside the running container):
    docker cp web_panel/config_presets/text_replacement_rules.json \
        t2rss-web-panel:/app/rules.json
    docker cp web_panel/scripts/restore_text_rules.py t2rss-web-panel:/app/rt.py
    docker exec -w /app t2rss-web-panel python rt.py rules.json

Refuses to write unless every rule compiles AND survives a simulated
ConfigStore encode/decode round-trip (guards against literal \n in a rule).
Pass --dry-run to verify without writing.
"""
import json
import logging
import sys
from pathlib import Path

from app.config_store import ConfigStore
from app.forwarder_service import _apply_text_replacements, _compile_text_replacement_regex

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("restore")

preset_path = Path(sys.argv[1] if len(sys.argv) > 1 else "rules.json")
dry_run = "--dry-run" in sys.argv

rules = [r.strip() for r in json.loads(preset_path.read_text(encoding="utf-8"))["rules"] if r.strip()]
print(f"preset rules: {len(rules)}")

# 1. every rule must compile
compiled = _compile_text_replacement_regex("\n".join(rules), logger)
assert len(compiled) == len(rules), f"only {len(compiled)}/{len(rules)} rules compile"

# 2. must survive the config encode/decode round-trip (catches literal \n)
encoded = "\n".join(rules).replace("\n", r"\n")
decoded = encoded.replace(r"\r\n", r"\n").replace(r"\n", "\n")
survived = [x.strip() for x in decoded.splitlines() if x.strip()]
assert len(survived) == len(rules), (
    f"round-trip would split rules ({len(rules)} -> {len(survived)}); "
    "a rule contains a literal \\n"
)
assert len(_compile_text_replacement_regex(decoded, logger)) == len(rules)
print("compile + round-trip OK")

# 3. behavioural check against real footers seen in the source channels
BODY = (
    "名称：某剧 4K 更新至10集\n.\n"
    "夸克：https://pan.quark.cn/s/KEEPquark\n"
    "百度：https://pan.baidu.com/s/1KEEPbaidu?pwd=abcd\n"
    "UC：https://drive.uc.cn/s/KEEPuc?public=1\n"
    "迅雷：https://pan.xunlei.com/s/KEEPxunlei?pwd=efgh\n\n"
    "🏷 标签：#某剧 #多多影音 #ucquark #baidu"
)
LINKS = ("KEEPquark", "KEEPbaidu", "KEEPuc", "KEEPxunlei")
DIRT = ("来自", "频道：", "群组", "资源搜索", "反馈合作", "投稿", "防失联",
        "网盘专搜", "社工导航", "版权反馈", "BooksRealm", "Twitter", "私聊")

CASES = {
    "ucquark full": "\n🎉 来自：大风车\n📢 频道：@ucquark  防失联 @yydsys\n👥 群组：@uckuake\n资源搜索：@yydsysbot\n反馈合作：@yydsys_bot",
    "no-channel variant": "\n🎉 来自：大风车\n👥 群组：@uckuake\n资源搜索：@yydsysbot\n反馈合作：@yydsys_bot",
    "quark share group": "\n📢 频道：资源频道\n👥 群组：@Quark_Share_Group\n🤖 投稿：@QuarkRobot",
    "dangling paren": "🤖 投稿：@QuarkRobot (",
    "dmca block": "\n⚠️ 版权：版权反馈/DMCA (https://t.me/YunpanTip/708)\n📢 频道 (https://t.me/BaiduCloudDisk) 👥 群组 (https://t.me/yunpangroup) 🔍 投稿/搜索 (https://t.me/kejiqubot)",
    "foreign/domestic": "\n📢 国外影视发布频道：@tgsearchers7\n📢 此频道只发国内影视：@Baidu_Netdisk",
    "comment search": "\n⬇️【评论区可搜索】 | 🔍网盘专搜",
    "shegong nav": "\n🧰社工导航 | 🛍全国抵押车\n⬇️【评论区可搜索】 | 🔍网盘专搜",
    "trailing promo url": "https://t.me/zaihuapd/6",
}

failures = []
for label, tail in CASES.items():
    out, _, hits = _apply_text_replacements(BODY + tail, [], compiled)
    dirty = [d for d in DIRT if d in out]
    lost = [l for l in LINKS if l not in out]
    dangle = out.rstrip().endswith("(")
    ok = not dirty and not lost and not dangle and hits >= 1
    print(f"{'PASS' if ok else 'FAIL'} {label:20s} hits={hits}")
    if not ok:
        failures.append((label, dirty, lost, out))

out, _, hits = _apply_text_replacements(BODY, [], compiled)
ok = out.strip() == BODY.strip() and hits == 0
print(f"{'PASS' if ok else 'FAIL'} clean untouched      hits={hits}")
if not ok:
    failures.append(("clean", [], [], out))

prose = "描述：这个频道很好，群组也活跃\n夸克：https://pan.quark.cn/s/KEEPprose"
out, _, _ = _apply_text_replacements(prose, [], compiled)
ok = "这个频道很好，群组也活跃" in out and "KEEPprose" in out
print(f"{'PASS' if ok else 'FAIL'} prose preserved")
if not ok:
    failures.append(("prose", [], [], out))

if failures:
    print(f"\n{len(failures)} FAILURE(S) — NOT WRITING")
    for label, dirty, lost, out in failures:
        print(f"  {label}: leftover={dirty} lost_links={lost}\n    {out!r}")
    raise SystemExit(1)

if dry_run:
    print("\nDRY RUN — config not modified")
    raise SystemExit(0)

store = ConfigStore(Path("/app/data"))
before = [x for x in store.load_raw_config().get("TEXT_REPLACEMENT_REGEX", "").splitlines() if x.strip()]
store.save_raw_config({"TEXT_REPLACEMENT_REGEX": "\n".join(rules)})

after = [x.strip() for x in store.load_raw_config()["TEXT_REPLACEMENT_REGEX"].splitlines() if x.strip()]
recompiled = _compile_text_replacement_regex("\n".join(after), logger)
assert len(after) == len(recompiled) == len(rules), (len(after), len(recompiled), len(rules))
out, _, _ = _apply_text_replacements(BODY + CASES["ucquark full"], [], recompiled)
assert not any(d in out for d in DIRT) and all(l in out for l in LINKS)
print(f"\nRESTORED  {len(before)} -> {len(after)} rules  (verified after reload)")
