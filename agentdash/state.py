"""The single source of truth: one JSON document, guarded by an advisory lock.

Every writer goes through `update()`, which takes an exclusive flock on a
sidecar lock file, re-reads the document, applies the mutation, and writes it
back atomically via rename. Readers use `read()` and never block; a torn read
is impossible because writes are atomic renames.
"""
import errno
import fcntl
import json
import os
import time
from contextlib import contextmanager
from typing import Callable, Dict, List, Optional

from . import config, names, palette


def _blank() -> Dict:
    return {
        "version": config.STATE_VERSION,
        "rev": 0,
        "windows": {},
        "sessions": {},
        "ranker": {
            "session_id": None,
            "started": None,
            "turns": 0,
            "last_ok": None,
            "last_error": None,
            "last_rank_rev": 0,
            "retired": 0,
        },
        "meta": {
            "daemon_pid": None,
            "daemon_heartbeat": None,
            "updated": time.time(),
            "last_look": time.time(),
            # `rev` ticks on every write, heartbeats included. `content_rev`
            # ticks only when something a human would call an update happened,
            # which is what the ranker's staleness is measured against.
            "content_rev": 0,
        },
    }


def read() -> Dict:
    try:
        with open(config.STATE_FILE) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _blank()
    if data.get("version") != config.STATE_VERSION:
        # Forward/backward incompatible on-disk shape: start clean rather than
        # guess. Sessions re-register within a heartbeat anyway.
        return _blank()
    base = _blank()
    base.update(data)
    for key in ("ranker", "meta"):
        merged = _blank()[key]
        merged.update(data.get(key) or {})
        base[key] = merged
    return base


def bump_content(data: Dict) -> None:
    """Mark this mutation as a real update, not bookkeeping."""
    data["meta"]["content_rev"] = int(data["meta"].get("content_rev", 0)) + 1


@contextmanager
def _lock():
    config.ensure_dirs()
    fd = os.open(str(config.STATE_LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _write(data: Dict) -> None:
    config.ensure_dirs()
    data["rev"] = int(data.get("rev", 0)) + 1
    data["meta"]["updated"] = time.time()
    tmp = str(config.STATE_FILE) + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, str(config.STATE_FILE))


def update(mutate: Callable[[Dict], Optional[bool]]) -> Dict:
    """Apply `mutate` under lock. Return the written document.

    If `mutate` returns False the document is left untouched (no rev bump), so
    idempotent heartbeats do not wake every watcher.
    """
    with _lock():
        data = read()
        changed = mutate(data)
        if changed is False:
            return data
        _write(data)
        return data


# --- windows -----------------------------------------------------------------

def register_window(window_id: str, iterm_session: Optional[str] = None,
                    preferred: Optional[str] = None) -> str:
    """Assign (or return the existing) palette colour for an iTerm2 window."""
    result = {}

    def mutate(data):
        wins = data["windows"]
        entry = wins.get(window_id)
        if entry is None:
            in_use = [w.get("color") for wid, w in wins.items() if wid != window_id]
            colour = palette.allocate(in_use, preferred=preferred)
            entry = {
                "color": colour,
                "first_seen": time.time(),
                "iterm_sessions": [],
                "painted": False,
            }
            wins[window_id] = entry
        if iterm_session and iterm_session not in entry["iterm_sessions"]:
            entry["iterm_sessions"].append(iterm_session)
        elif iterm_session:
            result["color"] = entry["color"]
            return False
        result["color"] = entry["color"]
        return True

    update(mutate)
    return result.get("color") or ""


def release_window(window_id: str) -> None:
    def mutate(data):
        if window_id not in data["windows"]:
            return False
        del data["windows"][window_id]
        for sess in data["sessions"].values():
            if sess.get("window_id") == window_id:
                sess["window_id"] = None
        return True
    update(mutate)


def mark_painted(window_id: str, colour: str) -> None:
    def mutate(data):
        entry = data["windows"].get(window_id)
        if not entry or (entry.get("painted") and entry.get("color") == colour):
            return False
        entry["painted"] = True
        entry["color"] = colour
        return True
    update(mutate)


def sync_windows(live: Dict[str, List[str]]) -> Dict[str, str]:
    """Reconcile state against the windows iTerm2 currently has open.

    `live` maps window_id -> list of iterm session uuids. Windows that vanished
    are dropped (freeing their colour); new ones are allocated. Returns the full
    window_id -> colour map.
    """
    out: Dict[str, str] = {}

    def mutate(data):
        wins = data["windows"]
        changed = False
        for gone in [w for w in wins if w not in live]:
            del wins[gone]
            changed = True
        for wid, sessions in live.items():
            entry = wins.get(wid)
            if entry is None:
                in_use = [w.get("color") for w2, w in wins.items() if w2 != wid]
                entry = {
                    "color": palette.allocate(in_use),
                    "first_seen": time.time(),
                    "iterm_sessions": list(sessions),
                    "painted": False,
                }
                wins[wid] = entry
                changed = True
            elif sorted(entry.get("iterm_sessions", [])) != sorted(sessions):
                entry["iterm_sessions"] = list(sessions)
                changed = True
            out[wid] = entry["color"]
        for sess in data["sessions"].values():
            if sess.get("window_id") and sess["window_id"] not in live:
                sess["window_id"] = None
        return changed or None

    update(mutate)
    return out


def window_for_iterm_session(data: Dict, iterm_session: str) -> Optional[str]:
    for wid, entry in data.get("windows", {}).items():
        if iterm_session in entry.get("iterm_sessions", []):
            return wid
    return None


# --- claude sessions ----------------------------------------------------------

def new_session_record(session_id: str, taken_names) -> Dict:
    return {
        "id": session_id,
        "name": names.generate(session_id, taken_names),
        "window_id": None,
        "color": None,
        "cwd": None,
        "repo": None,
        "tag": None,
        "summary": None,
        "ranker_context": None,
        "status": "working",
        "action_needed": False,
        "blocked_since": None,
        "acknowledged_rev": 0,
        "priority": None,
        "rank_reason": None,
        "started": time.time(),
        "last_seen": time.time(),
        "last_report": None,
        "reports": 0,
    }


def touch_session(session_id: str, **fields) -> Dict:
    """Create-or-update a session row. Unknown keys are stored verbatim."""
    captured = {}

    def mutate(data):
        sessions = data["sessions"]
        rec = sessions.get(session_id)
        created = rec is None
        if created:
            rec = new_session_record(session_id, [s["name"] for s in sessions.values()])
            sessions[session_id] = rec
        before = dict(rec)
        rec.update({k: v for k, v in fields.items() if v is not None})
        rec["last_seen"] = time.time()
        # Resolve colour from the owning window every time; windows outlive
        # sessions and may have been recoloured after a daemon restart.
        wid = rec.get("window_id")
        if wid and wid in data["windows"]:
            rec["color"] = data["windows"][wid]["color"]
        captured.update(rec)
        if created:
            return True
        before.pop("last_seen", None)
        after = dict(rec)
        after.pop("last_seen", None)
        return before != after or None

    update(mutate)
    return captured


def remove_session(session_id: str) -> None:
    def mutate(data):
        if session_id not in data["sessions"]:
            return False
        del data["sessions"][session_id]
        bump_content(data)
        return True
    update(mutate)


def reap(now: Optional[float] = None) -> int:
    """Drop sessions whose process is gone or which went silent long ago."""
    now = now or time.time()
    removed = [0]

    def mutate(data):
        dead = []
        for sid, rec in data["sessions"].items():
            pid = rec.get("pid")
            if pid and not _pid_alive(int(pid)):
                dead.append(sid)
            elif now - rec.get("last_seen", now) > config.SESSION_REAP_SECONDS:
                dead.append(sid)
        for sid in dead:
            del data["sessions"][sid]
        removed[0] = len(dead)
        if dead:
            bump_content(data)
            return True
        return None

    update(mutate)
    return removed[0]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def mark_looked() -> None:
    """Record that the user has just seen the dashboard."""
    def mutate(data):
        data["meta"]["last_look"] = time.time()
        for rec in data["sessions"].values():
            rec["acknowledged_rev"] = data.get("rev", 0)
        return True
    update(mutate)
