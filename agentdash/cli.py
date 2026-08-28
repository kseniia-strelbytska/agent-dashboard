"""Command line surface.

`agentdash report` is what Claude sessions call; everything else is for you or
for the installer.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from typing import List, Optional

from . import config, hooks, installer, ranker, report, state


def _p(text: str = "") -> None:
    sys.stdout.write(text + "\n")


# --- report -------------------------------------------------------------------

def cmd_report(args) -> int:
    session_id = args.session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        sys.stderr.write(
            "agentdash: no session id. Run this from inside a Claude Code session, "
            "or pass --session-id.\n")
        return 2
    if args.status not in report.STATUSES:
        sys.stderr.write("agentdash: --status must be one of %s\n"
                         % ", ".join(report.STATUSES))
        return 2
    result = report.submit(
        session_id=session_id,
        status=args.status,
        tag=args.tag,
        summary=args.summary,
        ranker_context=args.context,
        cwd=args.cwd,
    )
    rec = result["session"]
    if args.json:
        _p(json.dumps(result, indent=2, sort_keys=True))
    else:
        colour = rec.get("color") or "unassigned"
        _p("posted: %s [%s] status=%s colour=%s"
           % (rec.get("name"), rec.get("tag") or "-", rec.get("status"), colour))
    return 0


# --- window registration (called from the shell snippet) -----------------------

def cmd_window_register(args) -> int:
    uuid = args.iterm_session or report.iterm_session_uuid()
    if not uuid:
        if args.shell:
            _p("# agentdash: not an iTerm2 session")
        return 1
    window_id, colour = report.resolve_window(uuid)
    if not colour:
        if args.shell:
            _p("# agentdash: daemon did not answer; window is uncoloured")
        else:
            sys.stderr.write("agentdash: could not resolve a colour for this window\n")
        return 1
    if args.shell:
        _p('export AGENTDASH_COLOR=%s' % colour)
        _p('export AGENTDASH_WINDOW_ID=%s' % window_id)
        _p('export AGENTDASH_ITERM_SESSION=%s' % uuid)
    else:
        _p(json.dumps({"window_id": window_id, "color": colour,
                       "iterm_session": uuid}, indent=2))
    return 0


# --- dashboard ------------------------------------------------------------------

def cmd_dash(args) -> int:
    from . import tui
    return tui.run()


DASHBOARD_APPLESCRIPT = '''
tell application "iTerm2"
    activate
    create window with default profile command "%s"
end tell
'''


def cmd_open(args) -> int:
    """Open the dashboard window, or focus it if it is already up."""
    data = state.read()
    existing = data["meta"].get("dashboard_iterm_sessions") or []
    if existing and not args.force:
        from . import iterm_focus
        for uuid in existing:
            if iterm_focus.focus(uuid):
                _p("dashboard is already open; brought it to the front")
                return 0
    binary = shutil.which("agentdash") or os.path.abspath(sys.argv[0])
    script = DASHBOARD_APPLESCRIPT % ("%s dash" % binary)
    try:
        proc = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write("agentdash: could not talk to iTerm2: %s\n" % exc)
        return 1
    if proc.returncode != 0:
        sys.stderr.write("agentdash: iTerm2 refused to open a window: %s\n"
                         % (proc.stderr or "").strip())
        return 1
    _p("dashboard window opened")
    return 0


# --- hooks / ranking --------------------------------------------------------------

def cmd_hook(args) -> int:
    return hooks.run(args.event)


def cmd_rank_worker(args) -> int:
    return ranker.run_worker()


def cmd_rank(args) -> int:
    ok = ranker.rank_once()
    if not ok:
        data = state.read()
        sys.stderr.write("agentdash: rank failed: %s\n"
                         % data["ranker"].get("last_error"))
        return 1
    data = state.read()
    for rec in sorted(data["sessions"].values(), key=lambda r: r.get("priority") or 999):
        _p("%2s  %-18s %-14s %s" % (rec.get("priority") or "-", rec["name"],
                                    rec.get("tag") or "-", rec.get("rank_reason") or ""))
    return 0


# --- inspection -------------------------------------------------------------------

def _fmt_age(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    delta = int(time.time() - ts)
    if delta < 60:
        return "%ds ago" % delta
    if delta < 3600:
        return "%dm ago" % (delta // 60)
    return "%dh ago" % (delta // 3600)


def cmd_status(args) -> int:
    data = state.read()
    if args.json:
        _p(json.dumps(data, indent=2, sort_keys=True))
        return 0
    meta = data["meta"]
    beat = meta.get("daemon_heartbeat")
    alive = beat and time.time() - beat < 30
    _p("agentdash %s" % config.VERSION)
    _p("state      %s (rev %d)" % (config.STATE_FILE, data.get("rev", 0)))
    _p("daemon     %s%s" % ("running (pid %s)" % meta.get("daemon_pid") if alive else "NOT RUNNING",
                            "" if alive else "  - run `agentdash daemon-start`"))
    _p("windows    %d" % len(data["windows"]))
    for wid, win in sorted(data["windows"].items()):
        _p("           %-14s %s  %d session(s)" % (wid, win["color"], len(win.get("iterm_sessions", []))))
    rk = data["ranker"]
    _p("ranker     %s  turns=%s ranks=%s cost=$%.4f last=%s"
       % (rk.get("session_id") or "(not started)", rk.get("turns", 0),
          rk.get("ranks", 0), float(rk.get("cost_usd") or 0), _fmt_age(rk.get("last_ok"))))
    warn = ranker.staleness(rk, data)
    if warn:
        _p("           warning: %s" % warn)
    _p("sessions   %d" % len(data["sessions"]))
    for rec in sorted(data["sessions"].values(), key=lambda r: r.get("priority") or 999):
        waited = ("waited %s" % _fmt_age(rec["blocked_since"]).replace(" ago", "")
                  if rec.get("blocked_since") else "running")
        _p("  %2s %-18s %-8s %-12s %-10s %s"
           % (rec.get("priority") or "-", rec["name"], rec.get("color") or "-",
              rec.get("tag") or "-", waited, rec.get("repo") or ""))
    return 0


def cmd_doctor(args) -> int:
    return installer.doctor()


def cmd_daemon_start(args) -> int:
    return installer.start_daemon(foreground=args.foreground)


def cmd_install(args) -> int:
    return installer.install(disable_conflicting=args.disable_conflicting)


def cmd_uninstall(args) -> int:
    return installer.uninstall(purge=args.purge)


def cmd_reset(args) -> int:
    def mutate(data):
        data["sessions"] = {}
        data["ranker"] = {"session_id": None, "started": None, "turns": 0,
                          "last_ok": None, "last_error": None, "last_rank_rev": 0,
                          "retired": 0}
        return True
    state.update(mutate)
    _p("cleared sessions and retired the ranking session")
    return 0


def cmd_version(args) -> int:
    _p("agentdash %s" % config.VERSION)
    return 0


# --- parser -------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentdash",
        description="Concurrent Claude Code session monitor for iTerm2.")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    sub = parser.add_subparsers(dest="command")

    rep = sub.add_parser("report", help="post an update from inside a Claude session")
    rep.add_argument("--status", required=True,
                     help="one of: %s" % ", ".join(report.STATUSES))
    rep.add_argument("--tag", help="type of work last done, e.g. tests, docs, debugging")
    rep.add_argument("--summary", help="exactly 3 sentences, shown to the user")
    rep.add_argument("--context", help="exactly 4 sentences, seen only by the ranking agent")
    rep.add_argument("--session-id", help="override the Claude session id")
    rep.add_argument("--cwd", help="override the reported working directory")
    rep.add_argument("--json", action="store_true", help="print the stored row as JSON")
    rep.set_defaults(func=cmd_report)

    win = sub.add_parser("window-register", help="assign this iTerm2 window its colour")
    win.add_argument("--iterm-session", help="iTerm2 session uuid (defaults to $ITERM_SESSION_ID)")
    win.add_argument("--shell", action="store_true", help="print shell export statements")
    win.set_defaults(func=cmd_window_register)

    dash = sub.add_parser("dash", help="run the dashboard in this terminal")
    dash.set_defaults(func=cmd_dash)

    opn = sub.add_parser("open", help="open (or focus) the dashboard iTerm2 window")
    opn.add_argument("--force", action="store_true", help="open a new window even if one exists")
    opn.set_defaults(func=cmd_open)

    hook = sub.add_parser("hook", help="Claude Code hook entrypoint")
    hook.add_argument("event", choices=sorted(hooks.DISPATCH))
    hook.set_defaults(func=cmd_hook)

    sub.add_parser("rank-worker", help="internal: debounced ranking worker").set_defaults(func=cmd_rank_worker)

    rank = sub.add_parser("rank", help="force a rerank now")
    rank.set_defaults(func=cmd_rank)

    stat = sub.add_parser("status", help="print the current state")
    stat.add_argument("--json", action="store_true")
    stat.set_defaults(func=cmd_status)

    sub.add_parser("doctor", help="check the installation").set_defaults(func=cmd_doctor)

    dae = sub.add_parser("daemon-start", help="start the iTerm2 window daemon")
    dae.add_argument("--foreground", action="store_true")
    dae.set_defaults(func=cmd_daemon_start)

    ins = sub.add_parser("install", help="wire up hooks, shell snippet and daemon")
    ins.add_argument("--disable-conflicting", action="store_true",
                     help="move other iTerm2 AutoLaunch scripts that recolour windows aside")
    ins.set_defaults(func=cmd_install)

    uni = sub.add_parser("uninstall", help="remove all wiring")
    uni.add_argument("--purge", action="store_true", help="also delete state and logs")
    uni.set_defaults(func=cmd_uninstall)

    sub.add_parser("reset", help="clear all sessions and the ranking session").set_defaults(func=cmd_reset)
    sub.add_parser("version", help="print the version").set_defaults(func=cmd_version)
    return parser


def main(argv: List[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False):
        return cmd_version(args)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    config.ensure_dirs()
    return args.func(args)
