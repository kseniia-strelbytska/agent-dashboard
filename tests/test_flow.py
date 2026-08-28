"""End-to-end plumbing without iTerm2 or the model: windows, reports, hooks."""
import io
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HOME = tempfile.mkdtemp(prefix="agentdash-flow-")
os.environ["AGENTDASH_HOME"] = HOME
os.environ["AGENTDASH_DEBUG"] = "1"

from agentdash import hooks, palette, ranker, report, state  # noqa: E402

FAILURES = []


def check(cond, label):
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s" % label)
        FAILURES.append(label)


def run_hook(event, payload, env=None):
    """Hooks read JSON on stdin; drive them the way Claude Code does."""
    old_stdin, old_env = sys.stdin, dict(os.environ)
    if env:
        os.environ.update(env)
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        return hooks.run(event)
    finally:
        sys.stdin = old_stdin
        os.environ.clear()
        os.environ.update(old_env)


def main():
    print("windows")
    c1 = state.register_window("win-A", "uuid-a1")
    c2 = state.register_window("win-B", "uuid-b1")
    c3 = state.register_window("win-A", "uuid-a2")   # second tab, same window
    check(c1 != c2, "two concurrent windows get different colours")
    check(c1 == c3, "a second tab inherits its window's colour")
    check(c1 in palette.BASE_PALETTE, "first colours come from the published palette")

    many = [state.register_window("win-%d" % i, "u%d" % i) for i in range(12)]
    check(len(set(many + [c1, c2])) == 14, "14 concurrent windows, 14 distinct colours")
    for i in range(12):
        state.release_window("win-%d" % i)
    reused = state.register_window("win-C", "uuid-c1")
    check(reused not in (c1, c2), "a reused colour still avoids live windows")

    print("reports")
    os.environ["ITERM_SESSION_ID"] = "w0t0p0:uuid-a1"
    res = report.submit("sess-1", "working", "implementation",
                        "One. Two. Three.", "A. B. C. D.", cwd="/tmp/repo")
    rec = res["session"]
    check(rec["color"] == c1, "a session inherits the colour of its window")
    check(rec["action_needed"] is False, "status=working needs no action")
    check(rec["blocked_since"] is None, "status=working runs no timer")
    check(bool(rec["name"]) and "-" in rec["name"], "session got a punchy two-part name")

    res = report.submit("sess-1", "question", "debugging",
                        "Q one. Q two. Q three. Q four. Q five.",
                        "P one. P two. P three. P four. P five. P six.")
    rec = res["session"]
    check(len(report.split_sentences(rec["summary"])) == 3, "summary clipped to 3 sentences")
    check(len(report.split_sentences(rec["ranker_context"])) == 4, "context clipped to 4 sentences")
    check(rec["action_needed"] is True, "status=question needs action")
    started = rec["blocked_since"]
    check(started is not None, "status=question starts the waiting clock")

    time.sleep(0.05)
    report.submit("sess-1", "blocked", "infra", "Still stuck. On it. Sorry.", "a. b. c. d.")
    check(state.read()["sessions"]["sess-1"]["blocked_since"] == started,
          "the clock keeps running across repeated reports")

    print("hooks")
    run_hook("user-prompt", {"session_id": "sess-1", "cwd": "/tmp/repo"})
    rec = state.read()["sessions"]["sess-1"]
    check(rec["action_needed"] is False and rec["blocked_since"] is None,
          "your reply stops the clock")

    run_hook("stop", {"session_id": "sess-1", "cwd": "/tmp/repo"})
    rec = state.read()["sessions"]["sess-1"]
    check(rec["action_needed"] is True, "Stop marks the session as waiting")
    check(rec["self_reported"] is False, "Stop without a report is flagged, not invented")

    run_hook("session-start", {"session_id": "sess-2", "cwd": "/tmp/other"})
    check("sess-2" in state.read()["sessions"], "SessionStart creates a row")
    run_hook("notification", {"session_id": "sess-2", "message": "Claude needs permission"})
    rec = state.read()["sessions"]["sess-2"]
    check(rec["action_needed"] and rec["status"] == "question",
          "a permission prompt counts as waiting on you")

    run_hook("session-end", {"session_id": "sess-2", "cwd": "/tmp/other"})
    check("sess-2" not in state.read()["sessions"], "SessionEnd removes the row")

    print("ranker isolation")
    run_hook("session-start", {"session_id": "ranker-sess", "cwd": "/tmp"},
             env={"AGENTDASH_INTERNAL": "1"})
    check("ranker-sess" not in state.read()["sessions"],
          "the ranking agent's own session never lands on the dashboard")

    print("fallback ordering")
    data = state.read()
    order = ranker.heuristic_order(list(data["sessions"].values()))
    check(order and order[0][0] == data["sessions"]["sess-1"]["name"],
          "without the model, the waiting session still sorts first")
    ranker.apply_heuristic()
    check(state.read()["sessions"]["sess-1"]["priority"] == 1,
          "heuristic priorities are written to state")

    print("payload for the ranking agent")
    prompt, sessions = ranker.build_payload(state.read())
    check("VISIBLE:" in prompt and "PRIVATE:" in prompt,
          "both the shown summary and the private context reach the ranker")
    check("Still stuck." in prompt, "the visible summary is included verbatim")

    print("reaping")
    state.touch_session("ghost", pid=999999)
    state.reap()
    check("ghost" not in state.read()["sessions"], "a dead process is reaped")

    print("")
    if FAILURES:
        print("%d FAILURES" % len(FAILURES))
        return 1
    print("all flow checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
