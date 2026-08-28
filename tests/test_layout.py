"""Layout invariants: nothing may overflow the terminal width, at any size."""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AGENTDASH_HOME", tempfile.mkdtemp(prefix="agentdash-test-"))

import render_demo  # noqa: E402
from agentdash import state, tui  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def visible(text):
    return tui.width_of(ANSI.sub("", text))


def main():
    render_demo.seed()
    variants = {"healthy": state.read()}

    stale = state.read()
    stale["ranker"]["turns"] = 52
    stale["ranker"]["started"] = stale["ranker"]["started"] - 20000
    variants["stale-ranker"] = stale

    dead = state.read()
    dead["meta"]["daemon_heartbeat"] = 0
    variants["no-daemon"] = dead

    broken = state.read()
    broken["ranker"]["last_error"] = "claude exited 1: " + "x" * 120
    variants["ranker-error"] = broken

    empty = state.read()
    empty["sessions"] = {}
    variants["empty"] = empty

    failures = []
    for label, data in variants.items():
      for cols in (40, 50, 60, 72, 80, 100, 120, 160, 200):
        for hover in (None, "s0", "s3"):
            for show_all in (False, True):
                for help_open in (False, True):
                    dash = tui.Dashboard()
                    dash.cols, dash.rows = cols, 400
                    dash.hover_id = hover
                    dash.show_all = show_all
                    dash.help_open = help_open
                    dash.status_note = "rerank requested " + "y" * 90
                    dash.status_until = 1e18
                    for i, line in enumerate(dash.compose(data)):
                        w = visible(line.text)
                        if w > cols:
                            failures.append(
                                "%s cols=%d hover=%s all=%s help=%s line %d: width %d > %d\n    %r"
                                % (label, cols, hover, show_all, help_open, i, w, cols,
                                   ANSI.sub("", line.text)))
    data = variants["healthy"]

    # hover must not move the hovered row
    for cols in (80, 120):
        dash = tui.Dashboard()
        dash.cols, dash.rows = cols, 400
        plain = dash.compose(data)
        target = next(i for i, l in enumerate(plain) if l.session_id == "s1")
        dash.hover_id = "s1"
        hovered = dash.compose(data)
        moved = next(i for i, l in enumerate(hovered) if l.session_id == "s1")
        if moved != target:
            failures.append("cols=%d: hovering s1 moved its first line %d -> %d"
                            % (cols, target, moved))

    # every rendered session line must be attributable for hit-testing
    dash = tui.Dashboard()
    dash.cols, dash.rows = 100, 400
    dash.hover_id = "s0"
    owners = {l.session_id for l in dash.compose(data) if l.session_id}
    if "s0" not in owners:
        failures.append("hovered session has no owned lines")

    if failures:
        print("FAIL (%d)" % len(failures))
        for f in failures:
            print(" -", f)
        return 1
    print("layout ok: widths, hover stability and hit-testing all hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
