"""The dashboard: a live terminal UI meant to own one iTerm2 window forever.

Design notes
------------
* By default only the top few rows render in full. Everything else collapses to
  one dim line under a divider, so the eye lands on what actually needs a human.
* Every session is numbered, and pressing that number toggles the four private
  sentences it wrote for the ranking agent. Nothing is revealed by pointing at
  it: mouse reporting is off by default, so text selection works normally and
  private context never opens by accident.
* The top strip is one number per session - tokens per minute - in that
  session's colour and in the same order as the cards, so it is obvious at a
  glance which agent is actually working.
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

from . import cat as cat_module
from . import config, palette, pressure, ranker, state, usage

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
    """One rendered line, the session it belongs to, and whether it is a gutter.

    Gutters are the blank lines between cards. They are the only place the cat
    is allowed to be, which is what makes "never overlaps a card" structural
    rather than a rule that has to be enforced.
    """
    __slots__ = ("text", "session_id", "gutter")

    def __init__(self, text: str, session_id: Optional[str] = None,
                 gutter: Optional[int] = None):
        self.text = text
        self.session_id = session_id
        self.gutter = gutter


class Dashboard:
    def __init__(self):
        self.cfg = config.load_config()
        self.top_n = int(self.cfg.get("top_n", config.TOP_N_DEFAULT))
        self.red_after = float(self.cfg.get("blocked_red_seconds", config.BLOCKED_RED_SECONDS))
        self.show_all = False
        # Opened explicitly with a number key. Never by pointing at something:
        # revealing private context by accident is worse than a second keypress.
        self.open_id: Optional[str] = None
        self.index_map: Dict[int, str] = {}
        self.cat = cat_module.Cat() if self.cfg.get("cat", True) else None
        self.cat_rows: Tuple[int, int] = (-1, -1)
        self._cat_ctx = None
        self._started = time.time()
        # Motion reporting exists for exactly one purpose now: petting the cat.
        # It opens nothing, commits to nothing and changes nothing.
        self.mouse_enabled = bool(self.cat)
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
        if rk.get("tokens"):
            bits.append("%s tok" % usage.human(rk["tokens"]))
        if rk.get("cost_usd"):
            bits.append("$%.2f" % rk["cost_usd"])
        if self.cat:
            share = self.cat.cost_fraction(max(1.0, now - self._started)) * 100.0
            bits.append("cat %.2f%%" % share)
        if rk.get("retired"):
            bits.append("%d refresh%s" % (rk["retired"], "" if rk["retired"] == 1 else "es"))
        left = " " + " · ".join(bits)
        mem = pressure.read()
        deferred, why = pressure.should_defer(self.cfg, mem)
        shown_left = False
        if deferred or mem.get("level") in (pressure.WARNING, pressure.CRITICAL):
            bits.append(pressure.describe(mem))
            left = " " + " · ".join(bits)
            shown_left = True

        warn = ranker.staleness(rk, data, now)
        if deferred:
            # The pressure itself is already on the left; do not say it twice.
            warn = "ranking paused" if shown_left else why
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

    # -- coloured row blocks ---------------------------------------------------
    #
    # Each session's row is painted as a filled rectangle in the colour of the
    # iTerm2 window it runs in, so the dashboard reads as a stack of cards that
    # match the windows on screen. Every line of a row is padded to the full
    # terminal width, and the blank line between rows is left unpainted so the
    # cards separate cleanly.

    def _row_palette(self, colour: str, dim: bool = False) -> Dict[str, str]:
        ground = palette.lighten(colour, 0.80) if dim else colour
        ink = palette.readable_fg(ground)
        return {
            "bg": ground,
            "ink": ink,
            "muted": palette.blend(ink, ground, 0.42),
            "faint": palette.blend(ink, ground, 0.62),
            "chip": palette.lighten(colour, 2.1 if not dim else 1.7),
        }

    def _paint_row(self, segments: List[Tuple[str, int]], skin: Dict,
                   sid: str, lines: List[Line]) -> None:
        """Join pre-measured segments and pad the line out to the full width."""
        used = sum(w for _, w in segments)
        body = "".join(text for text, _ in segments)
        pad = max(0, self.cols - used)
        lines.append(Line(bg(skin["bg"]) + body + " " * pad + RESET, sid))

    def _waited_segment(self, rec: Dict, skin: Dict, now: float) -> Tuple[str, int]:
        if not rec.get("blocked_since"):
            return fg(skin["faint"]) + "running" + RESET + bg(skin["bg"]), 7
        waited = now - rec["blocked_since"]
        text = "waited " + fmt_duration(waited)
        late = waited >= self.red_after
        style = (BOLD + fg(RED)) if late else fg(AMBER)
        return style + text + RESET + bg(skin["bg"]), width_of(text)

    def _chip(self, label: str, skin: Dict) -> Tuple[str, int]:
        chip = skin["chip"]
        return (bg(chip) + fg(palette.readable_fg(chip)) + " " + label + " "
                + RESET + bg(skin["bg"])), width_of(label) + 2

    def _token_strip(self, ordered: List[Dict], lines: List[Line]):
        """One number per session, in its own colour, on the plain terminal
        background. Same order and same numbering as the cards below, so the
        strip, the rows and the number keys all agree."""
        if not ordered:
            return
        label = " tok/min "
        cells = []
        for i, rec in enumerate(ordered, start=1):
            colour = palette.lighten(rec.get("color") or "#666666", 2.4)
            rate = rec.get("tokens_per_min")
            if rec.get("container") and not rec.get("transcript_path"):
                text = "%d %s" % (i, "n/a")          # no transcript to meter
            elif rate is None:
                text = "%d %s" % (i, "-")
            else:
                text = "%d %s" % (i, usage.human(rate, "/m"))
            cells.append((text, colour))

        room = self.cols - width_of(label) - 1
        shown, used = [], 0
        for text, colour in cells:
            need = width_of(text) + (2 if shown else 0)
            if used + need > room:
                break
            shown.append((text, colour, need - width_of(text)))
            used += need
        out = [DIM + fg(FAINT) + label + RESET]
        cursor = width_of(label)
        self.strip_x = {}
        for i, (text, colour, gap) in enumerate(shown, start=1):
            out.append(" " * gap + fg(colour) + BOLD + text + RESET)
            self.strip_x[i] = cursor + gap
            cursor += gap + width_of(text)
        hidden = len(cells) - len(shown)
        if hidden and used + 6 <= room:
            out.append(DIM + fg(GREY) + "  +%d" % hidden + RESET)
        lines.append(Line("".join(out)))
        lines.append(Line(""))

    def _full_row(self, rec: Dict, index: int, data: Dict, lines: List[Line], now: float):
        sid = rec["id"]
        colour = rec.get("color") or "#555555"
        skin = self._row_palette(colour)
        name = rec.get("name") or "?"
        new = (rec.get("last_report") or 0) > (data["meta"].get("last_look") or 0)
        no_report = not rec.get("self_reported", True)

        waited_txt, waited_w = self._waited_segment(rec, skin, now)

        # Fit by dropping the least important decorations first, then shrinking
        # the name, rather than letting anything spill past `cols`.
        prefix_w = 1 + len(str(index)) + 2
        for want_note, want_new, want_tag in ((True, True, True), (False, True, True),
                                              (False, False, True), (False, False, False)):
            note_w = 12 if (no_report and want_note) else 0
            new_w = 4 if (new and want_new) else 0
            fixed = prefix_w + note_w + new_w + waited_w + 1
            tag_room = self.cols - fixed - width_of(name) - 3
            tag_label = (truncate(rec.get("tag") or "unknown", max(3, min(16, tag_room)))
                         if (want_tag and tag_room >= 5) else "")
            tag_w = (width_of(tag_label) + 2) if tag_label else 0
            name_budget = self.cols - fixed - tag_w - 1
            if name_budget >= 6 or not want_tag:
                shown = truncate(name, max(3, name_budget))
                break

        segs: List[Tuple[str, int]] = [
            (bg(skin["bg"]) + fg(skin["faint"]) + " %d  " % index + RESET + bg(skin["bg"]),
             prefix_w),
            (BOLD + fg(skin["ink"]) + shown + RESET + bg(skin["bg"]), width_of(shown)),
        ]
        if no_report and want_note:
            segs.append((fg(skin["faint"]) + " (no report)" + RESET + bg(skin["bg"]), 12))
        if new and want_new:
            segs.append((" " + BOLD + fg(AMBER) + "NEW" + RESET + bg(skin["bg"]), 4))

        used = sum(w for _, w in segs)
        gap = max(1, self.cols - used - waited_w - (width_of(tag_label) + 3 if tag_label else 0))
        segs.append((" " * gap, gap))
        segs.append((waited_txt, waited_w))
        if tag_label:
            segs.append((" ", 1))
            segs.append(self._chip(tag_label, skin))
        self._paint_row(segs, skin, sid, lines)

        indent = "     "
        body_width = max(20, self.cols - len(indent) - 1)
        summary = rec.get("summary") or "No summary reported yet."
        for text in wrap(summary, body_width):
            self._paint_row([(indent, len(indent)),
                             (fg(skin["ink"]) + text + RESET + bg(skin["bg"]), width_of(text))],
                            skin, sid, lines)

        meta_bits = [rec.get("repo") or rec.get("cwd") or "?"]
        if rec.get("rank_reason"):
            meta_bits.append("why: " + rec["rank_reason"])
        meta = truncate(" · ".join(meta_bits), body_width)
        self._paint_row([(indent, len(indent)),
                         (fg(skin["faint"]) + meta + RESET + bg(skin["bg"]), width_of(meta))],
                        skin, sid, lines)

        if self.open_id == sid:
            self._private_block(rec, indent, body_width, skin, lines, sid)
        lines.append(Line("", sid))

    def _private_block(self, rec: Dict, indent: str, body_width: int, skin: Dict,
                       lines: List[Line], sid: str):
        rule = palette.blend(skin["ink"], skin["bg"], 0.55)

        def bar(text_styled, visible):
            self._paint_row([(indent, len(indent)),
                             (fg(rule) + "\u2502 " + RESET + bg(skin["bg"]), 2),
                             (text_styled, visible)], skin, sid, lines)

        head = "ranker-only context"
        bar(ITALIC + fg(skin["faint"]) + head + RESET + bg(skin["bg"]), width_of(head))
        ctx = rec.get("ranker_context")
        if not ctx:
            miss = "(this session has not supplied any)"
            bar(fg(skin["faint"]) + miss + RESET + bg(skin["bg"]), width_of(miss))
            return
        for text in wrap(ctx, max(8, body_width - 2)):
            bar(ITALIC + fg(skin["muted"]) + text + RESET + bg(skin["bg"]), width_of(text))

    def _collapsed_row(self, rec: Dict, index: int, data: Dict, lines: List[Line], now: float):
        sid = rec["id"]
        colour = rec.get("color") or "#555555"
        skin = self._row_palette(colour, dim=True)
        name = rec.get("name") or "?"
        tag = (rec.get("tag") or "-")[:14]
        waited = (fmt_duration(now - rec["blocked_since"], short=True)
                  if rec.get("blocked_since") else "\u2014")
        overdue = rec.get("blocked_since") and (now - rec["blocked_since"]) >= self.red_after
        new = (rec.get("last_report") or 0) > (data["meta"].get("last_look") or 0)

        prefix = " %d " % index if index < 10 else "%d " % index
        budget = self.cols - 4 - width_of(prefix) - width_of(waited) - (4 if new else 0)
        name_col = max(6, min(24, budget // 2))
        tag_col = max(0, min(15, budget - name_col - 1))

        segs: List[Tuple[str, int]] = [
            (bg(skin["bg"]) + fg(skin["faint"]) + prefix + RESET + bg(skin["bg"]),
             width_of(prefix)),
            (fg(skin["ink"] if new else skin["muted"])
             + truncate(name, name_col).ljust(name_col) + RESET + bg(skin["bg"]), name_col),
        ]
        if tag_col >= 4:
            segs.append((fg(skin["faint"]) + truncate(tag, tag_col).ljust(tag_col)
                         + RESET + bg(skin["bg"]), tag_col))
        style = (BOLD + fg(RED)) if overdue else fg(skin["faint"])
        segs.append((style + waited + RESET + bg(skin["bg"]), width_of(waited)))
        if new:
            segs.append((" " + fg(AMBER) + "NEW" + RESET + bg(skin["bg"]), 4))

        summary = rec.get("summary") or ""
        used = sum(w for _, w in segs)
        room = self.cols - used - 3
        if room > 24 and summary:
            clipped = truncate(summary, room)
            segs.append(("  " + fg(skin["muted"]) + clipped + RESET + bg(skin["bg"]),
                         2 + width_of(clipped)))
        self._paint_row(segs, skin, sid, lines)

        if self.open_id == sid:
            indent = "     "
            body_width = max(20, self.cols - len(indent) - 1)
            for text in wrap(summary, body_width):
                self._paint_row([(indent, len(indent)),
                                 (fg(skin["ink"]) + text + RESET + bg(skin["bg"]),
                                  width_of(text))], skin, sid, lines)
            self._private_block(rec, indent, body_width, skin, lines, sid)
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
        ("1-9", "open context", "context"),
        ("pet", "the cat", "cat"),
        ("o", "jump to window", "jump"),
        ("q", "quit", "quit"),
        ("a", "all/fold", "all"),
        ("r", "rerank", "rerank"),
        ("m", "mouse", "mouse"),
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
            "  1 - 9      open or close that session's ranker-only context",
            "  o          bring the top session's iTerm2 window to the front",
            "  m          mouse reporting; turn it off to select text with the mouse",
            "",
            "  The cat lives in the gaps between cards, so it can never cover one.",
            "  It sits beside a session shortly before that timer turns red, walks",
            "  faster the more sessions are open, and gets sleepy late or after a",
            "  long day. Hovering it sends hearts and does nothing else at all.",
            "  Turn it off with {\"cat\": false} in ~/.agent-dashboard/config.json.",
            "  a          show every session in full / return to the fold",
            "  + / -      change how many rows stay expanded",
            "  r          force a rerank now (costs one model call)",
            "  o          open the highest-priority session's window",
            "  q          quit the dashboard",
            "",
            "  The top strip is tokens per minute per session, same order,",
            "  same colours. It excludes cache reads, so a session re-reading a",
            "  large context does not look busy while producing nothing.",
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
            self.cat_rows = (-1, -1)
            return lines

        action = [r for r in ordered if r.get("action_needed")]
        top = ordered if self.show_all else (action[:self.top_n] or ordered[:1])
        rest = [r for r in ordered if r not in top]

        # One numbering across the strip, the cards and the number keys.
        numbered = top + rest
        self.index_map = {i: r["id"] for i, r in enumerate(numbered, start=1)}
        if self.open_id not in {r["id"] for r in ordered}:
            self.open_id = None

        # The strip is built first so its column positions are known, but it is
        # appended after the cat's lane: the cat sits above everything, and
        # above the number of whichever session it is watching.
        strip: List[Line] = []
        self._token_strip(numbered, strip)
        self._cat_lane(lines, data, numbered, now)
        lines.extend(strip)

        if not action:
            lines.append(Line("   " + fg("#5AA469") + "Nothing needs you right now." + RESET
                              + DIM + fg(GREY) + truncate("  Showing the busiest session.",
                                                          max(0, self.cols - 34)) + RESET))
            lines.append(Line(""))

        for i, rec in enumerate(top, start=1):
            self._full_row(rec, i, data, lines, now)

        if rest:
            offset = len(top)
            waiting = len([r for r in rest if r.get("action_needed")])
            label = "%d more" % len(rest)
            if waiting:
                label += " · %d also waiting" % waiting
            self._divider(label, lines)
            for i, rec in enumerate(rest, start=offset + 1):
                self._collapsed_row(rec, i, data, lines, now)

        lines.append(Line(""))
        self._footer(lines)
        return lines

    def _cat_lane(self, lines: List[Line], data: Dict, numbered: List[Dict],
                  now: float) -> None:
        """The cat's own three rows, above every card.

        It cannot overlap a card because it is never on one; and it settles
        above the strip cell of whichever session is closest to going red, so
        "beside the one about to need you" is literally where it points.
        """
        self.cat_rows = (-1, -1)
        if not self.cat:
            return
        mem = pressure.read()
        self.cat.update(
            data, numbered, self.cols, self.red_after,
            decision_open=bool(self.open_id),
            under_pressure=pressure.should_defer(self.cfg, mem)[0],
            targets=getattr(self, "strip_x", {}), now=now)
        first = len(lines) + 1
        for text in self.cat.draw(self.cols):
            lines.append(Line(text))
        self.cat_rows = (first, first + cat_module.HEIGHT_ROWS - 1)
        self._cat_ctx = (data, numbered, first)

    def tick_cat(self, now: float) -> None:
        """Animate the cat without recomposing anything else.

        Only its own rows are rewritten, which is what keeps eight frames a
        second honest against the cost claim in the header.
        """
        if not self.cat or not self._cat_ctx or self.cat_rows[0] < 1:
            return
        data, numbered, first = self._cat_ctx
        mem = pressure.read()
        changed = self.cat.update(
            data, numbered, self.cols, self.red_after,
            decision_open=bool(self.open_id),
            under_pressure=pressure.should_defer(self.cfg, mem)[0],
            targets=getattr(self, "strip_x", {}), now=now)
        if not changed:
            return
        buf = []
        for offset, text in enumerate(self.cat.draw(self.cols)):
            row = first + offset
            if row > self.rows:
                return
            buf.append(CSI + "%d;1H" % row + RESET + text + CSI + "K")
        self._write("".join(buf))

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
        """Mouse reporting is off unless asked for, and even then only a
        deliberate click does anything. Motion is ignored outright."""
        if button & 64:                          # wheel
            return False
        if button & 32:                          # motion: only ever pets the cat
            if self.cat and self.cat_rows[0] <= row <= self.cat_rows[1]:
                within = row - self.cat_rows[0]
                if self.cat.covers(col - 1, within):
                    return self.cat.pet()
            return False
        if pressed == "M" and (button & 3) == 0:
            owner = self.line_owner.get(row)
            if owner:
                self._focus_session(owner)
                return True
        return False

    def _note(self, text: str, seconds: float = 3.0):
        self.status_note = text
        self.status_until = time.time() + seconds

    def _handle_key(self, key: str) -> bool:
        if key in ("q", "Q", "\x03", "\x04"):
            raise KeyboardInterrupt
        if key.isdigit():
            index = 10 if key == "0" else int(key)
            sid = self.index_map.get(index)
            if sid:
                self.open_id = None if self.open_id == sid else sid
            else:
                self._note("no session %d" % index)
            return True
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
            self._note("mouse %s" % ("on - click a row to jump to its window"
                                     if self.mouse_enabled else "off - select text freely"))
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
            last_cat = 0.0
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
                    last_cat = now
                elif self.cat and now - last_cat >= self.cat.interval:
                    self.tick_cat(now)
                    last_cat = now
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
