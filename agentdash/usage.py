"""Per-session token metering, read from Claude Code's own transcripts.

Each session's transcript is JSONL, and every assistant record carries a
`usage` block. Hooks hand us the transcript path, so the tool can meter a
session without the session doing anything - and without re-reading the file:
a byte offset is kept per session and only the newly appended bytes are parsed.

The headline number is tokens per minute over a rolling window, which is the
most honest single answer to "which of these agents is actually working".
Cache reads are excluded by default: a session re-reading a 200k context every
turn would otherwise look busy while producing nothing.
"""
import calendar
import glob
import json
import os
import time
from typing import Dict, List, Optional, Tuple

from . import config, state

WINDOW_SECONDS = 600.0        # rolling window the rate is computed over
MAX_SAMPLES = 400
# Counted by default. cache_read_input_tokens is deliberately absent.
COUNTED = ("input_tokens", "cache_creation_input_tokens", "output_tokens")


def _tokens(usage: Dict, counted=COUNTED) -> int:
    total = 0
    for key in counted:
        try:
            total += int(usage.get(key) or 0)
        except (TypeError, ValueError):
            pass
    return total


def parse_time(value, fallback: float) -> float:
    """Transcript timestamps are ISO-8601 UTC, e.g. 2026-08-28T10:44:17.404Z."""
    if not isinstance(value, str) or not value.endswith("Z"):
        return fallback
    head = value[:-1].split(".")[0]
    try:
        return calendar.timegm(time.strptime(head, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return fallback


def read_new(path: str, offset: int, now: Optional[float] = None):
    """Parse bytes appended since `offset`.

    Returns (new_offset, samples, output_tokens, assistant_records), where each
    sample is [epoch_seconds, tokens] taken from the record's own timestamp.
    Using the record time rather than the read time matters on the first pass:
    a session with hours of history would otherwise land as one enormous sample
    at "now" and read as millions of tokens per minute.

    A truncated or replaced transcript resets the offset rather than throwing.
    """
    now = now or time.time()
    try:
        size = os.path.getsize(path)
    except OSError:
        return offset, 0, 0, 0
    if size < offset:
        offset = 0                       # file was rotated or rewritten
    if size == offset:
        return offset, [], 0, 0
    samples: List = []
    output = records = 0
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(size - offset)
    except OSError:
        return offset, [], 0, 0

    # Only consume whole lines; a partially written last line is left for
    # the next pass by rewinding the offset to the last newline.
    end = chunk.rfind(b"\n")
    if end == -1:
        return offset, [], 0, 0
    consumed = chunk[:end + 1]
    for raw in consumed.split(b"\n"):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        usage = (rec.get("message") or {}).get("usage")
        if not isinstance(usage, dict):
            continue
        records += 1
        samples.append([parse_time(rec.get("timestamp"), now), _tokens(usage)])
        output += _tokens(usage, ("output_tokens",))
    return offset + len(consumed), samples, output, records


def trim(samples: List, now: Optional[float] = None) -> List:
    now = now or time.time()
    kept = [s for s in samples if now - s[0] <= WINDOW_SECONDS]
    return kept[-MAX_SAMPLES:]


def rate_per_minute(samples: List, now: Optional[float] = None) -> float:
    """Tokens per minute over the window actually covered by the samples.

    Dividing by the full window would understate a session that only started a
    minute ago, so the elapsed span is used, floored at 60s so a single large
    turn cannot read as an implausible spike.
    """
    samples = trim(samples, now)
    if not samples:
        return 0.0
    now = now or time.time()
    total = sum(s[1] for s in samples)
    span = max(60.0, now - samples[0][0])
    return total * 60.0 / span


TRANSCRIPT_GLOB = "~/.claude/projects/*/%s.jsonl"


def find_transcript(session_id: str) -> Optional[str]:
    """Locate a session's transcript from its id alone.

    Hooks hand us the path, but only from the next hook event onwards. Claude
    Code names transcripts after the session id, so a session already running
    when metering was switched on can be found immediately instead of showing
    a blank number until it happens to be prompted.
    """
    if not session_id or session_id.startswith("ct:"):
        return None                     # containerised: no transcript on this host
    matches = glob.glob(os.path.expanduser(TRANSCRIPT_GLOB % session_id))
    if not matches:
        return None
    return max(matches, key=lambda p: os.path.getmtime(p))


def meter_all(now: Optional[float] = None) -> int:
    """Update every session that has a transcript. Returns sessions touched."""
    now = now or time.time()
    data = state.read()
    pending = {}
    discovered = {}
    for sid, rec in data["sessions"].items():
        path = rec.get("transcript_path")
        if not path:
            path = find_transcript(sid)
            if not path:
                continue
            discovered[sid] = path
        new_offset, samples, output, records = read_new(
            path, rec.get("transcript_offset") or 0, now)
        if new_offset == (rec.get("transcript_offset") or 0) and not samples:
            continue
        pending[sid] = (new_offset, samples, output, records)
    if not pending and not discovered:
        return 0

    def mutate(d):
        changed = False
        for sid, path in discovered.items():
            if sid in d["sessions"]:
                d["sessions"][sid]["transcript_path"] = path
                changed = True
        for sid, (offset, new_samples, output, records) in pending.items():
            rec = d["sessions"].get(sid)
            if rec is None:
                continue
            rec["transcript_offset"] = offset
            if new_samples or output or records:
                counted = sum(sample[1] for sample in new_samples)
                rec["tokens_total"] = (rec.get("tokens_total") or 0) + counted
                rec["output_tokens_total"] = (rec.get("output_tokens_total") or 0) + output
                rec["model_turns"] = (rec.get("model_turns") or 0) + records
                merged = (rec.get("token_samples") or []) + new_samples
                merged.sort(key=lambda sample: sample[0])
                merged = trim(merged, now)
                rec["token_samples"] = merged
                rec["tokens_per_min"] = round(rate_per_minute(merged, now), 1)
            changed = True
        return changed or None

    state.update(mutate)
    return len(pending)


def decay(now: Optional[float] = None) -> None:
    """Let an idle session's rate fall to zero instead of freezing at its last
    value, which would make a finished session look permanently busy."""
    now = now or time.time()

    def mutate(d):
        changed = False
        for rec in d["sessions"].values():
            samples = rec.get("token_samples")
            if not samples:
                continue
            kept = trim(samples, now)
            new_rate = round(rate_per_minute(kept, now), 1)
            if kept != samples or new_rate != rec.get("tokens_per_min"):
                rec["token_samples"] = kept
                rec["tokens_per_min"] = new_rate
                changed = True
        return changed or None

    state.update(mutate)


def human(n: float, suffix: str = "") -> str:
    """Compact number for a one-line strip: 940, 3.4k, 12k, 1.2M."""
    n = float(n or 0)
    if n < 1000:
        return "%d%s" % (int(round(n)), suffix)
    if n < 10000:
        return "%.1fk%s" % (n / 1000.0, suffix)
    if n < 1000000:
        return "%dk%s" % (int(round(n / 1000.0)), suffix)
    return "%.1fM%s" % (n / 1000000.0, suffix)
