"""Render the dashboard against a synthetic roster, straight to stdout.

Not a unit test: this is the fastest way to eyeball layout, colour and the
hover expansion without opening eight terminal windows.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AGENTDASH_HOME", tempfile.mkdtemp(prefix="agentdash-demo-"))

from agentdash import palette, state, tui  # noqa: E402

NOW = time.time()

FIXTURE = [
    dict(name="amber-heron", tag="debugging", status="question", action=True,
         blocked=NOW - 8100, repo="~/dev/payments-api", priority=1,
         reason="prod-facing, blocked longest",
         summary="Traced the intermittent 502s to a connection-pool leak in db/pool.go. "
                 "The fix is written and the integration tests pass, but it changes retry "
                 "semantics for every caller. I need you to confirm that trade-off before I land it.",
         context="This has been shipping to production for three days and affects roughly 2% "
                 "of checkout requests. I cannot proceed without a decision because the "
                 "alternative fix would take another two hours. The user deferred a similar "
                 "question yesterday. Confidence in the diagnosis is high."),
    dict(name="wry-comet", tag="tests", status="done", action=True,
         blocked=NOW - 240, repo="~/dev/agent-dashboard", priority=2,
         reason="finished, two failures left",
         summary="Added twelve test cases covering the ranking payload builder and the "
                 "debounce worker. Ten pass; two fail because the fixture clock is frozen. "
                 "Everything is committed on the branch and ready for you to look at.",
         context="Nothing is blocked on the user here, but the two failures are trivial and "
                 "I could fix them unprompted. The branch is not merged so there is no risk "
                 "to anyone else. This can comfortably wait an hour. Low urgency."),
    dict(name="solar-stoat", tag="infra", status="blocked", action=True,
         blocked=NOW - 420, repo="~/ops/terraform", priority=3,
         reason="needs a credential you hold",
         summary="The staging apply is halfway through and stopped at the RDS module. "
                 "Terraform needs an AWS profile that is not on this machine. Nothing else "
                 "in the plan can run until that is sorted.",
         context="A half-applied Terraform state is genuinely risky if left overnight. "
                 "There is no workaround available to me. Every minute here increases the "
                 "chance of drift. This should outrank anything cosmetic."),
    dict(name="bold-finch", tag="docs", status="working", action=False,
         blocked=None, repo="~/dev/agent-dashboard", priority=4,
         reason="working", summary="Rewriting the README installation section.",
         context="Nothing needed. Purely cosmetic work. No deadline. Ignore."),
    dict(name="mossy-vole", tag="web search", status="working", action=False,
         blocked=None, repo="~/research", priority=5, reason="working",
         summary="Comparing three approaches to terminal mouse reporting.",
         context="Exploratory. No decisions pending. Nothing at stake. Ignore."),
    dict(name="iron-kite", tag="exploration", status="done", action=True,
         blocked=NOW - 90, repo="~/dev/scratch", priority=6,
         reason="finished, low stakes",
         summary="Read through the iTerm2 Python API surface and wrote up what is possible. "
                 "No code changed. The notes are in notes/iterm2.md.",
         context="Purely informational. Nothing waiting. The user asked out of curiosity. "
                 "Bottom of the list is correct."),
]


def seed():
    colours = palette.palette_sequence(len(FIXTURE))
    def mutate(data):
        for i, spec in enumerate(FIXTURE):
            wid = "w%d" % i
            data["windows"][wid] = {"color": colours[i], "first_seen": NOW,
                                    "iterm_sessions": ["u%d" % i], "painted": True}
            data["sessions"]["s%d" % i] = {
                "id": "s%d" % i, "name": spec["name"], "window_id": wid,
                "color": colours[i], "cwd": spec["repo"], "repo": spec["repo"],
                "tag": spec["tag"], "summary": spec["summary"],
                "ranker_context": spec["context"], "status": spec["status"],
                "action_needed": spec["action"], "blocked_since": spec["blocked"],
                "priority": spec["priority"], "rank_reason": spec["reason"],
                "started": NOW - 9000, "last_seen": NOW,
                "last_report": NOW - (0 if i in (0, 2) else 4000),
                "reports": 4, "self_reported": i != 5, "iterm_session": "u%d" % i,
            }
        data["meta"]["daemon_heartbeat"] = NOW
        data["meta"]["daemon_pid"] = 4242
        data["meta"]["last_look"] = NOW - 3600
        data["ranker"] = {"session_id": "abc12345-0000", "started": NOW - 5400,
                          "turns": 14, "ranks": 14, "last_ok": NOW - 20,
                          "last_error": None, "last_rank_rev": 99, "retired": 1,
                          "cost_usd": 0.5231}
        data["rev"] = 99
        return True
    state.update(mutate)


def main():
    seed()
    cols = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    dash = tui.Dashboard()
    dash.cols, dash.rows = cols, 200
    if "--hover" in sys.argv:
        dash.hover_id = "s0"
    if "--all" in sys.argv:
        dash.show_all = True
    if "--stale" in sys.argv:
        def mutate(data):
            data["ranker"]["turns"] = 52
            return True
        state.update(mutate)
    if "--empty" in sys.argv:
        def mutate(data):
            data["sessions"] = {}
            return True
        state.update(mutate)
    for line in dash.compose(state.read()):
        sys.stdout.write(line.text + "\033[0m\n")


if __name__ == "__main__":
    main()
