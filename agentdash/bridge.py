"""Bridging Claude sessions that run inside containers.

A containerised session reads the container's config, not yours, so it has none
of our hooks; and it cannot see the host's state directory or binary, so it has
nothing to report to. It is invisible to the dashboard rather than merely
silent, and the retrofit in the host hooks cannot help because there are no
hooks to run.

The bridge closes that gap without restarting anything: shell-only hooks and a
reporter are written into the running container, they append JSON records to a
spool directory on a mount the host already shares, and the host ingests them.
"""
import json
import os
import re
import shutil
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from . import config, names, state

BRIDGES_FILE = config.HOME / "bridges.json"
SPOOL_DIRNAME = ".agentdash-spool"
CONTAINER_HOME = "/home/claude/.agentdash"
DOCKER_TIMEOUT = 30

# `docker exec -w /work/thing ... container-name` - the workdir tells us which
# host terminal a given container session is being driven from.
_WORKDIR = re.compile(r"(?:-w|--workdir)[= ]+(\S+)")


def _docker() -> Optional[str]:
    return shutil.which("docker")


def _run(args: List[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=DOCKER_TIMEOUT, **kw)


# --- registry -----------------------------------------------------------------

def load() -> Dict:
    try:
        with open(BRIDGES_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save(data: Dict) -> None:
    config.ensure_dirs()
    tmp = str(BRIDGES_FILE) + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    os.replace(tmp, str(BRIDGES_FILE))


def mounts_of(container: str) -> List[Dict[str, str]]:
    """Bind mounts we can use as a shared spool location."""
    docker = _docker()
    if not docker:
        return []
    proc = _run([docker, "inspect", container, "--format",
                 "{{json .Mounts}}"])
    if proc.returncode != 0:
        return []
    try:
        raw = json.loads(proc.stdout or "[]")
    except ValueError:
        return []
    out = []
    for m in raw:
        src, dst = m.get("Source") or "", m.get("Destination") or ""
        # Docker Desktop reports host paths under /host_mnt for bind mounts.
        if src.startswith("/host_mnt/"):
            src = src[len("/host_mnt"):]
        if src and dst and os.path.isdir(src):
            out.append({"source": src, "destination": dst})
    return out


# --- installing into a running container ----------------------------------------

def _repo_file(name: str) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, "container", name),
                      os.path.join(os.path.dirname(here), "container", name)):
        if os.path.exists(candidate):
            with open(candidate) as fh:
                return fh.read()
    raise RuntimeError("missing container/%s in the installation" % name)


def _write_into(container: str, path: str, body: str, mode: str = "644") -> None:
    docker = _docker()
    parent = os.path.dirname(path)
    _run([docker, "exec", container, "sh", "-c", "mkdir -p '%s'" % parent])
    proc = subprocess.run(
        [docker, "exec", "-i", container, "sh", "-c", "cat > '%s'" % path],
        input=body, capture_output=True, text=True, timeout=DOCKER_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError("could not write %s into %s: %s"
                           % (path, container, (proc.stderr or "").strip()))
    _run([docker, "exec", container, "chmod", mode, path])


def _hook_entry(event: str) -> Dict:
    return {"hooks": [{"type": "command",
                       "command": "%s/agentdash-hook %s" % (CONTAINER_HOME, event),
                       "timeout": 10}]}


HOOK_EVENTS = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "user-prompt",
    "Notification": "notification",
    "Stop": "stop",
    "SessionEnd": "session-end",
}


def _is_ours(entry: Dict) -> bool:
    for hook in (entry or {}).get("hooks", []):
        if "agentdash-hook" in str(hook.get("command", "")):
            return True
    return False


def install(container: str, instructions: str) -> Dict:
    """Write the reporter, hooks and instructions into a running container."""
    docker = _docker()
    if not docker:
        raise RuntimeError("docker is not on PATH")
    probe = _run([docker, "inspect", container, "--format", "{{.State.Running}}"])
    if probe.returncode != 0:
        raise RuntimeError("no such container: %s" % container)
    if probe.stdout.strip() != "true":
        raise RuntimeError("container %s is not running" % container)

    binds = mounts_of(container)
    if not binds:
        raise RuntimeError(
            "%s has no host bind mount, so there is nowhere to spool through"
            % container)

    _write_into(container, "%s/agentdash-report" % CONTAINER_HOME,
                _repo_file("agentdash-report.sh"), "755")
    _write_into(container, "%s/agentdash-hook" % CONTAINER_HOME,
                _repo_file("agentdash-hook.sh"), "755")
    # Pre-escaped so the shell hook only has to cat it into a JSON string.
    body = instructions.replace("{{AGENTDASH}}", "%s/agentdash-report" % CONTAINER_HOME)
    _write_into(container, "%s/instructions.jsonfrag" % CONTAINER_HOME,
                json.dumps(body)[1:-1])

    settings_path = "/home/claude/.claude/settings.json"
    proc = _run([docker, "exec", container, "sh", "-c", "cat %s 2>/dev/null" % settings_path])
    try:
        settings = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except ValueError:
        raise RuntimeError("%s inside %s is not valid JSON" % (settings_path, container))
    hooks = settings.setdefault("hooks", {})
    for event, name in HOOK_EVENTS.items():
        entries = hooks.setdefault(event, [])
        entries[:] = [e for e in entries if not _is_ours(e)]
        entries.append(_hook_entry(name))
    _write_into(container, settings_path, json.dumps(settings, indent=2) + "\n")

    record = {"mounts": binds, "installed": time.time()}
    data = load()
    data[container] = record
    save(data)
    return record


def remove(container: str) -> bool:
    docker = _docker()
    data = load()
    existed = container in data
    if docker:
        settings_path = "/home/claude/.claude/settings.json"
        proc = _run([docker, "exec", container, "sh", "-c", "cat %s 2>/dev/null" % settings_path])
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                settings = json.loads(proc.stdout)
                hooks = settings.get("hooks") or {}
                for event in list(hooks):
                    hooks[event] = [e for e in hooks[event] if not _is_ours(e)]
                    if not hooks[event]:
                        del hooks[event]
                if not hooks:
                    settings.pop("hooks", None)
                _write_into(container, settings_path, json.dumps(settings, indent=2) + "\n")
            except (ValueError, RuntimeError):
                pass
        _run([docker, "exec", container, "rm", "-rf", CONTAINER_HOME])
    data.pop(container, None)
    save(data)
    return existed


# --- ingestion --------------------------------------------------------------------

def spool_roots() -> List[Tuple[str, str, List[Dict[str, str]]]]:
    """(container, host spool dir, mounts) for every place records may land."""
    out = []
    for container, record in load().items():
        binds = record.get("mounts") or []
        for m in binds:
            src = m["source"]
            out.append((container, os.path.join(src, SPOOL_DIRNAME), binds))
            try:
                for entry in sorted(os.listdir(src)):
                    nested = os.path.join(src, entry, SPOOL_DIRNAME)
                    if os.path.isdir(nested):
                        out.append((container, nested, binds))
            except OSError:
                pass
    return out


def to_host_path(path: str, binds: List[Dict[str, str]]) -> str:
    """Translate a container path to its host equivalent."""
    best = None
    for m in binds:
        dst = m["destination"].rstrip("/")
        if path == dst or path.startswith(dst + "/"):
            if best is None or len(dst) > len(best["destination"]):
                best = m
    if not best:
        return path
    dst = best["destination"].rstrip("/")
    return os.path.join(best["source"], path[len(dst):].lstrip("/"))


def _docker_exec_windows(container: str) -> Dict[str, str]:
    """Map a container workdir to the host tty driving it.

    The host terminal running `docker exec -w <dir> ... <container>` is the
    window the user sees that session in, so its colour is the right one for
    the row.
    """
    try:
        proc = subprocess.run(["ps", "-Ao", "tty=,command="],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in proc.stdout.splitlines():
        if "docker" not in line or " exec" not in line or container not in line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        tty, command = parts
        if tty in ("??", "-"):
            continue
        match = _WORKDIR.search(command)
        if match:
            out[match.group(1).rstrip("/")] = "/dev/" + tty
    return out


def _window_for(container: str, container_cwd: str, data: Dict) -> Tuple[Optional[str], Optional[str]]:
    ttys = data.get("meta", {}).get("ttys") or {}
    if not ttys:
        return None, None
    by_workdir = _docker_exec_windows(container)
    tty = by_workdir.get((container_cwd or "").rstrip("/"))
    if tty is None:
        # A single docker exec into this container is unambiguous.
        if len(by_workdir) == 1:
            tty = next(iter(by_workdir.values()))
        else:
            return None, None
    wid = ttys.get(tty)
    if not wid:
        return None, None
    win = data.get("windows", {}).get(wid)
    return (wid, win["color"]) if win else (None, None)


ACTION_EVENTS = {"stop", "notification"}


def _read_records(path: str) -> List[Tuple[str, Dict]]:
    out = []
    try:
        entries = sorted(os.listdir(path))
    except OSError:
        return out
    for entry in entries:
        if not entry.endswith(".json"):
            continue
        full = os.path.join(path, entry)
        try:
            with open(full) as fh:
                out.append((full, json.load(fh)))
        except (OSError, ValueError):
            try:
                os.unlink(full)          # unreadable: do not retry forever
            except OSError:
                pass
    out.sort(key=lambda pair: pair[1].get("at") or 0)
    return out


def ingest() -> int:
    """Apply every spooled record and delete it. Returns how many were applied."""
    applied = 0
    for container, spool, binds in spool_roots():
        for path, rec in _read_records(spool):
            try:
                if _apply(container, rec, binds):
                    applied += 1
            except Exception:
                pass
            try:
                os.unlink(path)
            except OSError:
                pass
    return applied


def _apply(container: str, rec: Dict, binds: List[Dict[str, str]]) -> bool:
    sid = rec.get("session_id")
    if not sid:
        return False
    key = "ct:%s:%s" % (container, sid)
    kind = rec.get("kind")
    event = rec.get("event")
    if kind == "hook" and event == "session-end":
        state.remove_session(key)
        return True

    container_cwd = rec.get("cwd") or ""
    host_cwd = to_host_path(container_cwd, binds)
    now = time.time()

    def mutate(data):
        sessions = data["sessions"]
        others = [s["name"] for k, s in sessions.items() if k != key]
        r = sessions.get(key)
        if r is None:
            r = state.new_session_record(key, others)
            sessions[key] = r
        r["container"] = container
        r["container_cwd"] = container_cwd
        r["cwd"] = host_cwd
        r["repo"] = _repo_label(host_cwd)
        r["last_seen"] = now

        wid, colour = _window_for(container, container_cwd, data)
        if wid:
            r["window_id"] = wid
            r["color"] = colour

        if kind == "report":
            for field in ("status", "tag", "summary", "ranker_context"):
                if rec.get(field):
                    r[field] = rec[field]
            label = names.describe(rec.get("name") or "", host_cwd, rec.get("tag") or "")
            if label and label != r.get("name"):
                r["name"] = names.unique(label, others)
                r["name_generated"] = False
            r["self_reported"] = True
            r["reports"] = (r.get("reports") or 0) + 1
            r["last_report"] = now
            action = (rec.get("status") or "working") != "working"
        else:
            if r.get("name_generated", names.looks_generated(r.get("name", ""))):
                derived = names.describe("", host_cwd, "")
                if derived:
                    r["name"] = names.unique(derived, others)
                    r["name_generated"] = False
            action = event in ACTION_EVENTS
            if action:
                r["status"] = "question" if event == "notification" else "done"
                if not r.get("reports"):
                    r["self_reported"] = False
                    r.setdefault("summary", None)
            else:
                r["status"] = "working"

        r["action_needed"] = action
        if action:
            if not r.get("blocked_since"):
                r["blocked_since"] = now
        else:
            r["blocked_since"] = None
        state.bump_content(data)
        return True

    state.update(mutate)
    return True


def _repo_label(path: str) -> str:
    from .report import repo_label
    return repo_label(path) or path
