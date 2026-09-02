r"""Regression tests for two forwarder data-loss bugs found in the system audit.

A) One unreachable source channel aborted the ENTIRE run.
   `[msg async for msg in client.iter_messages(...)]` had no try/except, so a
   private/banned/deleted channel raised out of the per-channel loop and every
   channel after it was never polled — the whole cycle forwarded nothing.

B) A message that failed to send was skipped forever.
   The send loop tracked outcomes per message, but the success path wrote
   `latest_ids_map` (max fetched id) to the checkpoint, jumping over the failure.
   Even the partial paths used a single high-water mark, so a later success in
   the same batch dragged the checkpoint past an earlier failure.
   Now every checkpoint write is clamped below the oldest failed id per channel.

Run inside the container:
    docker cp web_panel/tests/test_forwarder_resilience.py t2rss-web-panel:/app/f.py
    docker exec -w /app t2rss-web-panel python f.py
"""
import asyncio

from app.forwarder_service import _clamp_checkpoints_below_failures

failures = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'} {label}: {actual!r}")
    if not ok:
        failures.append(f"{label}: got {actual!r}, want {expected!r}")


print("=" * 74)
print("A. a failing channel must not abort the cycle")
print("=" * 74)


class FakeClient:
    def __init__(self, broken):
        self.broken = broken
        self.visited = []

    def iter_messages(self, cid, min_id=0):
        self.visited.append(cid)

        async def gen():
            if cid in self.broken:
                raise RuntimeError("ChannelPrivateError")
            for i in range(3):
                yield f"m{cid}_{i}"

        return gen()


async def fetch_loop(client, cids, stats):
    """Mirrors the guarded loop in run_forwarder_once()."""
    collected = []
    for cid in cids:
        try:
            msgs = [m async for m in client.iter_messages(cid, min_id=0)]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            stats["per_channel_fetched"][str(cid)] = 0
            stats["fetch_failed_channels"][str(cid)] = str(exc)
            continue
        stats["per_channel_fetched"][str(cid)] = len(msgs)
        collected.extend(msgs)
    return collected


CIDS = [111, 222, 333, 444]
stats = {"per_channel_fetched": {}, "fetch_failed_channels": {}}
client = FakeClient(broken={222})
collected = asyncio.run(fetch_loop(client, CIDS, stats))
check("every channel is attempted", client.visited, CIDS)
check("healthy channels still collected", len(collected), 9)
check("failure recorded", list(stats["fetch_failed_channels"]), ["222"])
check("failed channel counted as 0", stats["per_channel_fetched"]["222"], 0)

stats2 = {"per_channel_fetched": {}, "fetch_failed_channels": {}}
client2 = FakeClient(broken={111, 222, 333, 444})
collected2 = asyncio.run(fetch_loop(client2, CIDS, stats2))
check("all channels broken -> no crash", collected2, [])
check("all four recorded as failed", len(stats2["fetch_failed_channels"]), 4)

# Cancellation must still propagate (the run is being aborted on purpose).
class CancelClient:
    def iter_messages(self, cid, min_id=0):
        async def gen():
            raise asyncio.CancelledError()
            yield  # pragma: no cover
        return gen()


try:
    asyncio.run(fetch_loop(CancelClient(), [1], {"per_channel_fetched": {}, "fetch_failed_channels": {}}))
    check("CancelledError propagates", "swallowed", "propagated")
except asyncio.CancelledError:
    check("CancelledError propagates", "propagated", "propagated")

print()
print("=" * 74)
print("B. checkpoints must never jump past a failed send")
print("=" * 74)

# Mirrors the send loop's bookkeeping.
def simulate(outcomes, test_mode=False):
    fwd, failed = {}, {}
    for cid, mid, reason in outcomes:
        if test_mode:
            continue
        if reason == "error":
            cur = failed.get(cid)
            if cur is None or mid < cur:
                failed[cid] = mid
        elif mid > fwd.get(cid, 0):
            fwd[cid] = mid
    return fwd, failed


CID = 777
# Mid-batch failure: 12 fails, 13/14 succeed afterwards.
fwd, failed = simulate([(CID, 10, "forwarded"), (CID, 11, "forwarded"),
                        (CID, 12, "error"),
                        (CID, 13, "forwarded"), (CID, 14, "forwarded")])
check("high-water mark alone would pass the failure", fwd[CID], 14)
check("oldest failure tracked", failed[CID], 12)

latest = {CID: 14}   # max fetched, what the success path used to write
eff = _clamp_checkpoints_below_failures(latest, failed)
check("success path clamped below the failure", eff[CID], 11)
check("message 12 is re-fetched next run", 12 >= eff[CID] + 1, True)

# Partial (cancel/exception) path uses the same clamp.
eff_partial = _clamp_checkpoints_below_failures(fwd, failed)
check("partial path clamped too", eff_partial[CID], 11)

# Failure is the newest message.
fwd2, failed2 = simulate([(CID, 20, "forwarded"), (CID, 21, "forwarded"), (CID, 22, "error")])
eff2 = _clamp_checkpoints_below_failures({CID: 22}, failed2)
check("newest-message failure held back", eff2[CID], 21)

# Multiple failures: clamp to the OLDEST.
fwd3, failed3 = simulate([(CID, 30, "error"), (CID, 31, "forwarded"), (CID, 32, "error")])
check("oldest of several failures wins", failed3[CID], 30)
eff3 = _clamp_checkpoints_below_failures({CID: 32}, failed3)
check("clamped below the oldest failure", eff3[CID], 29)

# Channels without failures are untouched.
A, B = 100, 200
failed_mixed = {A: 55}
eff4 = _clamp_checkpoints_below_failures({A: 60, B: 70}, failed_mixed)
check("failing channel clamped", eff4[A], 54)
check("healthy channel unaffected", eff4[B], 70)

# Skips still count as progress (the earlier fix must not regress).
fwd5, failed5 = simulate([(CID, 40, "skipped_large_video"),
                          (CID, 41, "skipped_restricted_provider"),
                          (CID, 42, "skipped_keyword")])
check("no failures recorded for skips", failed5, {})
check("skips advance the checkpoint", fwd5[CID], 42)
check("no clamping without failures", _clamp_checkpoints_below_failures({CID: 42}, failed5)[CID], 42)

# A failure at the very first message must not produce a negative checkpoint.
eff6 = _clamp_checkpoints_below_failures({CID: 5}, {CID: 1})
check("first-message failure yields no bogus entry", eff6.get(CID), None)

# Test mode records nothing at all.
fwd7, failed7 = simulate([(CID, 50, "forwarded"), (CID, 51, "error")], test_mode=True)
check("test mode tracks nothing", (fwd7, failed7), ({}, {}))

print()
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("ALL CHECKS PASSED")
