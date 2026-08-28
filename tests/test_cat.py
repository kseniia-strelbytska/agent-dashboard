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

    print("it lives above every card, in its own lane")
    d = board()
    lines = d.compose(data)
    check(d.cat is not None, "the cat is on by default")
    first, last = d.cat_rows
    check(first > 0 and last - first + 1 == cat_module.HEIGHT_ROWS,
          "it occupies exactly %d rows" % cat_module.HEIGHT_ROWS)
    check(len(cat_module.FRAMES["walk_a"]) >= 6,
          "and is %d pixels tall" % len(cat_module.FRAMES["walk_a"]))
    check(all(len(r) == cat_module.WIDTH for f in cat_module.FRAMES.values() for r in f),
          "every frame is the same width, so it cannot shear")
    card_rows = [i + 1 for i, l in enumerate(lines) if l.session_id]
    check(all(r not in card_rows for r in range(first, last + 1)),
          "no cat row belongs to any card")
    check(card_rows and first < min(card_rows),
          "and the whole cat is above the first card (%d < %d)" % (first, min(card_rows)))

    ordered = sorted(data["sessions"].values(), key=lambda r: r.get("priority") or 99)
    c = cat_module.Cat()

    print("it stays away from a session that has already gone red")
    reds = [i for i, r in enumerate(ordered)
            if r.get("blocked_since") and now - r["blocked_since"] >= d.red_after]
    check(c.warning_index(ordered, d.red_after, now) not in reds,
          "an already-red session is not chosen (reds: %s)" % reds)

    print("it settles above the session just before that session turns red")
    soon = [dict(ordered[0]), dict(ordered[1])]
    soon[0]["blocked_since"] = now - d.red_after * 0.9      # nearly red
    soon[1]["blocked_since"] = now - 10                     # just started
    check(c.warning_index(soon, d.red_after, now) == 0,
          "it picks the one closest to going red")
    soon[0]["blocked_since"] = now - 60                     # nowhere near
    check(c.warning_index(soon, d.red_after, now) is None,
          "and picks nobody when nobody is close")

    print("and walks to that session's column in the strip")
    c2 = cat_module.Cat()
    soon[0]["blocked_since"] = now - d.red_after * 0.9
    c2.x = 60.0
    for step in range(80):
        c2.update({"sessions": {}}, soon, 100, d.red_after, False, "normal",
                  targets={0: 12}, now=now + step)
    check(abs(c2.x - 12) < 1.0, "it arrives at the column (x=%.0f)" % c2.x)
    check(c2.mood == "sit", "and sits down there")

    print("it freezes when attention is required, and when memory is critical")
    c = cat_module.Cat()
    c._last_step = 0
    moved = c.update(data, ordered, 100, d.red_after,
                     decision_open=True, pressure_level="normal", now=now)
    check(moved is False and c.frozen, "an expanded decision freezes it")
    moved = c.update(data, ordered, 100, d.red_after,
                     decision_open=False, pressure_level="critical", now=now)
    check(moved is False and c.frozen, "critical memory freezes it")
    # A busy Mac sits at the kernel's warning level for hours. Freezing there
    # would leave the cat permanently dead, which reads as broken rather than
    # considerate - it costs a few string operations, not a Node process.
    c2 = cat_module.Cat()
    c2.update(data, ordered, 100, d.red_after, False, pressure_level="warning", now=now)
    check(c2.frozen is False, "a mere warning slows it rather than stopping it")
    x_before, frame_before = c.x, c.frame
    for i in range(20):
        c.update(data, ordered, 100, d.red_after, False, "critical", now=now + i)
    check(c.x == x_before and c.frame == frame_before,
          "and it really does not move while frozen")
    c.pet(now)
    c.update(data, ordered, 100, d.red_after, False, "critical", now=now + 5)
    check(c.hearts == [], "hearts still expire while frozen, rather than sticking")

    print("it walks, and it is curious about it")
    c3 = cat_module.Cat()
    seen, xs = set(), []
    for i in range(300):
        c3.update({"sessions": {}}, [{"blocked_since": None}] * 3, 100, d.red_after,
                  False, "normal", now=now + i * 0.125)
        seen.add(c3.frame)
        xs.append(int(c3.x))
    check(max(xs) - min(xs) > 5, "it covers ground (%d..%d)" % (min(xs), max(xs)))
    check("idle" in seen, "and stops to look around on the way")
    check({"walk_a", "walk_b"} <= seen, "with both walk frames, so the legs move")

    print("its pace tracks how many sessions are open")
    c = cat_module.Cat()
    two, six = c._pace(2), c._pace(6)
    check(six < two, "six sessions trot faster than two (%.2fs vs %.2fs)" % (six, two))
    check(c._pace(40) >= 1.0 / 6.0, "and it never becomes a blur")

    print("it looks worried once there are too many sessions")
    c4 = cat_module.Cat()
    quiet = [{"blocked_since": None}] * (cat_module.WORRIED_SESSIONS - 1)
    busy = [{"blocked_since": None}] * cat_module.WORRIED_SESSIONS
    c4.update({"sessions": {}}, quiet, 100, d.red_after, False, "normal", now=now)
    check(c4.worried is False, "under the threshold it is calm")
    c4.update({"sessions": {}}, busy, 100, d.red_after, False, "normal", now=now + 1)
    check(c4.worried is True, "at %d sessions the drop appears" % cat_module.WORRIED_SESSIONS)
    drop_rows = cat_module._pixels("walk_a", 1, True)
    check(any("d" in r for r in drop_rows), "and it is actually drawn")
    check(cat_module._pixels("walk_a", 1, False) == list(cat_module.FRAMES["walk_a"]),
          "while a calm cat is pixel-for-pixel the original")
    check(cat_module._pixels("walk_a", -1, True)[0].index("d")
          < cat_module.WIDTH // 2,
          "and it follows the cat when it turns round")

    print("but worry never competes with the other signals")
    tired = {"sessions": {"a": {"started": now - 9 * 3600}}}
    c5 = cat_module.Cat()
    c5.update(tired, busy, 100, d.red_after, False, "normal", now=now)
    check(c5.mood == "sleep" and c5.worried is False,
          "a sleeping cat is not also sweating: one signal at a time")
    c6 = cat_module.Cat()
    c6.pet(now)
    c6.update({"sessions": {}}, busy, 100, d.red_after, False, "normal", now=now)
    check(c6.mood == "happy" and c6.worried is False,
          "and petting still buys a moment where nothing is being signalled")

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
        c.update(data, ordered, 100, d.red_after, False, "normal", now=now + i * 0.125)
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
