"""Retrofitting instructions into sessions that never loaded them.

A session resumed from an older transcript, or running with its own
configuration, never sees the CLAUDE.md block. The UserPromptSubmit hook hands
it the instructions mid-flight instead, with no restart. This must cost nothing
for a session that is already reporting properly.
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = tempfile.mkdtemp(prefix="agentdash-retrofit-")
os.environ["AGENTDASH_HOME"] = HOME
os.environ["ITERM_SESSION_ID"] = "w0t0p0:uuid-x"

from agentdash import config, hooks, report, state  # noqa: E402

config.ensure_dirs()
(config.HOME / "instructions.md").write_text(
    "## Reporting to the agent dashboard\n\nCall `agentdash report` with "
    "--name, --tag, --summary and --context.\n")

FAILURES = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILURES.append(label)


def prompt(sid, cwd="/tmp/repo"):
    """Drive UserPromptSubmit the way Claude Code does, capturing stdout."""
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps({"session_id": sid, "cwd": cwd}))
    sys.stdout = io.StringIO()
    try:
        hooks.run("user-prompt")
        return sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = old_in, old_out


def stop(sid, cwd="/tmp/repo"):
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(json.dumps({"session_id": sid, "cwd": cwd}))
    sys.stdout = io.StringIO()
    try:
        hooks.run("stop")
        return sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = old_in, old_out


def context_of(out):
    if not out.strip():
        return None
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def main():
    print("a well-behaved session pays nothing")
    check(context_of(prompt("good")) is None, "first prompt injects nothing")
    report.submit("good", "done", "tests", "A. B. C.", "a. B. C. D.", name="good-work")
    stop("good")
    check(context_of(prompt("good")) is None, "a session that reports is left alone")

    print("a session that never learned to report")
    prompt("silent")
    out = stop("silent")
    check(out.strip() == "", "the Stop hook itself emits no context")
    rec = state.read()["sessions"]["silent"]
    check(rec["self_reported"] is False, "the row is flagged as not self-reporting")
    ctx = context_of(prompt("silent"))
    check(ctx is not None and "agentdash report" in ctx,
          "its very next prompt carries the full instructions")

    print("and it is not spammed")
    check(context_of(prompt("silent")) is None, "the prompt right after injection is quiet")
    check(context_of(prompt("silent")) is None, "still quiet")
    check(context_of(prompt("silent")) is not None,
          "but it is retried after a few prompts if it still has not reported")

    print("once it complies, injection stops")
    report.submit("silent", "working", "docs", "A. B. C.", "a. B. C. D.", name="now-reporting")
    stop("silent")
    check(context_of(prompt("silent")) is None, "a now-compliant session is left alone")

    print("a session that goes quiet gets a short nudge, not the essay")
    seen = [context_of(prompt("silent")) for _ in range(hooks.QUIET_AFTER + 2)]
    nudges = [c for c in seen if c is not None]
    check(nudges == [hooks.NUDGE],
          "exactly one short nudge across %d quiet prompts, not one per prompt"
          % len(seen))
    check(len(hooks.NUDGE) < 400, "and the nudge really is short (%d chars)" % len(hooks.NUDGE))

    print("hook output is valid, parseable, and only the JSON")
    prompt("fresh2")
    stop("fresh2")
    raw = prompt("fresh2")
    parsed = json.loads(raw)
    check(parsed["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit",
          "the event name is what Claude Code expects")
    check(set(parsed) == {"hookSpecificOutput"}, "nothing else is written to stdout")

    print("the ranking agent is still never touched")
    os.environ["AGENTDASH_INTERNAL"] = "1"
    try:
        check(prompt("ranker-sess").strip() == "", "internal sessions get no injection")
    finally:
        del os.environ["AGENTDASH_INTERNAL"]

    print("")
    if FAILURES:
        print("%d FAILURES" % len(FAILURES))
        return 1
    print("all retrofit checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
