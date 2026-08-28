"""The container bridge: spooled records become dashboard rows.

Runs entirely offline against a fake shared mount - no docker, no container.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HOME = tempfile.mkdtemp(prefix="agentdash-bridge-")
os.environ["AGENTDASH_HOME"] = HOME

from agentdash import bridge, state  # noqa: E402

FAILURES = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILURES.append(label)


def main():
    share = tempfile.mkdtemp(prefix="agentdash-share-")
    project = os.path.join(share, "math-open-problem-solving")
    os.makedirs(project)
    mounts = [{"source": share, "destination": "/work"}]
    bridge.save({"acct-b": {"mounts": mounts, "installed": time.time()}})

    print("path translation")
    check(bridge.to_host_path("/work/math-open-problem-solving", mounts) == project,
          "a container path maps back to its host path")
    check(bridge.to_host_path("/elsewhere", mounts) == "/elsewhere",
          "an unmapped path is left alone")

    print("the container scripts really run under /bin/sh")
    env = dict(os.environ, AGENTDASH_SPOOL=os.path.join(share, ".agentdash-spool"),
               AGENTDASH_CONTAINER_HOME=os.path.join(share, ".agentdash"),
               CLAUDE_CODE_SESSION_ID="c0ffee-1")
    r = subprocess.run(["/bin/sh", os.path.join(ROOT, "container", "agentdash-report.sh"),
                        "--status", "question", "--name", "b93 enumeration",
                        "--tag", "data", "--summary", "One. Two. Three.",
                        "--context", "A. B. C. D.",
                        "--cwd", "/work/math-open-problem-solving"],
                       env=env, cwd=project, capture_output=True, text=True)
    check(r.returncode == 0, "the reporter exits cleanly (%s)" % (r.stderr.strip() or "ok"))

    r = subprocess.run(["/bin/sh", os.path.join(ROOT, "container", "agentdash-hook.sh"), "stop"],
                       input=json.dumps({"session_id": "c0ffee-2",
                                         "cwd": "/work/math-open-problem-solving"}),
                       env=env, capture_output=True, text=True)
    check(r.returncode == 0 and r.stdout.strip() == "",
          "the stop hook spools silently and emits no context")

    print("ingestion")
    applied = bridge.ingest()
    check(applied == 2, "both records are applied (%d)" % applied)
    check(bridge.ingest() == 0, "and are not applied twice")
    left = os.listdir(env["AGENTDASH_SPOOL"])
    check(left == [], "the spool is drained (%s)" % (left or "empty"))

    rows = {r["name"]: r for r in state.read()["sessions"].values()}
    print("rows")
    check("b93-enumeration" in rows, "the self-reported name is used (%s)" % list(rows))
    row = rows["b93-enumeration"]
    check(row["summary"] == "One. Two. Three.", "the summary lands verbatim")
    check(row["ranker_context"] == "A. B. C. D.", "the private context lands too")
    check(row["container"] == "acct-b", "the row records which container it came from")
    check(row["cwd"] == project, "cwd is the host path, not the container path")
    check(row["action_needed"] and row["blocked_since"], "status=question starts the clock")

    # names.describe caps at 24 characters, so the directory name is trimmed
    other = rows.get("math-open-problem")
    check(other is not None, "a hook-only session still gets a row (%s)" % list(rows))
    check(other and other["self_reported"] is False, "and is honestly flagged as silent")
    check(other and other["action_needed"], "a stop event means it is waiting on you")

    print("session end removes the row")
    spool = env["AGENTDASH_SPOOL"]
    with open(os.path.join(spool, "9-end.json"), "w") as fh:
        json.dump({"v": 1, "kind": "hook", "event": "session-end",
                   "session_id": "c0ffee-1", "cwd": "/work/math-open-problem-solving",
                   "at": time.time()}, fh)
    bridge.ingest()
    check("b93-enumeration" not in {r["name"] for r in state.read()["sessions"].values()},
          "SessionEnd removes the container row")

    print("bad input cannot wedge the spool")
    with open(os.path.join(spool, "10-bad.json"), "w") as fh:
        fh.write("{not json")
    with open(os.path.join(spool, "11-nosid.json"), "w") as fh:
        json.dump({"v": 1, "kind": "report", "status": "done"}, fh)
    bridge.ingest()
    check(os.listdir(spool) == [], "malformed records are discarded, not retried forever")

    print("the project-level spool is found too")
    nested = os.path.join(project, ".agentdash-spool")
    os.makedirs(nested, exist_ok=True)
    with open(os.path.join(nested, "1.json"), "w") as fh:
        json.dump({"v": 1, "kind": "report", "session_id": "c0ffee-3",
                   "status": "working", "name": "fallback spool", "tag": "tests",
                   "cwd": "/work/math-open-problem-solving", "at": time.time()}, fh)
    check(bridge.ingest() == 1, "a record spooled inside the project is picked up")
    check("fallback-spool" in {r["name"] for r in state.read()["sessions"].values()},
          "and becomes a row")

    print("")
    if FAILURES:
        print("%d FAILURES" % len(FAILURES))
        return 1
    print("all bridge checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
