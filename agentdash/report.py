"""`agentdash report` - the surface a Claude session uses to post an update."""
import os
import re
import sys
import time
from typing import List, Optional, Tuple

from . import daemon_client, ranker, state

STATUSES = ("working", "done", "question", "blocked")
# Statuses other than `working` mean the session is waiting on the human.
ACTION_STATUSES = ("done", "question", "blocked")

SUGGESTED_TAGS = (
    "tests", "docs", "infra", "implementation", "debugging", "exploration",
    "web search", "refactor", "review", "design", "data", "release", "setup",
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'`\[])")


def split_sentences(text: str) -> List[str]:
    text = " ".join((text or "").split())
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def clip_sentences(text: str, want: int, label: str) -> Tuple[str, Optional[str]]:
    """Keep the first `want` sentences. Returns (text, warning)."""
    parts = split_sentences(text)
    if not parts:
        return "", None
    plural = "sentence" if len(parts) == 1 else "sentences"
    if len(parts) > want:
        return " ".join(parts[:want]), (
            "%s had %d %s; kept the first %d." % (label, len(parts), plural, want))
    if len(parts) < want:
        return " ".join(parts), (
            "%s had %d %s; %d were asked for." % (label, len(parts), plural, want))
    return " ".join(parts), None


def iterm_session_uuid() -> Optional[str]:
    raw = os.environ.get("ITERM_SESSION_ID") or os.environ.get("TERM_SESSION_ID") or ""
    if ":" in raw:
        return raw.split(":", 1)[1].strip()
    return raw.strip() or None


def resolve_window(iterm_uuid: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(window_id, colour) for the terminal we are running in."""
    if not iterm_uuid:
        return None, None
    data = state.read()
    wid = state.window_for_iterm_session(data, iterm_uuid)
    if wid:
        return wid, data["windows"][wid].get("color")
    try:
        reply = daemon_client.resolve_window(iterm_uuid)
        if reply.get("ok"):
            return reply.get("window_id"), reply.get("color")
    except daemon_client.DaemonUnavailable:
        pass
    return None, None


def submit(session_id: str, status: str, tag: Optional[str], summary: Optional[str],
           ranker_context: Optional[str], cwd: Optional[str] = None,
           self_reported: bool = True, quiet: bool = False) -> dict:
    warnings = []
    if summary is not None:
        summary, warn = clip_sentences(summary, 3, "--summary")
        if warn:
            warnings.append(warn)
    if ranker_context is not None:
        ranker_context, warn = clip_sentences(ranker_context, 4, "--context")
        if warn:
            warnings.append(warn)
    if tag:
        tag = " ".join(tag.split()).lower()[:16]

    iterm_uuid = iterm_session_uuid()
    window_id, colour = resolve_window(iterm_uuid)
    now = time.time()
    action = status in ACTION_STATUSES

    fields = {
        "status": status,
        "tag": tag,
        "summary": summary,
        "ranker_context": ranker_context,
        "cwd": cwd or os.getcwd(),
        "window_id": window_id,
        "color": colour,
        "iterm_session": iterm_uuid,
        "last_report": now,
        "self_reported": self_reported,
    }
    fields["repo"] = repo_label(fields["cwd"])
    if os.environ.get("CLAUDE_PID"):
        try:
            fields["pid"] = int(os.environ["CLAUDE_PID"])
        except ValueError:
            pass

    captured = {}

    def mutate(data):
        sessions = data["sessions"]
        rec = sessions.get(session_id)
        if rec is None:
            rec = state.new_session_record(
                session_id, [s["name"] for s in sessions.values()])
            sessions[session_id] = rec
        rec.update({k: v for k, v in fields.items() if v is not None})
        rec["last_seen"] = now
        rec["action_needed"] = action
        rec["reports"] = rec.get("reports", 0) + (1 if self_reported else 0)
        if action:
            # The clock starts at the first wait and keeps running across
            # repeated reports, so "blocked 2h" means 2h since it first stalled.
            if not rec.get("blocked_since"):
                rec["blocked_since"] = now
        else:
            rec["blocked_since"] = None
        if window_id and window_id in data["windows"]:
            rec["color"] = data["windows"][window_id]["color"]
        captured.update(rec)
        state.bump_content(data)
        return True

    state.update(mutate)
    ranker.request_rank()
    ranker.spawn_worker()

    if warnings and not quiet:
        for warn in warnings:
            sys.stderr.write("agentdash: %s\n" % warn)
    return {"session": captured, "warnings": warnings,
            "window_id": window_id, "color": colour}


def repo_label(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    parts = [p for p in path.split(os.sep) if p]
    if len(parts) <= 2:
        return path
    return os.sep.join(["..."] + parts[-2:])
