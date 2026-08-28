"""An orange cat that lives in the gaps between the cards.

Three rules, in order of importance.

*It yields.* The cat only ever occupies a gutter - the blank line between two
cards - so it can never overlap a summary, a tag or a waiting timer. It leaves
the gutter of any session whose timer has already gone red, and it stops moving
entirely while a decision is expanded on screen. If something needs attention,
the cat is somewhere else.

*It costs nothing.* Eight frames a second at most, only the two lines it
occupies are redrawn, and it freezes completely when the machine is under
memory pressure. The time it spends is measured and shown in the header, which
is both a joke and the proof.

*It has an off switch.* `{"cat": false}` in the config. No dialogue, no guilt.

Beyond that it carries three signals that are true but awkward to say in words:
it sits beside a session shortly before that session's timer turns red; its pace
tracks how many sessions are open, so fragmentation is felt rather than counted;
and it gets sleepy late at night, after a long day, or when work is being sent
back a lot. There is deliberately no fourth behaviour - each one added makes the
others harder to read.
"""
import random
import time
from typing import Dict, List, Optional, Tuple

from . import palette

# --- sprite -------------------------------------------------------------------
# Four pixel rows, drawn two-per-terminal-row with half blocks, so the cat gets
# real per-pixel colour on any of the eight card backgrounds.
#   o rim   B body   L belly   e eye   n muzzle   ^ happy eye
FRAMES: Dict[str, List[str]] = {
    # Standing, looking around. A cat pauses constantly; this is the pause.
    "idle":   ["..........o..o..",
               ".........oooooo.",
               "..oo.....oBBBBBo",
               ".oBBo....oBeBBeB",
               ".oBBo....oBBnBBo",
               ".oBBo.oooBBBBBo.",
               "..oBBoBBBBBBBo..",
               "...oBBLLLLLLBo..",
               "...oBLLLLLLLBo..",
               "....oo.oo.oo.o.."],
    "walk_a": ["..........o..o..",
               ".........oooooo.",
               "...oo....oBBBBBo",
               "..oBBo...oBeBBeB",
               "..oBBo...oBBnBBo",
               "...oBBoooBBBBBo.",
               "....oBBBBBBBBo..",
               "...oBLLLLLLLBo..",
               "...oBLLLLLLLBo..",
               "...oo.oo.oo..o.."],
    "walk_b": ["..........o..o..",
               ".........oooooo.",
               "..oo.....oBBBBBo",
               ".oBBo....oBeBBeB",
               ".oBBo....oBBnBBo",
               "..oBBo.ooBBBBBo.",
               "...oBBBBBBBBBo..",
               "...oBLLLLLLLBo..",
               "...oBLLLLLLLBo..",
               "..oo..oo..oo.o.."],
    # Sitting up, tail wrapped round: the "something is about to need you" pose.
    "sit":    ["..........o..o..",
               ".........oooooo.",
               "..oo.....oBBBBBo",
               ".oBBo....oBeBBeB",
               ".oBBo....oBBnBBo",
               ".oBBo....oBBBBo.",
               "..oBBo..oBBBBBo.",
               "...oBBooBLLLLBo.",
               "...oBBBBLLLLLBo.",
               "....oooooooooo.."],
    "sleep":  ["................",
               "................",
               "................",
               "..........o..o..",
               ".........oooooo.",
               "..ooo....oBBBBBo",
               ".oBBBooooBBnBBo.",
               ".oBBBBBBBBBBBo..",
               "..oBLLLLLLLLBo..",
               "...ooooooooooo.."],
    "happy":  ["..........o..o..",
               ".........oooooo.",
               "..oo.....oBBBBBo",
               ".oBBo....oB^BB^B",
               ".oBBo....oBBnBBo",
               ".oBBo.oooBBBBBo.",
               "..oBBoBBBBBBBo..",
               "...oBBLLLLLLBo..",
               "...oBLLLLLLLBo..",
               "....oo.oo.oo.o.."],
}
WIDTH = 16
HEIGHT_ROWS = 5          # ten pixel rows, two per terminal row

# Orange, but with a dark rim and a pale belly: flat orange disappears against
# the docs card, whose colour is almost the same hue.
# The rim has two jobs that pull against each other: stay visible on the black
# terminal background the cat's lane sits on, and stay darker than the body so
# it still reads as an outline. #2E1608 was almost black and lost the first job
# entirely - contrast 1.16 against the terminal. This is 3.07 against black and
# still 2.30 against the body.
INK = {
    "o": "#8F4E23",      # rim - visible on black, still darker than the body
    "B": "#E8802A",      # body
    "L": "#F8CE97",      # belly
    "e": "#1B0D04",      # eye
    "n": "#C96A4E",      # muzzle when asleep
    "d": "#8FD3F4",      # the sweat drop, when there are too many sessions
    "^": "#1B0D04",      # closed happy eye
}
HEART = "#FF6B8B"

ESC = "\x1b["


def _fg(hex_value: str) -> str:
    r, g, b = palette.hex_to_rgb(hex_value)
    return ESC + "38;2;%d;%d;%dm" % (r, g, b)


def _bg(hex_value: str) -> str:
    r, g, b = palette.hex_to_rgb(hex_value)
    return ESC + "48;2;%d;%d;%dm" % (r, g, b)


RESET = ESC + "0m"


# Where the drop sits: just behind the head, in the two transparent cells above
# the shoulder. Overlaid before mirroring, so it follows the cat round.
DROP_COL = 8
DROP_ROWS = (0, 1)


def _pixels(frame: str, facing: int, worried: bool = False) -> List[str]:
    rows = list(FRAMES[frame])
    if worried:
        for r in DROP_ROWS:
            if rows[r][DROP_COL] == ".":
                rows[r] = rows[r][:DROP_COL] + "d" + rows[r][DROP_COL + 1:]
    if facing < 0:
        rows = [r[::-1] for r in rows]
    return rows


def render(frame: str, facing: int, worried: bool = False) -> List[str]:
    """Five terminal rows of styled text, no padding, transparent background."""
    rows = _pixels(frame, facing, worried)
    out = []
    for pair in ((0, 1), (2, 3), (4, 5), (6, 7), (8, 9)):
        top_row, bottom_row = rows[pair[0]], rows[pair[1]]
        chunks = []
        for col in range(WIDTH):
            top, bottom = top_row[col], bottom_row[col]
            if top == "." and bottom == ".":
                chunks.append(RESET + " ")
            elif bottom == ".":
                chunks.append(RESET + _fg(INK[top]) + "▀")
            elif top == ".":
                chunks.append(RESET + _fg(INK[bottom]) + "▄")
            else:
                chunks.append(_bg(INK[bottom]) + _fg(INK[top]) + "▀")
        out.append("".join(chunks) + RESET)
    return out


# --- behaviour ------------------------------------------------------------------

SLEEPY_HOURS = (23, 0, 1, 2, 3, 4, 5)
LONG_DAY_SECONDS = 6 * 3600
REVERSALS_WINDOW = 2 * 3600
REVERSALS_SLEEPY = 4
# Sit beside a session once it is this far into its hour, before it goes red.
EARLY_WARNING_FRACTION = 0.75
# Session count already drives the cat's pace. Past this many it also shows on
# its face, which is the same signal in a register you can read at a glance
# rather than only in peripheral motion. It is not a fourth behaviour.
WORRIED_SESSIONS = 5


class Cat:
    def __init__(self, fps: float = 8.0):
        self.enabled = True
        self.interval = 1.0 / max(1.0, fps)
        self.x = 2.0
        self.facing = 1
        self.frame = "walk_a"
        self.mood = "walk"
        self.hearts: List[List] = []          # [x, born]
        self._last_step = 0.0
        self._last_pet = 0.0
        self._flip = False
        self._pause_until = 0.0
        self.worried = False
        self.seconds = 0.0                    # total time spent being a cat
        self.frozen = False

    # -- signals ---------------------------------------------------------------

    def _sleepy(self, data: Dict, now: float) -> bool:
        if time.localtime(now).tm_hour in SLEEPY_HOURS:
            return True
        sessions = list(data.get("sessions", {}).values())
        if sessions:
            oldest = min(s.get("started") or now for s in sessions)
            if now - oldest > LONG_DAY_SECONDS:
                return True
        reversals = sum(
            len([t for t in (s.get("reopen_times") or []) if now - t < REVERSALS_WINDOW])
            for s in sessions)
        return reversals >= REVERSALS_SLEEPY

    def _pace(self, session_count: int) -> float:
        """Ambling at two sessions, trotting at six. Read in peripheral vision."""
        steps_per_second = 0.8 + 0.45 * max(0, session_count - 1)
        return 1.0 / min(6.0, max(0.5, steps_per_second))

    def warning_index(self, ordered: List[Dict], red_after: float,
                      now: float) -> Optional[int]:
        """Which session is closest to its timer turning red, if any.

        Already-red sessions are excluded: the point is the warning before the
        line is crossed, and once it is crossed the cat only adds noise.
        """
        best, best_waited = None, 0.0
        for index, rec in enumerate(ordered):
            since = rec.get("blocked_since")
            if not since:
                continue
            waited = now - since
            if waited >= red_after:
                continue
            if waited >= red_after * EARLY_WARNING_FRACTION and waited > best_waited:
                best, best_waited = index, waited
        return best

    # -- update ----------------------------------------------------------------

    def update(self, data: Dict, ordered: List[Dict], cols: int, red_after: float,
               decision_open: bool, pressure_level: str = "normal",
               targets: Optional[Dict[int, int]] = None,
               now: Optional[float] = None) -> bool:
        """Advance the cat. Returns True if anything visible changed."""
        started = time.perf_counter()
        try:
            now = now or time.time()

            # Hearts expire before anything else, so a frozen cat does not sit
            # there surrounded by them until the freeze lifts.
            changed = False
            if self.hearts:
                before = len(self.hearts)
                self.hearts = [h for h in self.hearts if now - h[1] < 1.6]
                changed = len(self.hearts) != before

            # Rule one: yield. Nothing moves while a decision is expanded.
            #
            # Memory is a matter of degree. The ranker stands down at the
            # kernel's *warning* level because it spawns a large process; the
            # cat costs a few string operations, and freezing it at warning
            # means it is frozen almost permanently on a busy machine, which
            # reads as broken rather than considerate. So it slows at warning
            # and stops dead at critical.
            self.frozen = decision_open or pressure_level == "critical"
            if self.frozen:
                return changed
            drag = 2.0 if pressure_level == "warning" else 1.0

            petted = now - self._last_pet < 1.4
            sleepy = self._sleepy(data, now)
            warn = self.warning_index(ordered, red_after, now)

            if petted:
                mood = "happy"
            elif sleepy:
                mood = "sleep"
            elif warn is not None:
                mood = "sit"
            else:
                mood = "walk"
            if mood != self.mood:
                self.mood = mood
                changed = True

            # Not while asleep or being petted: a sleeping cat with a sweat
            # drop, or a purring one, muddles both signals at once.
            worried = (len(ordered) >= WORRIED_SESSIONS
                       and mood not in ("sleep", "happy"))
            if worried != self.worried:
                self.worried = worried
                changed = True

            limit = float(max(1, cols - WIDTH - 2))
            # A sleepy cat still settles beside whoever is closest to going red:
            # two signals in one behaviour rather than a fourth to learn.
            target = (targets or {}).get(warn) if warn is not None else None

            if mood in ("sit", "sleep") and target is not None:
                goal = float(min(max(1, target), int(limit)))
                if abs(self.x - goal) > 0.5:
                    if now - self._last_step >= self._pace(len(ordered)) * drag:
                        self._last_step = now
                        step = 1.0 if goal > self.x else -1.0
                        self.facing = 1 if step > 0 else -1
                        self.x += step
                        self._flip = not self._flip
                        self.frame = "walk_b" if self._flip else "walk_a"
                        changed = True
                    return changed
                self.x = goal

            if mood == "walk":
                # Curiosity: a cat crossing a room stops, looks around, and
                # sometimes turns back. Without this it is a sprite on rails.
                if now < self._pause_until:
                    if self.frame != "idle":
                        self.frame = "idle"
                        changed = True
                    return changed
                if now - self._last_step >= self._pace(len(ordered)) * drag:
                    self._last_step = now
                    if random.random() < 0.06:
                        self._pause_until = now + random.uniform(0.8, 2.4)
                        self.frame = "idle"
                        return True
                    if random.random() < 0.04:
                        self.facing *= -1          # changed its mind
                    self.x += self.facing
                    if self.x <= 1 or self.x >= limit:
                        self.facing *= -1
                        self.x = min(max(1.0, self.x), limit)
                    self._flip = not self._flip
                    self.frame = "walk_b" if self._flip else "walk_a"
                    changed = True
            else:
                self.frame = {"sit": "sit", "sleep": "sleep", "happy": "happy"}[mood]
                self.x = min(max(1.0, self.x), limit)
            return changed
        finally:
            self.seconds += time.perf_counter() - started

    # -- interaction ------------------------------------------------------------

    def covers(self, col: int, row_within: int) -> bool:
        left = int(self.x)
        return left <= col < left + WIDTH and 0 <= row_within < HEIGHT_ROWS

    def pet(self, now: Optional[float] = None) -> bool:
        """The only interaction in the whole tool with no consequence."""
        now = now or time.time()
        if now - self._last_pet < 0.25:
            return False
        self._last_pet = now
        if len(self.hearts) < 5:
            self.hearts.append([int(self.x) + random.randrange(2, WIDTH - 1), now])
        return True

    # -- drawing ----------------------------------------------------------------

    def draw(self, cols: int) -> List[str]:
        """Exactly three terminal rows, left-padded to the cat's position.

        Hearts sit in the upper row, to the right of the cat's head, so they
        never overwrite the sprite and never need a row of their own.
        """
        started = time.perf_counter()
        try:
            rows = render(self.frame, self.facing, self.worried)
            left = int(self.x)
            lines = [" " * left + r for r in rows]
            if self.hearts:
                trail = []
                for _, born in sorted(self.hearts, key=lambda h: h[1]):
                    age = time.time() - born
                    trail.append(_fg(HEART if age < 0.8 else "#B8536B") + "♥" + RESET)
                if trail and left + WIDTH + 1 + len(trail) * 2 < cols:
                    lines[0] += " " + " ".join(trail)
            if self.mood == "sleep":
                lines[0] += _fg("#7A6A5A") + "  z" + RESET
            return lines
        finally:
            self.seconds += time.perf_counter() - started

    def cost_fraction(self, uptime: float) -> float:
        return (self.seconds / uptime) if uptime > 0 else 0.0
