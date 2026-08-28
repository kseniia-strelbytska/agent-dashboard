"""The cat's three rules, tested as rules rather than as decoration.

It yields, it costs nothing, and it turns off cleanly. The behaviours it
carries are checked against the signals they claim to represent.
"""
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AGENTDASH_HOME", tempfile.mkdtemp(prefix="agentdash-cat-"))

import render_demo  # noqa: E402
from agentdash import cat as cat_module, config, state, tui  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
FAILURES = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILURES.append(label)


def board(cols=100, rows=80):
    d = tui.Dashboard()
    d.cols, d.rows = cols, rows
    return d


def main():
    render_demo.seed()
    data = state.read()
    now = time.time()

    print("it stays in the gutters, always")
    d = board()
    lines = d.compose(data)
    check(d.cat is not None, "the cat is on by default")
    top, bottom = d.cat_rows
    check(top > 0, "and it is placed somewhere")
    for row in (top, bottom):
        line = lines[row - 1]
        check(line.gutter is not None,
              "row %d is a gutter, not a card" % row)
    card_rows = [i + 1 for i, l in enumerate(lines) if l.session_id and l.gutter is None]
    check(top not in card_rows and bottom not in card_rows,
          "the cat never occupies a line that belongs to a card")

    print("it stays away from a session that has already gone red")
    reds = [i for i, r in enumerate(sorted(data["sessions"].values(),
                                           key=lambda r: r.get("priority") or 99))
            if r.get("blocked_since") and now - r["blocked_since"] >= d.red_after]
    c = cat_module.Cat()
    ordered = sorted(data["sessions"].values(), key=lambda r: r.get("priority") or 99)
    target = c._target_gutter(ordered, d.red_after, now, len(ordered))
    check(target not in reds, "an already-red session is not chosen (%s vs %s)" % (target, reds))

    print("it sits beside a session just before that session turns red")
    soon = [dict(ordered[0]), dict(ordered[1])]
    soon[0]["blocked_since"] = now - d.red_after * 0.9      # nearly red
    soon[1]["blocked_since"] = now - 10                     # just started
    check(c._target_gutter(soon, d.red_after, now, 2) == 0,
          "it picks the one closest to going red")
    soon[0]["blocked_since"] = now - 60                     # nowhere near
    check(c._target_gutter(soon, d.red_after, now, 2) is None,
          "and picks nobody when nobody is close")

    print("it freezes when attention is required, and when memory is short")
    c = cat_module.Cat()
    c._last_step = 0
    moved = c.update(data, ordered, 3, 100, d.red_after,
                     decision_open=True, under_pressure=False, now=now)
    check(moved is False and c.frozen, "an expanded decision freezes it")
    moved = c.update(data, ordered, 3, 100, d.red_after,
                     decision_open=False, under_pressure=True, now=now)
    check(moved is False and c.frozen, "memory pressure freezes it")
    x_before, frame_before = c.x, c.frame
    for i in range(20):
        c.update(data, ordered, 3, 100, d.red_after, False, True, now=now + i)
    check(c.x == x_before and c.frame == frame_before,
          "and it really does not move while frozen")
    c.pet(now)
    c.update(data, ordered, 3, 100, d.red_after, False, True, now=now + 5)
    check(c.hearts == [], "hearts still expire while frozen, rather than sticking")

    print("its pace tracks how many sessions are open")
    c = cat_module.Cat()
    two, six = c._pace(2), c._pace(6)
    check(six < two, "six sessions trot faster than two (%.2fs vs %.2fs)" % (six, two))
    check(c._pace(40) >= 1.0 / 6.0, "and it never becomes a blur")

    print("it gets sleepy for reasons that are real")
    fresh = {"sessions": {"a": {"started": now - 60}}}
    old = {"sessions": {"a": {"started": now - 9 * 3600}}}
    reversed_a_lot = {"sessions": {"a": {"started": now - 60,
                                         "reopen_times": [now - 60] * 5}}}
    hour = time.localtime(now).tm_hour
    if hour not in cat_module.SLEEPY_HOURS:
        check(c._sleepy(fresh, now) is False, "a fresh short day is not sleepy")
    check(c._sleepy(old, now) is True, "a nine-hour day is sleepy")
    check(c._sleepy(reversed_a_lot, now) is True, "so is work being sent back repeatedly")

    print("petting gives hearts and changes nothing else")
    c = cat_module.Cat()
    before = {k: v for k, v in vars(c).items() if k not in ("hearts", "_last_pet", "seconds")}
    check(c.pet(now) is True, "it can be petted")
    check(len(c.hearts) == 1, "which produces a heart")
    check(c.pet(now + 0.01) is False, "and is rate limited, so a slow drag is not a swarm")
    after = {k: v for k, v in vars(c).items() if k not in ("hearts", "_last_pet", "seconds")}
    check(before == after, "and nothing else about the cat changes")
    ordered_before = [dict(r) for r in ordered]
    check(ordered_before == ordered, "and nothing about any session changes")

    print("it costs almost nothing")
    c = cat_module.Cat()
    start = time.perf_counter()
    for i in range(240):                       # 30 seconds at 8fps
        c.update(data, ordered, 3, 100, d.red_after, False, False, now=now + i * 0.125)
        c.draw(100)
    elapsed = time.perf_counter() - start
    check(c.seconds < 0.5, "240 frames of cat cost %.0f ms of CPU" % (c.seconds * 1000))
    check(c.cost_fraction(30.0) < 0.02,
          "which is under 2%% of wall clock (%.3f%%)" % (c.cost_fraction(30.0) * 100))

    print("and it says what it costs, in the header")
    d = board()
    header = " ".join(ANSI.sub("", l.text) for l in d.compose(data)[:3])
    check("cat " in header and "%" in header, "the header carries the cat's own cost")

    print("the off switch is one line and no argument")
    saved = dict(config.DEFAULTS)
    config.DEFAULTS["cat"] = False
    try:
        off = board()
        lines = off.compose(state.read())
        check(off.cat is None, "no cat")
        check(off.cat_rows == (-1, -1), "and nothing drawn")
        check("cat " not in " ".join(ANSI.sub("", l.text) for l in lines[:3]),
              "and no mention of it anywhere")
        widths = [tui.width_of(ANSI.sub("", l.text)) for l in lines]
        check(max(widths) <= off.cols, "layout still fits")
    finally:
        config.DEFAULTS.clear()
        config.DEFAULTS.update(saved)

    print("it never overflows the terminal, at any width")
    over = []
    for cols in (40, 60, 80, 100, 160):
        b = board(cols)
        for line in b.compose(data):
            if tui.width_of(ANSI.sub("", line.text)) > cols:
                over.append((cols, ANSI.sub("", line.text)[:40]))
    check(not over, "widths hold with the cat on (%s)" % (over[:1] or "all clear"))

    print("")
    if FAILURES:
        print("%d FAILURES" % len(FAILURES))
        return 1
    print("all cat checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
