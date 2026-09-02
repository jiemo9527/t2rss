r"""Regression test: a lock left by a dead container must not wedge the forwarder.

Real incident: deploying a new image while a run was in flight left
state/forwarder.lock holding "1" (the app is PID 1 inside the container). The
run guard only checked `lock_file.exists()`, and PID 1 in the NEW container is
always alive, so every subsequent run returned "skipped" forever. Four
consecutive cycles were lost until the file was deleted by hand.

Fix: the lock stores a per-process boot id, so a lock from any other instance
(or in the old bare-PID format) is recognised as stale and cleared.

Run inside the container:
    docker cp web_panel/tests/test_lock_staleness.py t2rss-web-panel:/app/l.py
    docker exec -w /app t2rss-web-panel python l.py
"""
import tempfile
from pathlib import Path

from app.forwarder_service import _BOOT_ID, _lock_payload, _stale_lock_reason

failures = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'} {label}: {actual!r}")
    if not ok:
        failures.append(f"{label}: got {actual!r}, want {expected!r}")


tmp = Path(tempfile.mkdtemp())
lock = tmp / "forwarder.lock"

print("=== the lock this process just took is NOT stale ===")
lock.write_text(_lock_payload(), encoding="utf-8")
check("own live lock is respected", _stale_lock_reason(lock), None)
check("payload carries this boot id", _lock_payload().startswith(_BOOT_ID), True)

print("\n=== the exact incident: old container's bare PID 1 ===")
lock.write_text("1", encoding="utf-8")
reason = _stale_lock_reason(lock)
check("bare PID lock detected as stale", reason is not None, True)
print(f"      reason: {reason}")

print("\n=== a lock from a different container instance ===")
lock.write_text("deadbeefdeadbeefdeadbeefdeadbeef:1", encoding="utf-8")
reason = _stale_lock_reason(lock)
check("other boot id detected as stale", reason is not None, True)
print(f"      reason: {reason}")

print("\n=== malformed / empty locks are stale, never fatal ===")
lock.write_text("", encoding="utf-8")
check("empty lock is stale", _stale_lock_reason(lock) is not None, True)
lock.write_text("   \n  ", encoding="utf-8")
check("whitespace lock is stale", _stale_lock_reason(lock) is not None, True)
lock.write_text("garbage-without-separator", encoding="utf-8")
check("garbage lock is stale", _stale_lock_reason(lock) is not None, True)

print("\n=== same boot id but a different PID is still ours ===")
# Same process instance (e.g. a forked worker) -- must not be cleared.
lock.write_text(f"{_BOOT_ID}:99999", encoding="utf-8")
check("same boot id respected", _stale_lock_reason(lock), None)

print("\n=== round-trip: write then evaluate ===")
lock.write_text(_lock_payload(), encoding="utf-8")
check("re-read own payload", _stale_lock_reason(lock), None)
boot, _, pid = lock.read_text(encoding="utf-8").strip().partition(":")
check("payload is boot_id:pid", (boot == _BOOT_ID, pid.isdigit()), (True, True))

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
