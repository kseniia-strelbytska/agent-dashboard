"""Claude Code hook entrypoints.

These keep a session's row alive and honest without spending a single token:
  SessionStart      -> the row appears the moment a session opens
  UserPromptSubmit  -> you just replied, so the blocked clock stops
  Notification      -> the agent is asking you something: it is waiting
  Stop              -> the agent finished its turn: it is waiting
  SessionEnd        -> the row disappears

The agent's own `agentdash report` calls supply the prose; the hooks supply the
timing, and catch the case where an agent finishes without reporting.
"""
import json
import os
import sys
import time
from typing import Dict, Optional

from . import config, names, ranker, report, state


NUDGE = (
    "Reminder from the agent dashboard: your row has gone quiet. Post an update "
    "with `agentdash report` - three sentences for the user in --summary, four "
    "for the ranking agent in --context, plus --name and --tag - and always "
    "report before you ask the user anything and when you finish."
)

# How many prompts to leave between re-injections, so a session that is already
# reporting properly never pays for this.
REINJECT_AFTER = 3
QUIET_AFTER = 6


def instructions_text() -> str:
    """The reporting instructions, as rendered by the installer."""
    try:
        return (config.HOME / "instructions.md").read_text().strip()
    except OSError:
        return ""


def _emit_context(event: str, text: str) -> None:
    """Hand extra context back to Claude Code for this turn.

    This is what makes the tool work on sessions that never loaded the
    instructions from CLAUDE.md - one resumed from an older transcript, or
    running somewhere with its own configuration. The next prompt they submit
    carries the instructions, with no restart and nothing for the user to do.
    """
    if not text:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        }
    }))


def _injection_needed(rec: Dict) -> Optional[str]:
    """'full', 'nudge' or None. Costs nothing for a well-behaved session."""
    prompts = rec.get("prompts") or 0
    injected = rec.get("injected_at_prompt")
    since_injection = prompts - injected if injected is not None else 10 ** 6

    # It has ended at least one turn without reporting: it does not know it
    # should. This is the case worth spending tokens on.
    if not rec.get("reports") and rec.get("self_reported") is False:
        return "full" if since_injection >= REINJECT_AFTER else None

    # It knows how, but has gone quiet.
    if rec.get("reports"):
        since_report = prompts - (rec.get("prompt_at_last_report") or 0)
        if since_report >= QUIET_AFTER and since_injection >= QUIET_AFTER:
            return "nudge"
    return None


def _internal() -> bool:
    """True inside the ranking agent's own session, which must not self-report."""
    return os.environ.get("AGENTDASH_INTERNAL") == "1"


def _payload() -> Dict:
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _session_id(payload: Dict) -> Optional[str]:
    return (payload.get("session_id")
            or os.environ.get("CLAUDE_CODE_SESSION_ID")
            or None)


def _log(event: str, msg: str) -> None:
    if not os.environ.get("AGENTDASH_DEBUG"):
        return
    config.ensure_dirs()
    try:
        with open(config.HOOK_LOG, "a") as fh:
            fh.write("%s %s %s\n" % (time.strftime("%H:%M:%S"), event, msg))
    except OSError:
        pass


def _base_fields(payload: Dict) -> Dict:
    cwd = payload.get("cwd") or os.getcwd()
    iterm_uuid = report.iterm_session_uuid()
    window_id, colour = report.resolve_window(iterm_uuid)
    fields = {
        "cwd": cwd,
        "repo": report.repo_label(cwd),
        # binding is applied separately, under the "one session, one window"
        # rule in state.apply_binding
        "_binding": (iterm_uuid, window_id, colour),
    }
    if os.environ.get("CLAUDE_PID"):
        try:
            fields["pid"] = int(os.environ["CLAUDE_PID"])
        except ValueError:
            pass
    return fields


def _set(session_id: str, fields: Dict, action: Optional[bool], status: Optional[str]) -> None:
    now = time.time()

    def mutate(data):
        sessions = data["sessions"]
        rec = sessions.get(session_id)
        others = [s["name"] for sid, s in sessions.items() if sid != session_id]
        if rec is None:
            rec = state.new_session_record(session_id, others)
            sessions[session_id] = rec
        if rec.get("name_generated", names.looks_generated(rec.get("name", ""))):
            # Before the session has reported, the directory is the best clue
            # available about what it is doing. Upgrade in place, so rows that
            # predate this ever having a name catch up on their next event.
            derived = names.describe("", fields.get("cwd") or rec.get("cwd") or "", "")
            if derived:
                rec["name"] = names.unique(derived, others)
                rec["name_generated"] = False
        before = json.dumps(rec, sort_keys=True)
        binding = fields.pop("_binding", (None, None, None))
        rec.update({k: v for k, v in fields.items() if v is not None})
        state.apply_binding(data, rec, *binding)
        rec["last_seen"] = now
        if status is not None:
            rec["status"] = status
        if action is not None:
            rec["action_needed"] = action
            if action:
                if not rec.get("blocked_since"):
                    rec["blocked_since"] = now
            else:
                rec["blocked_since"] = None
        rec_now = dict(rec)
        rec_now["last_seen"] = 0
        prev = json.loads(before)
        prev["last_seen"] = 0
        if prev == rec_now:
            return None
        state.bump_content(data)
        return True

    state.update(mutate)


def session_start() -> int:
    if _internal():
        return 0
    payload = _payload()
    sid = _session_id(payload)
    if not sid:
        return 0
    _log("session_start", sid)
    _set(sid, _base_fields(payload), action=False, status="working")
    return 0


def user_prompt_submit() -> int:
    """You just typed something, so this session is no longer waiting on you."""
    if _internal():
        return 0
    payload = _payload()
    sid = _session_id(payload)
    if not sid:
        return 0
    _log("user_prompt", sid)
    before = state.read()["sessions"].get(sid) or {}
    fields = _base_fields(payload)
    fields["last_prompt_at"] = time.time()
    fields["prompts"] = (before.get("prompts") or 0) + 1
    _set(sid, fields, action=False, status="working")

    rec = state.read()["sessions"].get(sid) or {}
    need = _injection_needed(rec)
    if need:
        _log("inject", "%s %s" % (sid, need))
        _emit_context("UserPromptSubmit",
                      instructions_text() if need == "full" else NUDGE)
        state.update(lambda d: d["sessions"].get(sid, {}).update(
            injected_at_prompt=rec.get("prompts") or 0) or True)

    ranker.request_rank()
    ranker.spawn_worker()
    return 0


def notification() -> int:
    """Claude Code is asking for input or permission: the clock starts."""
    if _internal():
        return 0
    payload = _payload()
    sid = _session_id(payload)
    if not sid:
        return 0
    fields = _base_fields(payload)
    message = (payload.get("message") or "").strip()
    if message:
        fields["notification"] = message[:200]
    _log("notification", "%s %s" % (sid, message[:60]))
    _set(sid, fields, action=True, status="question")
    ranker.request_rank()
    ranker.spawn_worker()
    return 0


def stop() -> int:
    """The agent finished its turn. If it never reported, say so plainly rather
    than inventing a summary - the row still needs to show as waiting."""
    if _internal():
        return 0
    payload = _payload()
    sid = _session_id(payload)
    if not sid:
        return 0
    fields = _base_fields(payload)
    data = state.read()
    rec = data["sessions"].get(sid) or {}
    # Fresh means: reported at some point after the prompt that started this turn.
    fresh = (rec.get("last_report") or 0) >= (rec.get("last_prompt_at") or 0)
    if not fresh:
        fields["self_reported"] = False
        if not rec.get("summary"):
            fields["summary"] = (
                "This session finished a turn without posting a summary. "
                "Open its window to see what it did. "
                "Its instructions ask it to report before stopping; it skipped that step.")
    _log("stop", sid)
    _set(sid, fields, action=True, status=rec.get("status") if rec.get("status") == "question" else "done")
    ranker.request_rank()
    ranker.spawn_worker()
    return 0


def session_end() -> int:
    if _internal():
        return 0
    payload = _payload()
    sid = _session_id(payload)
    if not sid:
        return 0
    _log("session_end", sid)
    state.remove_session(sid)
    ranker.request_rank()
    ranker.spawn_worker()
    return 0


DISPATCH = {
    "session-start": session_start,
    "user-prompt": user_prompt_submit,
    "notification": notification,
    "stop": stop,
    "session-end": session_end,
}


def run(event: str) -> int:
    handler = DISPATCH.get(event)
    if handler is None:
        sys.stderr.write("agentdash: unknown hook event %r\n" % event)
        return 2
    try:
        return handler()
    except Exception as exc:            # a broken hook must never block Claude
        _log("error", "%s: %s" % (event, exc))
        return 0
