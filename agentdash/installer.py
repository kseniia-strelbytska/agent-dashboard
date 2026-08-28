"""Install, uninstall and diagnose.

The tool has to survive being cloned onto a machine that has never seen it, so
everything the installer touches is either (a) inside AGENTDASH_HOME, or (b) a
clearly delimited managed block that `uninstall` removes again byte for byte.
"""
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config, pressure, state

BEGIN = "# >>> agentdash >>>"
END = "# <<< agentdash <<<"
MD_BEGIN = "<!-- >>> agentdash >>> -->"
MD_END = "<!-- <<< agentdash <<< -->"

LIB_DIR = config.HOME / "lib"
BIN_DIR_CANDIDATES = (Path.home() / ".local" / "bin", Path("/usr/local/bin"))
ITERM_SUPPORT = Path.home() / "Library" / "Application Support" / "iTerm2"
AUTOLAUNCH_DIR = ITERM_SUPPORT / "Scripts" / "AutoLaunch"
AUTOLAUNCH_SCRIPT = AUTOLAUNCH_DIR / "agentdash_daemon.py"
# Deliberately NOT inside AutoLaunch: iTerm2 treats every subdirectory there as
# a "full environment" script package and pops up "Cannot Run Script - ... is
# malformed" on each launch for anything that is not one.
DISABLED_DIR = config.HOME / "disabled-iterm-scripts"
LEGACY_DISABLED_DIR = AUTOLAUNCH_DIR / "disabled-by-agentdash"
CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_SETTINGS = CLAUDE_DIR / "settings.json"
CLAUDE_MEMORY = CLAUDE_DIR / "CLAUDE.md"
SHELL_SNIPPET = config.HOME / "shell" / "agentdash.sh"

HOOK_EVENTS = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "user-prompt",
    "Notification": "notification",
    "Stop": "stop",
    "SessionEnd": "session-end",
}

OK, WARN, BAD = "ok", "warn", "bad"
_MARKS = {OK: "\033[32m✓\033[0m", WARN: "\033[33m!\033[0m", BAD: "\033[31m✗\033[0m"}


def _say(status: str, label: str, detail: str = "") -> None:
    sys.stdout.write("  %s %-26s %s\n" % (_MARKS.get(status, " "), label, detail))


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


# --- environment checks --------------------------------------------------------

def check_platform() -> List[Tuple[str, str, str]]:
    out = []
    if platform.system() != "Darwin":
        out.append((BAD, "platform", "agentdash is macOS + iTerm2 only (found %s)" % platform.system()))
        return out
    out.append((OK, "platform", "macOS %s" % platform.mac_ver()[0]))
    if Path("/Applications/iTerm.app").exists() or ITERM_SUPPORT.exists():
        out.append((OK, "iTerm2", "installed"))
    else:
        out.append((BAD, "iTerm2", "not found - install iTerm2 first (https://iterm2.com)"))
    if sys.version_info < (3, 8):
        out.append((BAD, "python3", "need 3.8+, found %s" % platform.python_version()))
    else:
        out.append((OK, "python3", platform.python_version()))
    return out


def api_server_enabled() -> Optional[bool]:
    try:
        proc = subprocess.run(["defaults", "read", "com.googlecode.iterm2", "EnableAPIServer"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return False
    return proc.stdout.strip() in ("1", "YES", "true")


def enable_api_server() -> bool:
    try:
        subprocess.run(["defaults", "write", "com.googlecode.iterm2",
                        "EnableAPIServer", "-bool", "true"],
                       capture_output=True, timeout=10, check=True)
        return True
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return False


def iterm_python() -> Optional[Path]:
    """iTerm2 ships its own runtime; it is the only one with the iterm2 package."""
    pattern = str(ITERM_SUPPORT / "iterm2env" / "versions" / "*" / "bin" / "python3")
    best = None
    for candidate in sorted(glob.glob(pattern)):
        path = Path(candidate)
        try:
            proc = subprocess.run([str(path), "-c", "import iterm2"],
                                  capture_output=True, timeout=25)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            best = path
    return best


def migrate_legacy_disabled() -> int:
    """Rescue scripts parked in the old location inside AutoLaunch.

    Version 1.0.0 stashed conflicting scripts in a subdirectory of AutoLaunch,
    which iTerm2 then tried to load as a script package. Move them out.
    """
    if not LEGACY_DISABLED_DIR.exists():
        return 0
    DISABLED_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path in sorted(LEGACY_DISABLED_DIR.iterdir()):
        target = DISABLED_DIR / path.name
        if target.exists():
            target.unlink()
        shutil.move(str(path), str(target))
        moved += 1
    try:
        LEGACY_DISABLED_DIR.rmdir()
    except OSError:
        pass
    return moved


def conflicting_autolaunch() -> List[Path]:
    """Other AutoLaunch scripts that also recolour sessions would fight ours."""
    out = []
    if not AUTOLAUNCH_DIR.exists():
        return out
    for path in sorted(AUTOLAUNCH_DIR.glob("*.py")):
        if path.resolve() == AUTOLAUNCH_SCRIPT.resolve():
            continue
        try:
            body = path.read_text(errors="replace")
        except OSError:
            continue
        if "set_background_color" in body or "LocalWriteOnlyProfile" in body:
            out.append(path)
    return out


# --- managed text blocks -----------------------------------------------------------

def _strip_block(text: str, begin: str, end: str) -> str:
    if begin not in text:
        return text
    head, _, rest = text.partition(begin)
    _, _, tail = rest.partition(end)
    return (head.rstrip("\n") + "\n" + tail.lstrip("\n")).strip("\n") + "\n"


def _upsert_block(path: Path, body: str, begin: str, end: str) -> str:
    existing = path.read_text() if path.exists() else ""
    stripped = _strip_block(existing, begin, end)
    block = "%s\n%s\n%s\n" % (begin, body.strip("\n"), end)
    joined = (stripped.rstrip("\n") + "\n\n" + block) if stripped.strip() else block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(joined)
    return "updated" if begin in existing else "added"


def _remove_block(path: Path, begin: str, end: str) -> bool:
    if not path.exists():
        return False
    existing = path.read_text()
    if begin not in existing:
        return False
    path.write_text(_strip_block(existing, begin, end))
    return True


# --- pieces ----------------------------------------------------------------------

def install_library(python_bin: str) -> Path:
    """Copy the package out of the clone so the tool survives the clone moving."""
    src = _repo_root() / "agentdash"
    dst = LIB_DIR / "agentdash"
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    if dst.resolve() == src.resolve():
        return dst
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # The container bridge needs the shell scripts too.
    csrc = _repo_root() / "container"
    if csrc.is_dir():
        cdst = LIB_DIR / "container"
        if cdst.exists():
            shutil.rmtree(cdst)
        shutil.copytree(csrc, cdst)
    return dst


def install_launcher(python_bin: str) -> Tuple[Path, bool]:
    target_dir = None
    for candidate in BIN_DIR_CANDIDATES:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".agentdash-write-probe"
            probe.write_text("x")
            probe.unlink()
            target_dir = candidate
            break
        except OSError:
            continue
    if target_dir is None:
        raise RuntimeError("no writable directory for the launcher; tried %s"
                           % ", ".join(str(c) for c in BIN_DIR_CANDIDATES))
    launcher = target_dir / "agentdash"
    launcher.write_text(
        "#!/bin/sh\n"
        "# agentdash launcher - generated by the installer, safe to delete.\n"
        'AGENTDASH_LIB="%s"\n'
        'if [ -n "$PYTHONPATH" ]; then PYTHONPATH="$AGENTDASH_LIB:$PYTHONPATH"; '
        'else PYTHONPATH="$AGENTDASH_LIB"; fi\n'
        "export PYTHONPATH\n"
        'exec "%s" -m agentdash "$@"\n' % (LIB_DIR, python_bin))
    launcher.chmod(0o755)
    on_path = str(target_dir) in os.environ.get("PATH", "").split(os.pathsep)
    return launcher, on_path


def install_autolaunch() -> Path:
    AUTOLAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    AUTOLAUNCH_SCRIPT.write_text(
        '#!/usr/bin/env python3\n'
        '"""agentdash iTerm2 window daemon.\n\n'
        'Generated by `agentdash install`. Deleting this file disables automatic\n'
        'window colouring; everything else keeps working.\n"""\n'
        'import sys\n\n'
        'sys.path.insert(0, %r)\n\n'
        'from agentdash.iterm_daemon import run\n\n'
        'run()\n' % str(LIB_DIR))
    AUTOLAUNCH_SCRIPT.chmod(0o755)
    return AUTOLAUNCH_SCRIPT


def install_shell_snippet(launcher: Path) -> List[Tuple[Path, str]]:
    SHELL_SNIPPET.parent.mkdir(parents=True, exist_ok=True)
    src = _repo_root() / "shell" / "agentdash.sh"
    if src.exists() and src.resolve() != SHELL_SNIPPET.resolve():
        shutil.copyfile(src, SHELL_SNIPPET)
    SHELL_SNIPPET.chmod(0o644)

    body = ('export AGENTDASH_BIN="%s"\n'
            '[ -f "%s" ] && . "%s"' % (launcher, SHELL_SNIPPET, SHELL_SNIPPET))
    touched = []
    for rc in (Path.home() / ".zshrc", Path.home() / ".bashrc"):
        if rc.name == ".bashrc" and not rc.exists():
            continue
        touched.append((rc, _upsert_block(rc, body, BEGIN, END)))
    return touched


def install_hooks(launcher: Path) -> str:
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    settings: Dict = {}
    if CLAUDE_SETTINGS.exists():
        backup = CLAUDE_SETTINGS.with_suffix(".json.agentdash-backup")
        shutil.copyfile(CLAUDE_SETTINGS, backup)
        try:
            settings = json.loads(CLAUDE_SETTINGS.read_text() or "{}")
        except ValueError:
            raise RuntimeError(
                "%s is not valid JSON. A copy is at %s; fix the original and rerun."
                % (CLAUDE_SETTINGS, backup))

    hooks = settings.setdefault("hooks", {})
    for event, name in HOOK_EVENTS.items():
        entries = hooks.setdefault(event, [])
        entries[:] = [e for e in entries if not _is_ours(e)]
        entries.append({
            "hooks": [{
                "type": "command",
                "command": '"%s" hook %s' % (launcher, name),
                "timeout": 10,
            }]
        })
    CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    return "wrote %d hook events" % len(HOOK_EVENTS)


# Matches our hook command whatever the launcher path is and whether or not it
# ended up quoted. Getting this wrong once already produced duplicate hooks, so
# it is deliberately loose about surrounding punctuation.
_OURS = re.compile(r'agentdash["\']?\s+hook\s+[a-z-]+')


def _is_ours(entry: Dict) -> bool:
    for hook in (entry or {}).get("hooks", []):
        if _OURS.search(str(hook.get("command", ""))):
            return True
    return False


def remove_hooks() -> bool:
    if not CLAUDE_SETTINGS.exists():
        return False
    try:
        settings = json.loads(CLAUDE_SETTINGS.read_text() or "{}")
    except ValueError:
        return False
    hooks = settings.get("hooks") or {}
    changed = False
    for event in list(hooks):
        before = len(hooks[event])
        hooks[event] = [e for e in hooks[event] if not _is_ours(e)]
        if len(hooks[event]) != before:
            changed = True
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    if changed:
        CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    return changed


def install_instructions(launcher: Path) -> str:
    src = _repo_root() / "claude" / "instructions.md"
    if not src.exists():
        raise RuntimeError("missing %s in the clone" % src)
    body = src.read_text().replace("{{AGENTDASH}}", str(launcher))
    # Also kept where the hooks can read it, so a session that never loaded
    # CLAUDE.md can still be handed the instructions mid-flight.
    config.ensure_dirs()
    (config.HOME / "instructions.md").write_text(body)
    return _upsert_block(CLAUDE_MEMORY, body, MD_BEGIN, MD_END)


# --- daemon ---------------------------------------------------------------------

def daemon_alive(within: float = 30.0) -> bool:
    """A recent heartbeat is not enough: the process may have died between
    beats, and then `daemon-start` would refuse to start a replacement."""
    meta = state.read()["meta"]
    beat = meta.get("daemon_heartbeat")
    if not beat or time.time() - beat >= within:
        return False
    pid = meta.get("daemon_pid")
    return state._pid_alive(int(pid)) if pid else False


def start_daemon(foreground: bool = False) -> int:
    python = iterm_python()
    if python is None:
        sys.stderr.write(
            "agentdash: could not find iTerm2's bundled Python runtime.\n"
            "  Open iTerm2 > Scripts > Manage > Install Python Runtime, then rerun.\n")
        return 1
    if api_server_enabled() is False:
        sys.stderr.write(
            "agentdash: iTerm2's Python API is disabled.\n"
            "  Enable it in iTerm2 > Settings > General > Magic > Enable Python API,\n"
            "  or run: defaults write com.googlecode.iterm2 EnableAPIServer -bool true\n"
            "  then restart iTerm2.\n")
        return 1
    if daemon_alive():
        sys.stdout.write("daemon already running (pid %s)\n"
                         % state.read()["meta"].get("daemon_pid"))
        return 0
    if not AUTOLAUNCH_SCRIPT.exists():
        install_autolaunch()
    cmd = [str(python), str(AUTOLAUNCH_SCRIPT)]
    if foreground:
        return subprocess.call(cmd)
    config.ensure_dirs()
    with open(config.DAEMON_LOG, "a") as log:
        subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    for _ in range(60):
        if daemon_alive(within=15):
            sys.stdout.write("daemon started\n")
            return 0
        time.sleep(0.25)
    sys.stderr.write(
        "agentdash: the daemon did not report in within 15s.\n"
        "  iTerm2 may be waiting for you to authorise the script - look for a\n"
        "  permission prompt in iTerm2. Log: %s\n" % config.DAEMON_LOG)
    return 1


def stop_daemon() -> bool:
    pid = state.read()["meta"].get("daemon_pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), 15)
        return True
    except (OSError, ValueError):
        return False


# --- top level -----------------------------------------------------------------

def install(disable_conflicting: Optional[bool] = None) -> int:
    checks = check_platform()
    for status, label, detail in checks:
        _say(status, label, detail)
    if any(c[0] == BAD for c in checks):
        sys.stderr.write("\nagentdash: cannot install on this machine.\n")
        return 1

    config.ensure_dirs()
    python_bin = sys.executable or shutil.which("python3") or "/usr/bin/python3"

    install_library(python_bin)
    _say(OK, "library", str(LIB_DIR / "agentdash"))

    launcher, on_path = install_launcher(python_bin)
    _say(OK if on_path else WARN, "launcher",
         str(launcher) + ("" if on_path else "  (not on PATH - add %s)" % launcher.parent))

    install_autolaunch()
    _say(OK, "iTerm2 daemon script", str(AUTOLAUNCH_SCRIPT))

    rescued = migrate_legacy_disabled()
    if rescued:
        _say(OK, "legacy stash", "moved %d script(s) out of AutoLaunch into %s"
             % (rescued, DISABLED_DIR))

    conflicts = conflicting_autolaunch()
    if conflicts:
        if disable_conflicting:
            DISABLED_DIR.mkdir(parents=True, exist_ok=True)
            for path in conflicts:
                shutil.move(str(path), str(DISABLED_DIR / path.name))
            _say(OK, "conflicting scripts", "moved %d into %s" % (len(conflicts), DISABLED_DIR))
        else:
            _say(WARN, "conflicting scripts",
                 "%s also recolour windows and will fight agentdash"
                 % ", ".join(p.name for p in conflicts))

    for rc, action in install_shell_snippet(launcher):
        _say(OK, "shell snippet", "%s (%s)" % (rc, action))

    try:
        _say(OK, "claude hooks", install_hooks(launcher))
    except RuntimeError as exc:
        _say(BAD, "claude hooks", str(exc))
        return 1

    try:
        _say(OK, "claude instructions", "%s (%s)" % (CLAUDE_MEMORY, install_instructions(launcher)))
    except RuntimeError as exc:
        _say(BAD, "claude instructions", str(exc))
        return 1

    if api_server_enabled() is False:
        enable_api_server()
        _say(WARN, "iTerm2 Python API", "just enabled - restart iTerm2 for it to take effect")
    else:
        _say(OK, "iTerm2 Python API", "enabled")

    if not shutil.which("claude"):
        _say(WARN, "claude CLI", "not on PATH - ranking will fall back to a heuristic")
    else:
        _say(OK, "claude CLI", shutil.which("claude"))

    start_daemon()
    _say(OK if daemon_alive() else WARN, "daemon",
         "running" if daemon_alive() else "not yet running - see `agentdash doctor`")

    sys.stdout.write("""
Installed. Next:
  1. Restart iTerm2 (or open a new window) so the daemon and shell snippet load.
  2. Run:  agentdash open      - opens the dashboard in its own iTerm2 window.
  3. Start Claude sessions in other windows. They report themselves.

Uninstall with:  agentdash uninstall   (add --purge to delete state and logs)
""")
    return 0


def uninstall(purge: bool = False) -> int:
    stopped = stop_daemon()
    _say(OK if stopped else WARN, "daemon", "stopped" if stopped else "was not running")

    if AUTOLAUNCH_SCRIPT.exists():
        AUTOLAUNCH_SCRIPT.unlink()
        _say(OK, "iTerm2 daemon script", "removed")
    migrate_legacy_disabled()
    if DISABLED_DIR.exists():
        restored = list(DISABLED_DIR.glob("*.py"))
        AUTOLAUNCH_DIR.mkdir(parents=True, exist_ok=True)
        for path in restored:
            shutil.move(str(path), str(AUTOLAUNCH_DIR / path.name))
        if restored:
            _say(OK, "conflicting scripts", "restored %d to AutoLaunch" % len(restored))
        try:
            DISABLED_DIR.rmdir()
        except OSError:
            pass

    _say(OK if remove_hooks() else WARN, "claude hooks",
         "removed" if CLAUDE_SETTINGS.exists() else "settings.json not found")
    _say(OK if _remove_block(CLAUDE_MEMORY, MD_BEGIN, MD_END) else WARN,
         "claude instructions", "removed")
    for rc in (Path.home() / ".zshrc", Path.home() / ".bashrc"):
        if _remove_block(rc, BEGIN, END):
            _say(OK, "shell snippet", "removed from %s" % rc)

    for candidate in BIN_DIR_CANDIDATES:
        launcher = candidate / "agentdash"
        if launcher.exists():
            try:
                launcher.unlink()
                _say(OK, "launcher", "removed %s" % launcher)
            except OSError as exc:
                _say(WARN, "launcher", "could not remove %s: %s" % (launcher, exc))

    if purge:
        shutil.rmtree(config.HOME, ignore_errors=True)
        _say(OK, "state", "deleted %s" % config.HOME)
    else:
        _say(OK, "state", "kept at %s (use --purge to delete)" % config.HOME)
    sys.stdout.write("\nUninstalled. Restart iTerm2 to drop the window colours.\n")
    return 0


def doctor() -> int:
    sys.stdout.write("agentdash %s\n\n" % config.VERSION)
    worst = OK
    rows = list(check_platform())

    api = api_server_enabled()
    rows.append((OK if api else BAD, "iTerm2 Python API",
                 "enabled" if api else "disabled - enable it in Settings > General > Magic"))
    py = iterm_python()
    rows.append((OK if py else BAD, "iTerm2 python runtime",
                 str(py) if py else "missing - iTerm2 > Scripts > Manage > Install Python Runtime"))

    lib = LIB_DIR / "agentdash"
    rows.append((OK if lib.exists() else BAD, "library", str(lib) if lib.exists() else "not installed"))

    launcher = next((c / "agentdash" for c in BIN_DIR_CANDIDATES if (c / "agentdash").exists()), None)
    if launcher:
        on_path = shutil.which("agentdash") is not None
        rows.append((OK if on_path else WARN, "launcher",
                     str(launcher) + ("" if on_path else "  (not on PATH)")))
    else:
        rows.append((BAD, "launcher", "not installed"))

    rows.append((OK if AUTOLAUNCH_SCRIPT.exists() else BAD, "iTerm2 daemon script",
                 str(AUTOLAUNCH_SCRIPT) if AUTOLAUNCH_SCRIPT.exists() else "not installed"))
    conflicts = conflicting_autolaunch()
    if conflicts:
        rows.append((WARN, "conflicting scripts",
                     "%s also recolour windows" % ", ".join(p.name for p in conflicts)))
    if LEGACY_DISABLED_DIR.exists():
        rows.append((BAD, "legacy stash",
                     "%s makes iTerm2 show 'Cannot Run Script' on launch - "
                     "rerun `agentdash install` to move it out" % LEGACY_DISABLED_DIR))

    data = state.read()
    beat = data["meta"].get("daemon_heartbeat")
    if daemon_alive():
        rows.append((OK, "daemon", "running (pid %s, %d windows)"
                     % (data["meta"].get("daemon_pid"), len(data["windows"]))))
    else:
        rows.append((BAD, "daemon", "not running%s - `agentdash daemon-start`"
                     % ("" if not beat else ", last beat %ds ago" % int(time.time() - beat))))

    hooks_ok = False
    if CLAUDE_SETTINGS.exists():
        try:
            settings = json.loads(CLAUDE_SETTINGS.read_text() or "{}")
            found = sum(1 for event in HOOK_EVENTS
                        for e in (settings.get("hooks") or {}).get(event, []) if _is_ours(e))
            hooks_ok = found == len(HOOK_EVENTS)
            rows.append((OK if hooks_ok else WARN, "claude hooks",
                         "%d/%d installed" % (found, len(HOOK_EVENTS))))
        except ValueError:
            rows.append((BAD, "claude hooks", "settings.json is not valid JSON"))
    else:
        rows.append((BAD, "claude hooks", "~/.claude/settings.json missing"))

    md = CLAUDE_MEMORY.exists() and MD_BEGIN in CLAUDE_MEMORY.read_text()
    rows.append((OK if md else BAD, "claude instructions",
                 "present in %s" % CLAUDE_MEMORY if md else "missing"))

    rc_ok = any(rc.exists() and BEGIN in rc.read_text()
                for rc in (Path.home() / ".zshrc", Path.home() / ".bashrc"))
    rows.append((OK if rc_ok else WARN, "shell snippet",
                 "wired" if rc_ok else "not wired into any shell rc"))

    claude_bin = shutil.which("claude")
    rows.append((OK if claude_bin else WARN, "claude CLI",
                 claude_bin or "not on PATH - ranking falls back to a heuristic"))

    rk = data["ranker"]
    if rk.get("last_error"):
        rows.append((WARN, "ranker", "last error: %s" % str(rk["last_error"])[:70]))
    elif rk.get("session_id"):
        rows.append((OK, "ranker", "session %s, %s turns, $%.4f"
                     % (rk["session_id"][:8], rk.get("turns", 0), float(rk.get("cost_usd") or 0))))
    else:
        rows.append((OK, "ranker", "not started yet"))

    mem = pressure.read()
    deferred, why = pressure.should_defer(config.load_config(), mem)
    rows.append((WARN if deferred else OK, "machine memory",
                 why or pressure.describe(mem)))

    rows.append((OK, "sessions", "%d tracked, %d need action"
                 % (len(data["sessions"]),
                    sum(1 for s in data["sessions"].values() if s.get("action_needed")))))

    for status, label, detail in rows:
        _say(status, label, detail)
        if status == BAD:
            worst = BAD
        elif status == WARN and worst != BAD:
            worst = WARN
    sys.stdout.write("\n%s\n" % {OK: "All good.", WARN: "Usable, with warnings above.",
                                 BAD: "Not working - fix the ✗ lines above."}[worst])
    return 0 if worst != BAD else 1
