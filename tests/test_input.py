"""Input decoding: SGR mouse hover, focus reporting, keys."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AGENTDASH_HOME", tempfile.mkdtemp(prefix="agentdash-input-"))

from agentdash import tui  # noqa: E402

FAILURES = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILURES.append(label)


class Fake(tui.Dashboard):
    """A dashboard that captures writes instead of touching a terminal."""
    def __init__(self):
        super().__init__()
        self.written = []
        self.looked = 0

    def _write(self, text):
        self.written.append(text)

    def _mark_looked(self):
        self.looked += 1


def feed(dash, data):
    """Drive the same decoding path _read_input uses, without select()."""
    import agentdash.tui as t
    chunk = data
    dirty = False
    i = 0
    while i < len(chunk):
        rest = chunk[i:]
        match = t._SGR_MOUSE.match(rest)
        if match:
            dirty |= dash._handle_mouse(int(match.group(1)), int(match.group(2)),
                                        int(match.group(3)), match.group(4))
            i += match.end()
            continue
        if rest.startswith(t.ESC + "[I") or rest.startswith(t.ESC + "[O"):
            dash._mark_looked()
            dirty = True
            i += 3
            continue
        if rest.startswith(t.ESC + "["):
            end = 2
            while end < len(rest) and not rest[end].isalpha():
                end += 1
            i += min(end + 1, len(rest))
            continue
        dirty |= dash._handle_key(rest[0])
        i += 1
    return dirty


def main():
    print("mouse never opens anything")
    dash = Fake()
    dash.line_owner = {4: "sess-a", 5: "sess-a", 9: "sess-b"}
    jumped = []
    dash._focus_session = lambda sid: jumped.append(sid)
    check(not feed(dash, "\x1b[<35;10;4M"), "motion over a row does nothing")
    check(dash.open_id is None, "and opens no private context")
    check(not feed(dash, "\x1b[<35;12;9M"), "moving between rows does nothing")
    check(dash.open_id is None and jumped == [], "still nothing opened, nothing jumped")
    check(not feed(dash, "\x1b[<64;12;4M"), "the scroll wheel is ignored")
    check(feed(dash, "\x1b[<0;12;9M") and jumped == ["sess-b"],
          "a deliberate click still jumps to that session's window")
    check(dash.open_id is None, "and a click does not open private context either")
    check(Fake().mouse_enabled is False,
          "mouse reporting is off by default, so text selection works")

    print("numbers open and close context")
    dash = Fake()
    dash.index_map = {1: "sess-a", 2: "sess-b"}
    check(feed(dash, "1") and dash.open_id == "sess-a", "1 opens the first session")
    check(feed(dash, "1") and dash.open_id is None, "1 again closes it")
    feed(dash, "1")
    check(feed(dash, "2") and dash.open_id == "sess-b", "2 switches to the second")
    check(feed(dash, "7") and dash.open_id == "sess-b",
          "a number with no session leaves the open one alone")
    check("no session 7" in dash.status_note, "and says so")

    print("focus")
    dash = Fake()
    feed(dash, "\x1b[O")
    feed(dash, "\x1b[I")
    check(dash.looked == 2, "focus in and focus out both stamp 'last looked'")

    print("keys")
    dash = Fake()
    start = dash.top_n
    feed(dash, "+")
    check(dash.top_n == start + 1, "+ expands one more row")
    feed(dash, "-")
    feed(dash, "-")
    check(dash.top_n == max(1, start - 1), "- collapses a row, with a floor of 1")
    for _ in range(30):
        dash._handle_key("-")
    check(dash.top_n == 1, "- cannot go below one row")
    for _ in range(30):
        dash._handle_key("+")
    check(dash.top_n == 12, "+ is capped")
    feed(dash, "a")
    check(dash.show_all is True, "a unfolds everything")
    feed(dash, "a")
    check(dash.show_all is False, "a folds it back")
    feed(dash, "?")
    check(dash.help_open is True, "? opens help")
    feed(dash, "m")
    check(dash.mouse_enabled is True and tui.MOUSE_ON in dash.written[-1],
          "m turns mouse reporting on for click-to-jump")
    feed(dash, "m")
    check(dash.mouse_enabled is False and tui.MOUSE_OFF in dash.written[-1],
          "and off again")
    dash.open_id = "x"
    feed(dash, "m")
    check(dash.open_id == "x", "toggling the mouse leaves an open row alone")

    print("quit")
    dash = Fake()
    for key in ("q", "\x03", "\x04"):
        try:
            dash._handle_key(key)
            check(False, "%r quits" % key)
        except KeyboardInterrupt:
            check(True, "%r quits" % key)

    print("")
    if FAILURES:
        print("%d FAILURES" % len(FAILURES))
        return 1
    print("all input checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
