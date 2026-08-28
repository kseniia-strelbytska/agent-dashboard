"""Memory pressure: the tool must stand down when the machine is starving,
and must never withhold work merely because it could not read the kernel.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AGENTDASH_HOME", tempfile.mkdtemp(prefix="agentdash-pressure-"))

from agentdash import config, pressure, state, tui  # noqa: E402

FAILURES = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILURES.append(label)


def sample(level, pct, known=True):
    return {"level": level, "raw": None, "available_percent": pct, "known": known}


def main():
    cfg = config.load_config()

    print("reading the kernel")
    live = pressure.read()
    check(live["level"] in (pressure.NORMAL, pressure.WARNING,
                            pressure.CRITICAL, pressure.UNKNOWN),
          "a live read returns a known level (%s)" % live["level"])
    check(pressure.read() is pressure.read(), "consecutive reads are cached, not re-syscalled")
    check(isinstance(pressure.describe(live), str) and pressure.describe(live),
          "describe() always produces something printable")

    print("deciding whether to spend")
    check(pressure.should_defer(cfg, sample(pressure.NORMAL, 80))[0] is False,
          "a healthy machine is left alone")
    defer, why = pressure.should_defer(cfg, sample(pressure.WARNING, 30))
    check(defer and "pressure" in why, "kernel warning level pauses ranking")
    defer, why = pressure.should_defer(cfg, sample(pressure.CRITICAL, 3))
    check(defer and "critical" in why, "kernel critical level pauses ranking")
    defer, why = pressure.should_defer(cfg, sample(pressure.NORMAL, 5))
    check(defer and "5%" in why,
          "a low free-memory floor pauses ranking even when the kernel is calm")

    print("failing open, not closed")
    check(pressure.should_defer(cfg, sample(pressure.UNKNOWN, None, known=False))[0] is False,
          "an unreadable kernel never withholds work on a guess")
    off = dict(cfg, defer_under_memory_pressure=False)
    check(pressure.should_defer(off, sample(pressure.CRITICAL, 1))[0] is False,
          "the behaviour can be turned off in config")

    print("the user is told, not left guessing")
    def mutate(data):
        data["sessions"]["s"] = state.new_session_record("s", [])
        data["sessions"]["s"].update(summary="A. B. C.", tag="tests",
                                     action_needed=True, priority=1)
        data["meta"]["daemon_heartbeat"] = __import__("time").time()
        return True
    state.update(mutate)

    dash = tui.Dashboard()
    dash.cols, dash.rows = 120, 40
    import agentdash.pressure as pm
    pm._cache.update(at=float("inf"), value=sample(pressure.CRITICAL, 3))
    try:
        header = " ".join(l.text for l in dash.compose(state.read())[:3])
    finally:
        pm._cache.clear()
    check("critical" in header and "3% free" in header,
          "the dashboard header names the pressure and how much is left")
    check("paused" in header, "and says ranking has stood down")

    print("still width-safe while warning")
    import re
    ansi = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
    over = []
    for cols in (40, 60, 80, 120, 200):
        d = tui.Dashboard()
        d.cols, d.rows = cols, 60
        pm._cache.update(at=float("inf"), value=sample(pressure.CRITICAL, 3))
        try:
            for line in d.compose(state.read()):
                if tui.width_of(ansi.sub("", line.text)) > cols:
                    over.append(cols)
        finally:
            pm._cache.clear()
    check(not over, "the pressure warning never overflows the terminal (%s)" % (over or "all widths ok"))

    print("")
    if FAILURES:
        print("%d FAILURES" % len(FAILURES))
        return 1
    print("all pressure checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
