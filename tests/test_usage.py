"""Per-session token metering, read from real transcript files."""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("AGENTDASH_HOME", tempfile.mkdtemp(prefix="agentdash-usage-"))

from agentdash import state, usage  # noqa: E402

FAILURES = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILURES.append(label)


def turn(inp, cc, cr, out, ts=None):
    rec = {"type": "assistant", "message": {"usage": {
        "input_tokens": inp, "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr, "output_tokens": out}}}
    if ts:
        rec["timestamp"] = ts
    return json.dumps(rec) + "\n"


def main():
    tdir = tempfile.mkdtemp(prefix="agentdash-transcript-")
    path = os.path.join(tdir, "s.jsonl")

    print("reading a transcript incrementally")
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        fh.write(turn(10, 1000, 50000, 200))
    off, samples, out, recs = usage.read_new(path, 0)
    counted = sum(x[1] for x in samples)
    check(recs == 1, "only assistant records with usage are counted")
    check(counted == 1210, "cache reads are excluded (%d)" % counted)
    check(out == 200, "output tokens are tracked separately")

    off2, s2, _, _ = usage.read_new(path, off)
    counted2 = sum(x[1] for x in s2)
    check(off2 == off and counted2 == 0, "re-reading an unchanged file costs nothing")

    with open(path, "a") as fh:
        fh.write(turn(5, 0, 60000, 300))
    off3, s3, _, _ = usage.read_new(path, off2)
    counted3 = sum(x[1] for x in s3)
    check(counted3 == 305, "only the appended bytes are parsed (%d)" % counted3)

    print("a half-written line is not eaten")
    with open(path, "a") as fh:
        fh.write('{"type":"assistant","message":{"usage":{"output_toke')
    off4, s4, _, _ = usage.read_new(path, off3)
    counted4 = sum(x[1] for x in s4)
    check(off4 == off3 and counted4 == 0, "a partial line is left for the next pass")
    with open(path, "a") as fh:
        fh.write('ns": 7}}}\n')
    off5, s5, _, _ = usage.read_new(path, off4)
    counted5 = sum(x[1] for x in s5)
    check(counted5 == 7, "and is counted once it is complete")

    print("a rotated transcript does not break the meter")
    with open(path, "w") as fh:
        fh.write(turn(1, 2, 3, 4))
    _, s6, _, _ = usage.read_new(path, 10 ** 9)
    counted6 = sum(x[1] for x in s6)
    check(counted6 == 7, "a shorter file restarts from the beginning (%d)" % counted6)

    print("history does not read as an instant spike")
    hist = os.path.join(tdir, "hist.jsonl")
    with open(hist, "w") as fh:
        fh.write(turn(0, 0, 0, 900000, "2020-01-01T00:00:00.000Z"))   # ancient
        fh.write(turn(0, 0, 0, 1200, time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                                   time.gmtime(time.time() - 60))))
    state.touch_session("hist", transcript_path=hist)
    usage.meter_all()
    r = state.read()["sessions"]["hist"]
    check(r["tokens_total"] == 901200, "all history still counts toward the total")
    check(r["tokens_per_min"] < 2000,
          "but the rate only reflects the recent window (%s/min)" % r["tokens_per_min"])

    print("rates")
    now = time.time()
    check(usage.rate_per_minute([], now) == 0.0, "no samples means zero, not a crash")
    r = usage.rate_per_minute([[now - 120, 5000], [now - 60, 5000], [now, 5000]], now)
    check(6000 < r < 9000, "three 5k turns over two minutes reads as ~7.5k/min (%d)" % r)
    old = usage.rate_per_minute([[now - 4000, 100000]], now)
    check(old == 0.0, "samples outside the window are dropped (%s)" % old)

    print("metering a session end to end")
    state.touch_session("s1", transcript_path=path)
    usage.meter_all()
    rec = state.read()["sessions"]["s1"]
    check(rec["tokens_total"] == 7, "the row carries a running total")
    check(rec["tokens_per_min"] > 0, "and a rate")
    check(rec["transcript_offset"] > 0, "and remembers where it got to")

    print("an idle session decays to zero rather than looking busy forever")
    def age(d):
        d["sessions"]["s1"]["token_samples"] = [[time.time() - 4000, 50000]]
        return True
    state.update(age)
    usage.decay()
    check(state.read()["sessions"]["s1"]["tokens_per_min"] == 0.0,
          "an hour-old sample no longer counts as activity")

    print("a transcript can be found from the session id alone")
    check(usage.find_transcript("ct:acct-b:whatever") is None,
          "a containerised session has no host transcript to find")
    check(usage.find_transcript("definitely-not-a-real-session") is None,
          "an unknown id finds nothing rather than guessing")

    print("a session with no transcript is skipped, not invented")
    state.touch_session("s2")
    usage.meter_all()
    check(state.read()["sessions"]["s2"].get("tokens_per_min") is None,
          "no transcript means no number, rather than a made-up zero")

    print("compact formatting")
    check([usage.human(n) for n in (0, 940, 3400, 12000, 1250000)]
          == ["0", "940", "3.4k", "12k", "1.2M"], "numbers stay short enough for one row")

    print("")
    if FAILURES:
        print("%d FAILURES" % len(FAILURES))
        return 1
    print("all usage checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
