"""Resource guards: the tool must not be able to spawn model processes or
Python workers without bound, however hard the sessions report at it.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AGENTDASH_HOME", tempfile.mkdtemp(prefix="agentdash-limits-"))

from agentdash import config, ranker, state  # noqa: E402

FAILURES = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILURES.append(label)


def main():
    cfg = config.load_config()
    now = time.time()

    print("rate limit between model calls")
    allowed, why = ranker.budget_state({}, cfg, now)
    check(allowed, "a cold ranker may call the model")
    allowed, why = ranker.budget_state({"last_call": now - 1}, cfg, now)
    check(not allowed and "rate limited" in why, "a call one second ago is refused")
    allowed, _ = ranker.budget_state(
        {"last_call": now - config.RANK_MIN_INTERVAL_SECONDS - 1}, cfg, now)
    check(allowed, "past the minimum gap it is allowed again")

    print("hourly ceiling")
    spent = {"last_call": now - 999,
             "recent_calls": [now - i for i in range(config.RANK_MAX_PER_HOUR)]}
    allowed, why = ranker.budget_state(spent, cfg, now)
    check(not allowed and "budget" in why, "the hourly ceiling stops further calls")
    aged = {"last_call": now - 999,
            "recent_calls": [now - 3601 - i for i in range(config.RANK_MAX_PER_HOUR)]}
    allowed, _ = ranker.budget_state(aged, cfg, now)
    check(allowed, "calls older than an hour fall out of the window")

    print("the dashboard is told why the order is not fresh")
    data = state.read()
    data["ranker"]["budget_note"] = "hourly rank budget spent (60/h) - using heuristic order"
    check("budget" in (ranker.staleness(data["ranker"], data) or ""),
          "a spent budget shows as a header warning, not silence")

    print("ordering survives the budget running out")
    state.touch_session("a", status="done", action_needed=True, blocked_since=now - 500)
    state.touch_session("b", status="working", action_needed=False)
    ranker.apply_heuristic()
    rows = state.read()["sessions"]
    check(rows["a"]["priority"] == 1 and rows["b"]["priority"] == 2,
          "the heuristic still produces a usable order with no model at all")

    print("worker herd control")
    import fcntl
    check(not ranker.worker_running(), "no worker running to begin with")
    fd = os.open(str(config.RANK_LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        check(ranker.worker_running(), "a held lock is detected as a running worker")
        before = ranker.run_worker()
        check(before == 0, "a second worker exits immediately instead of competing")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    check(not ranker.worker_running(), "the lock is released cleanly")

    print("worker loop is bounded")
    check(config.RANK_MAX_CONSECUTIVE > 0 and config.RANK_MAX_CONSECUTIVE < 1000,
          "a single worker retires after a bounded number of rounds")

    print("")
    if FAILURES:
        print("%d FAILURES" % len(FAILURES))
        return 1
    print("all resource-guard checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
