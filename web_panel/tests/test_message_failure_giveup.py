"""Regression: a permanently-unsendable message must not pin the checkpoint forever.

Run inside a copied app dir:  python test_message_failure_giveup.py

Background
----------
A message whose send fails holds its channel's checkpoint so it can be retried
(this is deliberate — see `_clamp_checkpoints_below_failures`). But a message
that can NEVER be sent — Telegram refuses to serve its media file, so
`download_media` raises `ValueError: Request was unsuccessful 6 time(s)` on every
attempt — used to hold that checkpoint forever. Every scheduled run then
re-fetched the whole backlog behind it, retried the same message, failed again,
and logged the same traceback. Observed in production: message 389168 of channel
2463707870 failed on 83 consecutive runs while its checkpoint sat frozen at
389167.

The fix gives each message a bounded retry budget, counted only over runs that
were otherwise healthy (they forwarded something else), after which the message
is given up on and the checkpoint is allowed past it.

These checks exercise the ledger directly; the forwarder wiring is asserted by
`test_forwarder_giveup_wiring` below, which drives the real decision logic.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.checkpoint_store import ChannelCheckpointStore  # noqa: E402
from app.forwarder_service import (  # noqa: E402
    MESSAGE_FAILURE_GIVE_UP_ATTEMPTS,
    _clamp_checkpoints_below_failures,
)

CHANNEL = 2463707870
BROKEN_MESSAGE = 389168

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def new_store(tmp):
    store = ChannelCheckpointStore(Path(tmp) / "panel.db")
    store.init_db()
    return store


def test_ledger_counts_only_confirmed_failures():
    print("[1] ledger separates confirmed failures from outage failures")
    with tempfile.TemporaryDirectory() as tmp:
        store = new_store(tmp)

        # Three runs where NOTHING forwarded (Telegram outage / FloodWait):
        # attempts accumulate but the give-up budget must not be consumed.
        for _ in range(3):
            counts = store.record_failure(CHANNEL, BROKEN_MESSAGE, confirmed=False)
        check("attempt_count counts every failure", counts["attempt_count"] == 3, counts)
        check("outage failures do not burn budget", counts["confirmed_count"] == 0, counts)

        counts = store.record_failure(CHANNEL, BROKEN_MESSAGE, confirmed=True)
        check("healthy-run failure counts", counts["confirmed_count"] == 1, counts)


def test_give_up_after_budget():
    print("[2] message is given up after the confirmed-failure budget")
    with tempfile.TemporaryDirectory() as tmp:
        store = new_store(tmp)

        for attempt in range(1, MESSAGE_FAILURE_GIVE_UP_ATTEMPTS):
            counts = store.record_failure(CHANNEL, BROKEN_MESSAGE, confirmed=True)
            check(
                f"still blocking after {attempt} confirmed failure(s)",
                counts["confirmed_count"] < MESSAGE_FAILURE_GIVE_UP_ATTEMPTS,
                counts,
            )

        counts = store.record_failure(CHANNEL, BROKEN_MESSAGE, confirmed=True)
        check(
            f"gives up at {MESSAGE_FAILURE_GIVE_UP_ATTEMPTS} confirmed failures",
            counts["confirmed_count"] >= MESSAGE_FAILURE_GIVE_UP_ATTEMPTS,
            counts,
        )


def test_success_clears_strikes():
    print("[3] a message that finally sends loses its strike count")
    with tempfile.TemporaryDirectory() as tmp:
        store = new_store(tmp)

        store.record_failure(CHANNEL, BROKEN_MESSAGE, confirmed=True)
        store.record_failure(CHANNEL, BROKEN_MESSAGE, confirmed=True)
        store.clear_failure(CHANNEL, BROKEN_MESSAGE)

        counts = store.get_failure_counts(CHANNEL)
        check("strikes cleared on success", BROKEN_MESSAGE not in counts, counts)


def test_prune_below_checkpoint():
    print("[4] ledger rows are pruned once the checkpoint passes them")
    with tempfile.TemporaryDirectory() as tmp:
        store = new_store(tmp)

        store.record_failure(CHANNEL, 100, confirmed=True)
        store.record_failure(CHANNEL, 200, confirmed=True)
        store.record_failure(CHANNEL, 300, confirmed=True)

        store.prune_failures_below({CHANNEL: 200})
        remaining = sorted(store.get_failure_counts(CHANNEL))
        check("rows at or below the checkpoint removed", remaining == [300], remaining)


def test_forwarder_giveup_wiring():
    """Replay the forwarder's post-loop decision on a real ledger.

    This mirrors run_forwarder_once: a batch where the broken message fails and
    a later message forwards fine. While the message has budget the checkpoint
    must stay clamped below it; once the budget is spent the checkpoint must
    advance past it.
    """
    print("[5] forwarder decision: blocked while budget remains, released after")
    with tempfile.TemporaryDirectory() as tmp:
        store = new_store(tmp)
        latest_ids_map = {CHANNEL: 389235}

        def run_once():
            """One scheduled cycle: broken msg errors, a later msg forwards."""
            forwarded_ids_map = {CHANNEL: 389230}
            failed_ids_map = {}
            pipeline_healthy = True  # forwarded_total > 0

            counts = store.record_failure(CHANNEL, BROKEN_MESSAGE, confirmed=pipeline_healthy)
            gave_up = counts["confirmed_count"] >= MESSAGE_FAILURE_GIVE_UP_ATTEMPTS
            if gave_up:
                if BROKEN_MESSAGE > forwarded_ids_map.get(CHANNEL, 0):
                    forwarded_ids_map[CHANNEL] = BROKEN_MESSAGE
            else:
                failed_ids_map[CHANNEL] = BROKEN_MESSAGE

            effective = _clamp_checkpoints_below_failures(latest_ids_map, failed_ids_map)
            store.bulk_update(effective)
            store.prune_failures_below(effective)
            return gave_up, store.get_last_id(CHANNEL)

        for cycle in range(1, MESSAGE_FAILURE_GIVE_UP_ATTEMPTS):
            gave_up, last_id = run_once()
            check(
                f"cycle {cycle}: checkpoint held below the broken message",
                not gave_up and last_id == BROKEN_MESSAGE - 1,
                f"gave_up={gave_up} last_id={last_id}",
            )

        gave_up, last_id = run_once()
        check(
            "final cycle: message abandoned and checkpoint released",
            gave_up and last_id == latest_ids_map[CHANNEL],
            f"gave_up={gave_up} last_id={last_id}",
        )

        remaining = store.get_failure_counts(CHANNEL)
        check("abandoned message pruned from the ledger", BROKEN_MESSAGE not in remaining, remaining)


def test_other_channels_unaffected():
    print("[6] a failure in one channel never blocks another")
    with tempfile.TemporaryDirectory() as tmp:
        store = new_store(tmp)
        store.record_failure(CHANNEL, BROKEN_MESSAGE, confirmed=True)

        effective = _clamp_checkpoints_below_failures(
            {CHANNEL: 389235, 3886862704: 85112},
            {CHANNEL: BROKEN_MESSAGE},
        )
        check("failing channel clamped", effective[CHANNEL] == BROKEN_MESSAGE - 1, effective)
        check("healthy channel untouched", effective[3886862704] == 85112, effective)


if __name__ == "__main__":
    test_ledger_counts_only_confirmed_failures()
    test_give_up_after_budget()
    test_success_clears_strikes()
    test_prune_below_checkpoint()
    test_forwarder_giveup_wiring()
    test_other_channels_unaffected()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        sys.exit(1)
    print("ALL CHECKS PASSED")
