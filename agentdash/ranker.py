"""The ranking agent.

A single long-lived Claude session is resumed on every update, so it builds up
judgement about what has been urgent today rather than seeing each snapshot
cold. Auto-compaction (`--autocompact auto`) keeps that session inside its
context window; on top of that we retire and restart the session once it passes
hard age/turn limits, and the dashboard warns the user as soon as it passes the
softer staleness marks.

Updates are debounced: a burst of agents finishing at once produces one rerank,
not five.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from typing import Dict, List, Optional, Tuple

from . import config, pressure, state

SYSTEM_PROMPT = """You are the ranking engine of a live dashboard that watches \
several concurrent Claude Code sessions for one developer.

On every update you receive the current roster of sessions. Each carries:
  - name, work tag, status, and how long it has been waiting on the user
  - VISIBLE: three sentences the session wrote for the developer
  - PRIVATE: four further sentences written only for you, never shown to the
    developer, giving the context you need to judge urgency accurately

Order the sessions by how much they need the developer's attention RIGHT NOW.

Judge on: is a human decision actually blocking progress; how expensive is the
delay (a stalled deploy or a broken build beats a finished refactor awaiting
review); how long it has already waited; whether the session can make progress
without an answer. A session that merely finished cleanly and needs a glance
ranks below one that is genuinely stuck. Trust the PRIVATE context over the
tone of the VISIBLE summary.

You are resumed across updates, so use what you learned earlier: if a session
has been repeatedly deferred, or if the developer clearly cares about one thread
today, weigh that.

Reply with JSON and nothing else, in exactly this shape:
{"ranking":[{"name":"<session name>","priority":1,"reason":"<max 12 words>"}]}
Every session in the roster must appear exactly once. priority starts at 1."""


def _log(msg: str) -> None:
    config.ensure_dirs()
    try:
        with open(config.RANKER_LOG, "a") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


# --- fallback ordering --------------------------------------------------------

def heuristic_order(sessions: List[Dict]) -> List[Tuple[str, int, str]]:
    """Deterministic ordering used before the first rank and whenever the
    ranking agent is unavailable. Never leaves the dashboard unordered."""
    now = time.time()

    def key(rec):
        blocked_for = now - (rec.get("blocked_since") or now)
        return (
            0 if rec.get("action_needed") else 1,
            0 if rec.get("status") == "question" else 1,
            -blocked_for,
        )

    out = []
    for i, rec in enumerate(sorted(sessions, key=key), start=1):
        if rec.get("action_needed"):
            reason = "waiting on you"
        elif rec.get("status") == "done":
            reason = "finished, needs a glance"
        else:
            reason = "working"
        out.append((rec["name"], i, reason))
    return out


# --- payload ------------------------------------------------------------------

def _fmt_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds < 0:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)


def build_payload(data: Dict) -> Tuple[str, List[Dict]]:
    now = time.time()
    sessions = sorted(data["sessions"].values(), key=lambda r: r.get("started", 0))
    lines = ["ROSTER (%d sessions, %s):" % (len(sessions), time.strftime("%H:%M"))]
    for rec in sessions:
        waited = _fmt_duration(now - rec["blocked_since"]) if rec.get("blocked_since") else "not waiting"
        lines.append("")
        lines.append("- name: %s" % rec["name"])
        lines.append("  tag: %s" % (rec.get("tag") or "unknown"))
        lines.append("  status: %s" % (rec.get("status") or "working"))
        lines.append("  action_needed: %s" % ("yes" if rec.get("action_needed") else "no"))
        lines.append("  waiting_on_user_for: %s" % waited)
        lines.append("  working_dir: %s" % (rec.get("repo") or rec.get("cwd") or "unknown"))
        lines.append("  VISIBLE: %s" % (rec.get("summary") or "(no summary reported yet)"))
        lines.append("  PRIVATE: %s" % (rec.get("ranker_context") or "(none supplied)"))
    lines.append("")
    lines.append("Return the JSON ranking for exactly these %d names." % len(sessions))
    return "\n".join(lines), sessions


# --- claude invocation ---------------------------------------------------------

def _claude_bin() -> Optional[str]:
    return shutil.which("claude") or os.environ.get("AGENTDASH_CLAUDE_BIN")


def _extract_json(text: str) -> Optional[Dict]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            return None
    return None


def _invoke(prompt: str, ranker: Dict, model: str) -> Tuple[Optional[Dict], Dict]:
    binary = _claude_bin()
    if not binary:
        return None, {"error": "the `claude` CLI is not on PATH"}

    fresh = not ranker.get("session_id")
    # --allowedTools is variadic, so anything following it is swallowed as a
    # tool name. The roster therefore goes in on stdin rather than as a
    # positional argument, which is immune to flag ordering and to ARG_MAX.
    cmd = [binary, "-p",
           "--model", model,
           "--autocompact", "auto",
           "--output-format", "json",
           "--setting-sources", "",
           "--allowedTools", ""]
    if fresh:
        session_id = str(uuid.uuid4())
        cmd += ["--session-id", session_id, "--system-prompt", SYSTEM_PROMPT]
    else:
        session_id = ranker["session_id"]
        cmd += ["--resume", session_id]

    env = dict(os.environ)
    env["AGENTDASH_INTERNAL"] = "1"          # keeps our own hooks from firing
    env.pop("AGENTDASH_COLOR", None)

    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              env=env, cwd=str(config.HOME),
                              timeout=config.RANKER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, {"error": "ranking agent timed out after %ds" % config.RANKER_TIMEOUT_SECONDS,
                      "session_id": session_id if not fresh else None}
    except OSError as exc:
        return None, {"error": "could not run claude: %s" % exc}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = err[-1][:200] if err else "exit %d" % proc.returncode
        # A resume that fails (transcript pruned, session gone) must not wedge
        # the ranker forever: drop the session id so the next call starts fresh.
        return None, {"error": detail, "reset": not fresh}

    try:
        envelope = json.loads(proc.stdout)
    except ValueError:
        return None, {"error": "unparseable CLI output", "reset": not fresh}

    meta = {
        "session_id": envelope.get("session_id") or session_id,
        "cost": float(envelope.get("total_cost_usd") or 0.0),
        "fresh": fresh,
    }
    if envelope.get("is_error"):
        meta["error"] = str(envelope.get("result"))[:200]
        return None, meta
    parsed = _extract_json(envelope.get("result") or "")
    if not parsed or "ranking" not in parsed:
        meta["error"] = "ranking agent did not return the expected JSON"
        return None, meta
    return parsed, meta


# --- the rank cycle -------------------------------------------------------------

def _should_retire(ranker: Dict, now: float) -> bool:
    if not ranker.get("session_id"):
        return False
    if ranker.get("turns", 0) >= config.RANKER_MAX_TURNS:
        return True
    started = ranker.get("started")
    return bool(started and now - started > config.RANKER_MAX_AGE_SECONDS)


def staleness(ranker: Dict, data: Dict, now: Optional[float] = None) -> Optional[str]:
    """Human-readable warning when the ranker's context is getting old, or its
    ordering no longer reflects the state on screen. None when healthy."""
    now = now or time.time()
    if ranker.get("last_error"):
        return "ranker error: %s" % str(ranker["last_error"])[:60]
    if ranker.get("budget_note"):
        return str(ranker["budget_note"])
    if not ranker.get("session_id"):
        return None
    turns = ranker.get("turns", 0)
    age = now - (ranker.get("started") or now)
    if turns >= config.RANKER_STALE_TURNS or age >= config.RANKER_STALE_AGE_SECONDS:
        return "ranker context stale (%d turns, %s old) - refreshing soon" % (
            turns, _fmt_duration(age))
    pending = data["meta"].get("content_rev", 0) - ranker.get("last_rank_rev", 0)
    last_ok = ranker.get("last_ok")
    if pending > 0 and last_ok and now - last_ok > config.RANKER_RESULT_STALE_SECONDS:
        return "order is %s old, %d update%s behind" % (
            _fmt_duration(now - last_ok), pending, "" if pending == 1 else "s")
    return None


def _clip_reason(reason, limit: int = 76) -> str:
    """The agent is asked for 12 words; clip politely when it writes more."""
    text = " ".join(str(reason or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]).rstrip(",.;:") + "\u2026"


def apply_ranking(ranking: List[Dict]) -> None:
    by_name = {}
    for item in ranking:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        try:
            priority = int(item.get("priority") or 999)
        except (TypeError, ValueError):
            priority = 999
        by_name[name] = (priority, _clip_reason(item.get("reason")))

    def mutate(data):
        ranked, missing = [], []
        for rec in data["sessions"].values():
            (ranked if rec["name"] in by_name else missing).append(rec)
        for rec in ranked:
            rec["priority"], rec["rank_reason"] = by_name[rec["name"]]
        # Anything the agent omitted still needs a slot, or it would vanish
        # from the ordering entirely. Park it below everything it did rank.
        if missing:
            floor = max([r.get("priority") or 0 for r in ranked] or [0])
            by_id = {r["name"]: r for r in missing}
            for name, offset, reason in heuristic_order(missing):
                rec = by_id.get(name)
                if rec is not None:
                    rec["priority"] = floor + offset
                    rec["rank_reason"] = reason
        return True

    state.update(mutate)


def _id_for(data: Dict, name: str) -> Optional[str]:
    for sid, rec in data["sessions"].items():
        if rec["name"] == name:
            return sid
    return None


def apply_heuristic() -> None:
    def mutate(data):
        for name, prio, reason in heuristic_order(list(data["sessions"].values())):
            sid = _id_for(data, name)
            if sid:
                data["sessions"][sid]["priority"] = prio
                data["sessions"][sid]["rank_reason"] = reason
        return True
    state.update(mutate)


def budget_state(ranker: Dict, cfg: Dict, now: float) -> Tuple[bool, Optional[str]]:
    """(may_call_model, why_not). Keeps the worst case bounded and visible."""
    min_gap = float(cfg.get("rank_min_interval_seconds", config.RANK_MIN_INTERVAL_SECONDS))
    max_hour = int(cfg.get("rank_max_per_hour", config.RANK_MAX_PER_HOUR))
    last = ranker.get("last_call") or 0
    if now - last < min_gap:
        return False, "rate limited (%ds between ranks)" % int(min_gap)
    recent = [t for t in (ranker.get("recent_calls") or []) if now - t < 3600]
    if len(recent) >= max_hour:
        return False, "hourly rank budget spent (%d/h) - using heuristic order" % max_hour
    return True, None


def _record_call(now: float) -> None:
    def mutate(data):
        r = data["ranker"]
        r["last_call"] = now
        recent = [t for t in (r.get("recent_calls") or []) if now - t < 3600]
        recent.append(now)
        r["recent_calls"] = recent[-200:]
        return True
    state.update(mutate)


def rank_once() -> bool:
    cfg = config.load_config()
    data = state.read()
    if not data["sessions"]:
        return True
    if not cfg.get("ranking_enabled", True):
        apply_heuristic()
        return True

    deferred, why = pressure.should_defer(cfg)
    if deferred:
        _log("deferring rank: %s" % why)
        apply_heuristic()
        state.update(lambda d: d["ranker"].update(budget_note=why) or True)
        return True

    allowed, why = budget_state(data["ranker"], cfg, time.time())
    if not allowed:
        _log("skipping model call: %s" % why)
        apply_heuristic()
        state.update(lambda d: d["ranker"].update(budget_note=why) or True)
        return True

    now = time.time()
    ranker = dict(data["ranker"])
    if _should_retire(ranker, now):
        _log("retiring ranker session %s (%d turns)" % (ranker.get("session_id"), ranker.get("turns", 0)))
        ranker["session_id"] = None
        ranker["retired"] = ranker.get("retired", 0) + 1

    prompt, sessions = build_payload(data)
    rev_at_call = data["meta"].get("content_rev", 0)
    _record_call(now)
    parsed, meta = _invoke(prompt, ranker, cfg.get("ranker_model", config.RANKER_MODEL))

    if parsed is None:
        _log("rank failed: %s" % meta.get("error"))
        apply_heuristic()

        def mutate(d):
            r = d["ranker"]
            r["last_error"] = meta.get("error")
            if meta.get("reset"):
                r["session_id"] = None
                r["started"] = None
                r["turns"] = 0
            return True
        state.update(mutate)
        return False

    apply_ranking(parsed.get("ranking") or [])

    def mutate(d):
        r = d["ranker"]
        if meta.get("fresh"):
            r["session_id"] = meta["session_id"]
            r["started"] = now
            r["turns"] = 0
        r["turns"] = r.get("turns", 0) + 1
        r["last_ok"] = time.time()
        r["last_error"] = None
        r["last_rank_rev"] = rev_at_call
        r["cost_usd"] = round(float(r.get("cost_usd") or 0.0) + meta.get("cost", 0.0), 4)
        r["ranks"] = int(r.get("ranks") or 0) + 1
        r["budget_note"] = None
        return True

    state.update(mutate)
    _log("ranked %d sessions (turn %d, +$%.4f)" % (len(sessions), ranker.get("turns", 0) + 1, meta.get("cost", 0.0)))
    return True


def request_rank() -> None:
    """Mark that a rerank is wanted. Cheap; safe to call on every report."""
    config.ensure_dirs()
    try:
        with open(config.RANK_REQUEST, "w") as fh:
            fh.write(str(time.time()))
    except OSError:
        pass


def _requested_at() -> float:
    try:
        with open(config.RANK_REQUEST) as fh:
            return float(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def run_worker() -> int:
    """Debounce loop. Only one of these runs at a time, enforced by flock."""
    import fcntl
    config.ensure_dirs()
    fd = os.open(str(config.RANK_LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # another worker owns the debounce window; it will pick this up
    try:
        debounce = float(config.load_config().get("rank_debounce_seconds",
                                                  config.RANK_DEBOUNCE_SECONDS))
        rounds = 0
        while True:
            rounds += 1
            if rounds > config.RANK_MAX_CONSECUTIVE:
                # Hand off rather than loop forever: the next report spawns a
                # fresh worker, and this process stops holding resources.
                _log("worker retiring after %d rounds" % (rounds - 1))
                return 0
            requested = _requested_at()
            if not requested:
                return 0
            wait = (requested + debounce) - time.time()
            while wait > 0:
                time.sleep(min(wait, 0.25))
                requested = _requested_at()
                wait = (requested + debounce) - time.time()
            served = requested
            try:
                rank_once()
            except Exception as exc:
                _log("worker error: %s" % exc)
            if _requested_at() <= served:
                try:
                    os.unlink(config.RANK_REQUEST)
                except OSError:
                    pass
                return 0
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def worker_running() -> bool:
    """True when a debounce worker already holds the lock."""
    import fcntl
    try:
        fd = os.open(str(config.RANK_LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def spawn_worker() -> None:
    """Fire-and-forget the debounce worker; returns immediately.

    Hooks fire on every prompt, notification and stop across every session, so
    without this check a busy machine would spawn a herd of short-lived Python
    processes that all immediately lose the same lock.
    """
    if worker_running():
        return
    try:
        subprocess.Popen(
            [sys.executable, "-m", "agentdash", "rank-worker"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(config.HOME),
        )
    except OSError as exc:
        _log("could not spawn rank worker: %s" % exc)
