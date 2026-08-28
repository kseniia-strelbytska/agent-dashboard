"""The dashboard: a live terminal UI meant to own one iTerm2 window forever.

Design notes
------------
* By default only the top few rows render in full. Everything else collapses to
  one dim line under a divider, so the eye lands on what actually needs a human.
* Hovering a row reveals the four private sentences that the session wrote for
  the ranking agent. Expansion only ever adds lines *below* the hovered row's
  first line, so the row under the pointer never moves and hover cannot
  oscillate.
* "Since you last looked" is anchored on terminal focus reporting: the moment
  this window loses focus we stamp `last_look`, and anything reported after that
  gets a NEW marker until you look away again.
"""
import os
import re
import select
import signal
import sys
import termios
import time
import tty
import unicodedata
from typing import Dict, List, Optional, Tuple

from . import config, palette, ranker, state

ESC = "\x1b"
CSI = ESC + "["

ALT_SCREEN_ON = CSI + "?1049h"
ALT_SCREEN_OFF = CSI + "?1049l"
CURSOR_HIDE = CSI + "?25l"
CURSOR_SHOW = CSI + "?25h"
MOUSE_ON = CSI + "?1000h" + CSI + "?1002h" + CSI + "?1003h" + CSI + "?1006h"
MOUSE_OFF = CSI + "?1006l" + CSI + "?1003l" + CSI + "?1002l" + CSI + "?1000l"
FOCUS_ON = CSI + "?1004h"
FOCUS_OFF = CSI + "?1004l"

DIM = CSI + "2m"
BOLD = CSI + "1m"
ITALIC = CSI + "3m"
RESET = CSI + "0m"
REVERSE = CSI + "7m"

RED = "#E5484D"
AMBER = "#F5A623"
GREY = "#6B7280"
FAINT = "#9AA0A6"
TEXT = "#E6E6E6"
CHROME = "#3A3F45"

_SGR_MOUSE = re.compile(r"^\x1b\[<(\d+);(\d+);(\d+)([Mm])")


# --- small terminal helpers ---------------------------------------------------

def fg(hex_value: str) -> str:
    r, g, b = palette.hex_to_rgb(hex_value)
    return CSI + "38;2;%d;%d;%dm" % (r, g, b)


def bg(hex_value: str) -> str:
    r, g, b = palette.hex_to_rgb(hex_value)
    return CSI + "48;2;%d;%d;%dm" % (r, g, b)


def width_of(text: str) -> int:
    total = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if width_of(text) <= limit:
        return text
    out, used = [], 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > limit - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def wrap(text: str, limit: int) -> List[str]:
    words = (text or "").split()
    if not words:
        return []
    lines, cur = [], ""
    for word in words:
        candidate = (cur + " " + word) if cur else word
        if width_of(candidate) <= limit:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = word if width_of(word) <= limit else truncate(word, limit)
    if cur:
        lines.append(cur)
    return lines


def fmt_duration(seconds: Optional[float], short: bool = False) -> str:
    if seconds is None or seconds < 0:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    hours, mins = seconds // 3600, (seconds % 3600) // 60
    if short or hours >= 24:
        return "%dh" % hours if hours < 24 else "%dd%dh" % (hours // 24, hours % 24)
    return "%dh %02dm" % (hours, mins)


# --- the frame ----------------------------------------------------------------

class Line:
    """One rendered line plus the session it belongs to (for hover hit-testing)."""
    __slots__ = ("text", "session_id")

    def __init__(self, text: str, session_id: Optional[str] = None):
        self.text = text
        self.session_id = session_id


class Dashboard:
    def __init__(self):
        self.cfg = config.load_config()
        self.top_n = int(self.cfg.get("top_n", config.TOP_N_DEFAULT))
        self.red_after = float(self.cfg.get("blocked_red_seconds", config.BLOCKED_RED_SECONDS))
        self.show_all = False
        self.hover_id: Optional[str] = None
        self.hover_row: Optional[int] = None
        self.mouse_enabled = True
        self.help_open = False
        self.rows, self.cols = 24, 80
        self.line_owner: Dict[int, str] = {}
        self.last_rev = -1
        self._cached_state = None
        self._cached_stamp = None
        self.status_note = ""
        self.status_until = 0.0
        self._out = sys.stdout

    # -- terminal lifecycle ----------------------------------------------------

    def _measure(self):
        try:
            size = os.get_terminal_size()
            self.cols, self.rows = size.columns, size.lines
        except OSError:
            self.cols, self.rows = 80, 24

    def _write(self, text: str):
        try:
            self._out.write(text)
            self._out.flush()
        except (OSError, ValueError):
            pass

    def _enter(self):
        self._write(ALT_SCREEN_ON + CURSOR_HIDE + FOCUS_ON)
        if self.mouse_enabled:
            self._write(MOUSE_ON)
        self._write(ESC + "]0;Agent Dashboard\x07")

    def _leave(self):
        self._write(MOUSE_OFF + FOCUS_OFF + CURSOR_SHOW + ALT_SCREEN_OFF + RESET)

    # -- data ------------------------------------------------------------------

    def _load_state(self) -> Dict:
        """Re-read the state file only when it has actually changed on disk.

        The event loop ticks eight times a second; parsing the document every
        tick is pure waste, and this window is meant to stay open all day.
        """
        try:
            st = os.stat(config.STATE_FILE)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp is not None and stamp == self._cached_stamp and self._cached_state is not None:
            return self._cached_state
        data = state.read()
        self._cached_state = data
        self._cached_stamp = stamp
        return data

    def _ordered(self, data: Dict) -> List[Dict]:
        sessions = list(data["sessions"].values())
        ranked = [s for s in sessions if s.get("priority")]
        unranked = [s for s in sessions if not s.get("priority")]
        ranked.sort(key=lambda s: (s["priority"], s.get("started", 0)))
        for name, prio, reason in ranker.heuristic_order(unranked):
            for rec in unranked:
                if rec["name"] == name:
                    rec["_fallback_priority"] = prio
                    rec.setdefault("rank_reason", reason)
        unranked.sort(key=lambda s: s.get("_fallback_priority", 999))
        return ranked + unranked

    # -- rendering -------------------------------------------------------------

    def _tag_label(self, tag: Optional[str], room: int) -> str:
        return truncate((tag or "unknown"), max(3, min(16, room)))

    def _tag_block(self, label: str, colour: str) -> str:
        tint = palette.lighten(colour or "#333333", 2.1)
        return bg(tint) + fg(palette.readable_fg(tint)) + " " + label + " " + RESET

    def _blocked_cell(self, rec: Dict, now: float) -> Tuple[str, int]:
        if not rec.get("blocked_since"):
            return DIM + fg(GREY) + "running" + RESET, len("running")
        waited = now - rec["blocked_since"]
        text = "waited " + fmt_duration(waited)
        colour = RED if waited >= self.red_after else AMBER
        style = (BOLD + fg(colour)) if waited >= self.red_after else fg(colour)
        return style + text + RESET, width_of(text)

    def _header(self, data: Dict, lines: List[Line], now: float):
        sessions = list(data["sessions"].values())
        needing = [s for s in sessions if s.get("action_needed")]
        title = " AGENT DASHBOARD "
        variants = [
            "%d need action · %d live · %s" % (len(needing), len(sessions), time.strftime("%H:%M:%S")),
            "%d action · %d live · %s" % (len(needing), len(sessions), time.strftime("%H:%M")),
            "%d/%d · %s" % (len(needing), len(sessions), time.strftime("%H:%M")),
            "%d/%d" % (len(needing), len(sessions)),
            "",
        ]
        title = truncate(title, max(3, self.cols - 4))
        right = next(v for v in variants
                     if width_of(title) + width_of(v) + 2 <= self.cols)
        pad = max(1, self.cols - width_of(title) - width_of(right) - 1)
        lines.append(Line(bg(CHROME) + fg("#FFFFFF") + BOLD + title + RESET
                          + bg(CHROME) + " " * pad + fg(TEXT) + right + " " + RESET))

        rk = data["ranker"]
        bits = ["ranker: %s" % self.cfg.get("ranker_model", config.RANKER_MODEL)]
        if rk.get("ranks"):
            bits.append("%d ranks" % rk["ranks"])
        if rk.get("cost_usd"):
            bits.append("$%.2f" % rk["cost_usd"])
        if rk.get("retired"):
            bits.append("%d refresh%s" % (rk["retired"], "" if rk["retired"] == 1 else "es"))
        left = " " + " · ".join(bits)
        warn = ranker.staleness(rk, data, now)
        if not data["meta"].get("daemon_heartbeat") or \
                now - (data["meta"].get("daemon_heartbeat") or 0) > 30:
            warn = "iTerm2 daemon not responding - window colours are frozen"
        if warn:
            room = self.cols - width_of(left) - 3
            if room < 24:
                # No space beside the ranker line: give the warning its own row.
                lines.append(Line(DIM + fg(FAINT) + truncate(left, self.cols - 1) + RESET))
                lines.append(Line(" " + fg(AMBER) + truncate("⚠ " + warn, self.cols - 2) + RESET))
            else:
                warn_txt = truncate("⚠ " + warn, room)
                pad = max(1, self.cols - width_of(left) - width_of(warn_txt) - 1)
                lines.append(Line(DIM + fg(FAINT) + left + RESET + " " * pad
                                  + fg(AMBER) + warn_txt + RESET))
        else:
            lines.append(Line(DIM + fg(FAINT) + truncate(left, self.cols - 1) + RESET))
        lines.append(Line(""))

    def _full_row(self, rec: Dict, index: int, data: Dict, lines: List[Line], now: float):
        sid = rec["id"]
        colour = rec.get("color") or "#555555"
        indent = "     "
        name = rec.get("name") or "?"
        marker = "●"
        new = (rec.get("last_report") or 0) > (data["meta"].get("last_look") or 0)

        blocked_txt, blocked_w = self._blocked_cell(rec, now)

        # Fit the row by dropping the least important decorations first, then
        # shrinking the name, rather than letting anything spill past `cols`.
        prefix_w = 1 + len(str(index)) + 2 + 1 + 1     # " N  ● "
        no_report = not rec.get("self_reported", True)
        for want_note, want_new, want_tag in ((True, True, True), (False, True, True),
                                              (False, False, True), (False, False, False)):
            note_w = 12 if (no_report and want_note) else 0
            new_w = 4 if (new and want_new) else 0
            fixed = prefix_w + note_w + new_w + blocked_w + 1
            tag_room = self.cols - fixed - width_of(name) - 3
            tag_label = self._tag_label(rec.get("tag"), tag_room) if (want_tag and tag_room >= 5) else ""
            tag_w = (width_of(tag_label) + 2) if tag_label else 0
            name_budget = self.cols - fixed - tag_w - 1
            if name_budget >= 6 or not want_tag:
                shown = truncate(name, max(3, name_budget))
                break
        left = " %s%d%s  %s%s%s %s%s%s" % (
            DIM + fg(FAINT), index, RESET,
            fg(colour) + BOLD, marker, RESET,
            BOLD + fg(TEXT), shown, RESET)
        left_w = prefix_w + width_of(shown)
        if no_report and want_note:
            left += " " + DIM + fg(GREY) + "(no report)" + RESET
            left_w += 12
        if new and want_new:
            left += " " + fg(AMBER) + BOLD + "NEW" + RESET
            left_w += 4
        tag_txt = self._tag_block(tag_label, colour) if tag_label else ""
        gap = max(1, self.cols - left_w - blocked_w - tag_w - 1)
        lines.append(Line(left + " " * gap + blocked_txt + (" " + tag_txt if tag_txt else ""), sid))

        body_width = max(20, self.cols - len(indent) - 1)
        summary = rec.get("summary") or "No summary reported yet."
        for line in wrap(summary, body_width):
            lines.append(Line(indent + fg(TEXT) + line + RESET, sid))

        meta_bits = [rec.get("repo") or rec.get("cwd") or "?"]
        if rec.get("rank_reason"):
            meta_bits.append("why: " + rec["rank_reason"])
        meta = truncate(" · ".join(meta_bits), body_width)
        lines.append(Line(indent + DIM + fg(GREY) + meta + RESET, sid))

        if self.hover_id == sid:
            self._private_block(rec, indent, body_width, lines, sid)
        lines.append(Line("", sid))

    def _private_block(self, rec: Dict, indent: str, body_width: int,
                       lines: List[Line], sid: str):
        ctx = rec.get("ranker_context")
        head = "ranker-only context"
        lines.append(Line(indent + fg(CHROME) + "│ " + RESET
                          + DIM + ITALIC + fg(FAINT) + head + RESET, sid))
        if not ctx:
            lines.append(Line(indent + fg(CHROME) + "│ " + RESET
                              + DIM + fg(GREY) + "(this session has not supplied any)" + RESET, sid))
            return
        for line in wrap(ctx, body_width - 2):
            lines.append(Line(indent + fg(CHROME) + "│ " + RESET
                              + ITALIC + fg(FAINT) + line + RESET, sid))

    def _collapsed_row(self, rec: Dict, data: Dict, lines: List[Line], now: float):
        sid = rec["id"]
        colour = rec.get("color") or "#555555"
        name = rec.get("name") or "?"
        tag = (rec.get("tag") or "-")[:14]
        waited = fmt_duration(now - rec["blocked_since"], short=True) if rec.get("blocked_since") else "—"
        overdue = rec.get("blocked_since") and (now - rec["blocked_since"]) >= self.red_after
        new = (rec.get("last_report") or 0) > (data["meta"].get("last_look") or 0)

        # Columns shrink with the window; below a point the tag drops out first.
        budget = self.cols - 4 - width_of(waited) - (4 if new else 0)
        name_col = max(6, min(18, budget // 2))
        tag_col = max(0, min(15, budget - name_col - 1))
        parts = [" " + DIM + fg(colour) + "●" + RESET + "  ",
                 fg(TEXT if new else FAINT) + truncate(name, name_col).ljust(name_col) + RESET]
        used = 1 + 1 + 2 + name_col
        if tag_col >= 4:
            parts.append(DIM + fg(GREY) + truncate(tag, tag_col).ljust(tag_col) + RESET)
            used += tag_col
        parts.append((fg(RED) + BOLD if overdue else DIM + fg(GREY)) + waited + RESET)
        used += width_of(waited)
        if new:
            parts.append(" " + fg(AMBER) + "NEW" + RESET)
            used += 4
        summary = rec.get("summary") or ""
        room = self.cols - used - 3
        if room > 24 and summary:
            parts.append("  " + DIM + fg(GREY) + truncate(summary, room) + RESET)
        lines.append(Line("".join(parts), sid))
        if self.hover_id == sid:
            body_width = max(20, self.cols - 6)
            for text in wrap(summary, body_width):
                lines.append(Line("     " + fg(TEXT) + text + RESET, sid))
            self._private_block(rec, "     ", body_width, lines, sid)
            lines.append(Line("", sid))

    def _divider(self, label: str, lines: List[Line]):
        label = " %s " % truncate(label, max(3, self.cols - 6))
        room = max(0, self.cols - width_of(label) - 2)
        left = room // 2
        lines.append(Line(fg(CHROME) + " " + "─" * left + RESET
                          + DIM + fg(FAINT) + label + RESET
                          + fg(CHROME) + "─" * (room - left) + RESET))

    # Hints in priority order, each with a long and a short label. The footer
    # keeps as many as fit and drops the rest rather than wrapping or spilling.
    HINTS = (
        ("hover", "private context", "private"),
        ("click", "jump to window", "jump"),
        ("q", "quit", "quit"),
        ("a", "all/fold", "all"),
        ("r", "rerank", "rerank"),
        ("m", "mouse off (to select text)", "mouse"),
        ("+/-", "rows", "rows"),
        ("?", "help", "help"),
    )

    def _footer(self, lines: List[Line]):
        if time.time() < self.status_until and self.status_note:
            lines.append(Line(" " + fg(AMBER) + truncate(self.status_note, self.cols - 2) + RESET))

        def build(labels):
            plain = "  ".join("%s %s" % (k, t) for k, t, _ in labels)
            styled = "  ".join(fg(TEXT) + k + RESET + DIM + fg(GREY) + " " + t + RESET
                               for k, t, _ in labels)
            return plain, styled

        for shorten in (False, True):
            picked = [(k, (s2 if shorten else l), s2) for k, l, s2 in self.HINTS]
            while picked:
                plain, styled = build(picked)
                if width_of(plain) + 1 <= self.cols:
                    lines.append(Line(" " + styled))
                    return
                picked.pop()
        lines.append(Line(""))

    def _help(self, lines: List[Line]):
        body = [
            "",
            "  Rows are ordered by a Claude ranking agent that also reads four",
            "  private sentences per session which you only see on hover.",
            "",
            "  hover      reveal a session's ranker-only context",
            "  click      bring that session's iTerm2 window to the front",
            "  a          show every session in full / return to the fold",
            "  + / -      change how many rows stay expanded",
            "  r          force a rerank now (costs one model call)",
            "  m          toggle mouse reporting; turn it off to select text",
            "  o          open the highest-priority session's window",
            "  q          quit the dashboard",
            "",
            "  Colours match the iTerm2 window each session runs in.",
            "  A waited-time turns red past one hour.",
            "",
        ]
        for text in body:
            lines.append(Line(DIM + fg(FAINT) + truncate(text, self.cols - 1) + RESET))

    def compose(self, data: Dict) -> List[Line]:
        now = time.time()
        lines: List[Line] = []
        self._header(data, lines, now)

        if self.help_open:
            self._help(lines)
            self._footer(lines)
            return lines

        ordered = self._ordered(data)
        if not ordered:
            lines.append(Line(""))
            lines.append(Line("   " + DIM + fg(GREY) + truncate(
                "No Claude sessions running. Open one in any iTerm2 window.",
                self.cols - 4) + RESET))
            lines.append(Line(""))
            self._footer(lines)
            return lines

        action = [r for r in ordered if r.get("action_needed")]
        top = ordered if self.show_all else (action[:self.top_n] or ordered[:1])
        rest = [r for r in ordered if r not in top]

        if not action:
            lines.append(Line("   " + fg("#5AA469") + "Nothing needs you right now." + RESET
                              + DIM + fg(GREY) + truncate("  Showing the busiest session.",
                                                          max(0, self.cols - 34)) + RESET))
            lines.append(Line(""))

        for i, rec in enumerate(top, start=1):
            self._full_row(rec, i, data, lines, now)

        if rest:
            waiting = len([r for r in rest if r.get("action_needed")])
            label = "%d more" % len(rest)
            if waiting:
                label += " · %d also waiting" % waiting
            self._divider(label, lines)
            for rec in rest:
                self._collapsed_row(rec, data, lines, now)

        lines.append(Line(""))
        self._footer(lines)
        return lines

    def paint(self, data: Dict):
        lines = self.compose(data)
        self.line_owner = {}
        buf = [CSI + "H"]
        limit = self.rows
        for row in range(limit):
            if row < len(lines):
                buf.append(CSI + "%d;1H" % (row + 1))
                buf.append(lines[row].text)
                buf.append(CSI + "K")
                if lines[row].session_id:
                    self.line_owner[row + 1] = lines[row].session_id
            else:
                buf.append(CSI + "%d;1H" % (row + 1))
                buf.append(CSI + "K")
        self._write("".join(buf))

    # -- input -----------------------------------------------------------------

    def _handle_mouse(self, button: int, col: int, row: int, pressed: str) -> bool:
        if button & 64:                      # wheel: not ours, and never hover
            return False
        owner = self.line_owner.get(row)
        motion = bool(button & 32)
        if not motion and pressed == "M" and (button & 3) == 0:
            # A left click on a row jumps to that session's iTerm2 window.
            if owner:
                self.hover_id = owner
                self._focus_session(owner)
            return True
        if owner != self.hover_id:
            self.hover_id = owner
            self.hover_row = row
            return True
        return False

    def _note(self, text: str, seconds: float = 3.0):
        self.status_note = text
        self.status_until = time.time() + seconds

    def _handle_key(self, key: str) -> bool:
        if key in ("q", "Q", "\x03", "\x04"):
            raise KeyboardInterrupt
        if key in ("a", "A"):
            self.show_all = not self.show_all
            return True
        if key == "+" or key == "=":
            self.top_n = min(12, self.top_n + 1)
            config.save_config(top_n=self.top_n)
            return True
        if key == "-" or key == "_":
            self.top_n = max(1, self.top_n - 1)
            config.save_config(top_n=self.top_n)
            return True
        if key in ("r", "R"):
            ranker.request_rank()
            ranker.spawn_worker()
            self._note("rerank requested")
            return True
        if key in ("m", "M"):
            self.mouse_enabled = not self.mouse_enabled
            self._write(MOUSE_ON if self.mouse_enabled else MOUSE_OFF)
            if not self.mouse_enabled:
                self.hover_id = None
            self._note("mouse reporting %s" % ("on" if self.mouse_enabled else "off - select text freely"))
            return True
        if key in ("?", "h", "H"):
            self.help_open = not self.help_open
            return True
        if key in ("o", "O"):
            self._focus_top()
            return True
        return False

    def _focus_top(self):
        data = state.read()
        ordered = self._ordered(data)
        target = next((r for r in ordered if r.get("action_needed")), None) or \
            (ordered[0] if ordered else None)
        if not target:
            self._note("no session to focus")
            return
        self._focus_session(target["id"])

    def _focus_session(self, session_id: str):
        rec = state.read()["sessions"].get(session_id)
        if not rec or not rec.get("iterm_session"):
            self._note("that session has no iTerm2 window on record")
            return
        from . import iterm_focus
        if iterm_focus.focus(rec["iterm_session"]):
            self._note("brought %s to the front" % rec["name"])
        else:
            self._note("could not reach that window")

    def _mark_looked(self):
        try:
            state.mark_looked()
        except Exception:
            pass

    def _read_input(self, timeout: float) -> bool:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return False
        try:
            chunk = os.read(sys.stdin.fileno(), 4096).decode("utf-8", "replace")
        except OSError:
            return False
        dirty = False
        i = 0
        while i < len(chunk):
            rest = chunk[i:]
            match = _SGR_MOUSE.match(rest)
            if match:
                button, col, row, pressed = (int(match.group(1)), int(match.group(2)),
                                             int(match.group(3)), match.group(4))
                dirty |= self._handle_mouse(button, col, row, pressed)
                i += match.end()
                continue
            if rest.startswith(ESC + "[I"):
                self._mark_looked()
                dirty = True
                i += 3
                continue
            if rest.startswith(ESC + "[O"):
                self._mark_looked()
                dirty = True
                i += 3
                continue
            if rest.startswith(ESC + "["):
                end = 2
                while end < len(rest) and not rest[end].isalpha():
                    end += 1
                i += min(end + 1, len(rest))
                continue
            try:
                dirty |= self._handle_key(rest[0])
            except KeyboardInterrupt:
                raise
            i += 1
        return dirty

    # -- main loop -------------------------------------------------------------

    def run(self) -> int:
        config.ensure_dirs()
        self._measure()
        resized = {"flag": False}

        def on_winch(_signum, _frame):
            resized["flag"] = True

        old_winch = signal.signal(signal.SIGWINCH, on_winch)
        fd = sys.stdin.fileno()
        try:
            old_term = termios.tcgetattr(fd)
        except termios.error:
            sys.stderr.write("agentdash: the dashboard needs a real terminal (a TTY).\n")
            return 2

        register_dashboard_session()
        self._mark_looked()
        try:
            tty.setraw(fd)
            self._enter()
            last_paint = 0.0
            while True:
                data = self._load_state()
                dirty = self._read_input(0.12)
                if resized["flag"]:
                    resized["flag"] = False
                    self._measure()
                    dirty = True
                    self._write(CSI + "2J")
                now = time.time()
                if dirty or data.get("rev") != self.last_rev or now - last_paint > 0.9:
                    self.last_rev = data.get("rev")
                    self.paint(data)
                    last_paint = now
        except KeyboardInterrupt:
            return 0
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
            except termios.error:
                pass
            self._leave()
            signal.signal(signal.SIGWINCH, old_winch)
            unregister_dashboard_session()
        return 0


def register_dashboard_session() -> None:
    """Tell the daemon this window is the dashboard so it stays out of the
    palette and keeps its neutral background."""
    from .report import iterm_session_uuid
    uuid = iterm_session_uuid()
    if not uuid:
        return

    def mutate(data):
        current = data["meta"].setdefault("dashboard_iterm_sessions", [])
        if uuid in current:
            return False
        current.append(uuid)
        return True

    state.update(mutate)
    try:
        from . import daemon_client
        daemon_client.repaint(timeout=3.0)
    except Exception:
        pass


def unregister_dashboard_session() -> None:
    from .report import iterm_session_uuid
    uuid = iterm_session_uuid()
    if not uuid:
        return

    def mutate(data):
        current = data["meta"].get("dashboard_iterm_sessions") or []
        if uuid not in current:
            return False
        current.remove(uuid)
        data["meta"]["dashboard_iterm_sessions"] = current
        return True

    state.update(mutate)


def run() -> int:
    return Dashboard().run()
