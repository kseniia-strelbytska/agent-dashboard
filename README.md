# agentdash

A live monitor for concurrent Claude Code sessions, built for iTerm2 on macOS.

Every iTerm2 window gets its own colour. Every Claude session running in one of
those windows gets a row on the dashboard, painted as a filled rectangle in that
window's colour, so the dashboard reads as a stack of cards matching the windows
on your screen. Each carries a name describing what that session is actually
doing, a tag for the kind of work it last did, and three sentences on what
changed since you last looked. Only the two or three sessions that actually need you are shown
in full; the rest collapse to one line each.

The order is decided by a Claude ranking agent, which sees four extra sentences
per session that you never do — written by each session specifically to help it
judge urgency honestly.

```
 AGENT DASHBOARD                                          4 need action · 6 live · 12:23:46
 ranker: sonnet · 14 ranks · $0.52 · 1 refresh

 1  ● amber-heron NEW                                          waited 2h 15m  ▐ debugging ▌
     Traced the intermittent 502s to a connection-pool leak in db/pool.go. The fix is written
     and the integration tests pass, but it changes retry semantics for every caller. I need
     you to confirm that trade-off before I land it.
     ~/dev/payments-api · why: prod-facing, blocked longest

 2  ● wry-comet                                                        waited 4m  ▐ tests ▌
     Added twelve test cases covering the ranking payload builder and the debounce worker.
     Ten pass; two fail because the fixture clock is frozen. Everything is committed.
     ~/dev/agent-dashboard · why: finished, two failures left

 ─────────────────────────────── 3 more · 1 also waiting ───────────────────────────────────
 ●  bold-finch        docs           —   Rewriting the README installation section.
 ●  mossy-vole        web search     —   Comparing three approaches to mouse reporting.
```

Every session is numbered. Press its number to open the private context it
wrote for the ranking agent, and again to close it. Nothing opens by pointing at
it: mouse reporting is off by default, so text selection works normally.

The top strip is one number per session - tokens per minute - in that session's
colour and in the same order as the cards, so it is obvious at a glance which
agent is actually working.

## Install

```sh
git clone <this repo> agent-dashboard
cd agent-dashboard
./install.sh
```

Requirements: macOS, iTerm2, and python3 3.8 or newer. There are no pip
dependencies — everything is standard library. The `claude` CLI is optional; if
it is missing, ranking falls back to a deterministic ordering.

Then:

```sh
agentdash open      # opens the dashboard in its own iTerm2 window
```

Restart iTerm2 once after installing, so the window daemon and the shell snippet
both load.

## What it touches

| Path | What goes there |
| --- | --- |
| `~/.agent-dashboard/` | state, logs, the installed copy of the package |
| `~/.local/bin/agentdash` | the launcher |
| `~/Library/Application Support/iTerm2/Scripts/AutoLaunch/agentdash_daemon.py` | the window daemon (a file, never a directory — iTerm2 treats directories there as script packages) |
| `~/.claude/settings.json` | five hooks, tagged so uninstall finds them |
| `~/.claude/CLAUDE.md` | a delimited block telling sessions how to report |
| `~/.zshrc`, `~/.bashrc` | a delimited block that registers each new window |
| a bridged container | `~/.agentdash/` inside it, plus five hooks in its `settings.json` |

Everything outside `~/.agent-dashboard` is either a delimited managed block or a
tagged entry, and `agentdash uninstall` removes all of it. `settings.json` is
backed up to `settings.json.agentdash-backup` before it is edited.

```sh
agentdash uninstall            # remove the wiring, keep the state
agentdash uninstall --purge    # remove everything
```

## How it works

**Colours.** A daemon runs inside iTerm2's own Python runtime — the only place
with a live connection to the app. It watches windows open and close, hands each
new window a colour no currently-open window is using, and tints it. The palette
is [these eight](https://coolors.co/palette/664d00-6e2a0c-691312-5d0933-291938-042d3a-12403c-475200);
past eight windows it generates further shades of the same hues, staying dark
enough that light text remains readable. Closing a window returns its colour to
the pool. Tabs and split panes inherit their window's colour. Tinting is applied
per session, so your saved iTerm2 profile is never modified.

**Rows.** Five Claude Code hooks keep the timing honest without spending a
token: a row appears at `SessionStart`, the waiting clock starts at `Stop` or at
a permission `Notification`, it stops when you submit a prompt, and the row
disappears at `SessionEnd`. The waiting time turns red past one hour.

**Prose.** The sessions write their own summaries. An instruction block in
`~/.claude/CLAUDE.md` tells every session to call `agentdash report` before it
asks you anything and when it finishes work. If a session stops without
reporting, its row says so rather than inventing a summary for it.

**Retrofitting.** `CLAUDE.md` is only read when a session starts, so a session
that was already running, resumed from an older transcript, or started with its
own configuration never sees it — and shows up permanently as a `(no report)`
row. The `UserPromptSubmit` hook fixes that: a session that has ended a turn
without reporting is handed the full instructions as extra context on its very
next prompt. No restart, nothing for you to do. A session that is already
reporting properly is never given anything, so this costs nothing in the normal
case; one that has gone quiet for a few prompts gets a two-line nudge rather
than the whole block.

**Resource budget.** Each rank spawns a `claude` process, which is not small, so
the ranking agent runs under hard caps as well as the debounce: a floor between
consecutive model calls, a rolling hourly ceiling, and a bound on how long one
worker will loop. Past any of those the ordering falls back to the heuristic and
the header says so, rather than the tool quietly spending your machine. Hooks
skip spawning a worker when one is already running, so a busy machine does not
get a herd of short-lived processes. The window daemon backs its poll off while
nothing changes, and the dashboard re-parses state only when the file has
actually moved. Measured idle cost of the daemon: about 25 MB and 0.1% CPU.

**Token metering.** Every assistant record in a session's transcript carries a
`usage` block, and the hooks hand us the transcript path, so the tool meters a
session without the session doing anything. Only newly appended bytes are
parsed - a byte offset is kept per session - and a partially written line is
left for the next pass. The headline number is tokens per minute over a rolling
ten-minute window, **excluding cache reads**: a session re-reading a 200k
context every turn would otherwise look busy while producing nothing. An idle
session decays to zero instead of freezing at its last value. The ranking
agent's own token total is shown in the header, so switching it to `haiku` is
an informed decision rather than a guess.

**Standing down.** The tool reads the kernel's own memory-pressure level
(`kern.memorystatus_vm_pressure_level`) and free-memory percentage before every
model call, via `sysctlbyname` rather than a subprocess. If the machine is under
pressure, or free memory is below the configured floor, ranking pauses, the
order falls back to the heuristic, and the dashboard header says so. It fails
open: if the kernel cannot be read, work is never withheld on a guess.

**Ranking.** One long-lived Claude session is resumed on every update, so it
accumulates judgement over the day instead of seeing each snapshot cold. Updates
within five seconds are batched into a single rerank. It runs with
`--autocompact auto`; on top of that the session is retired and restarted once
it passes hard age and turn limits, and the dashboard header warns you as soon
as its context passes the softer marks — or when its ordering has fallen behind
the state on screen. Its running cost is shown in the header.

## Sessions inside containers

A Claude session running in a container reads that container's config, so it has
none of these hooks, and it cannot see the host binary or state directory. It is
invisible to the dashboard rather than merely silent, and the retrofit above
cannot help it because there are no hooks to run.

```sh
agentdash bridge install <container>   # writes into a *running* container
agentdash bridge list
agentdash bridge remove <container>
```

This writes a POSIX-shell reporter and the five hooks into the container - no
Python needed in there - and points them at a spool directory on a bind mount
the container already has. Records are appended as JSON files and the host
daemon drains them into the dashboard every few seconds. Nothing is restarted
and no network is involved.

The row is coloured correctly too: the host process running
`docker exec -w <dir> ... <container>` is matched to its tty, and the tty to the
iTerm2 window, so a containerised session appears in the colour of the window
you are actually watching it in.

The container's tool sandbox confines writes to the working directory, so the
reporter falls back from the shared root to `<cwd>/.agentdash-spool` - and that
`cwd` can be any depth under the mount, a git worktree for instance. The host
scans the mount roots, the working directory of every container session it
already knows about, and, on a slow cadence, a bounded walk of the shared tree.
Where a spool lands inside a repository it is added to `.git/info/exclude`, which
is local and never committed; your tracked `.gitignore` is not touched.

A session already running in the container picks the hooks up as soon as Claude
Code next reads its settings, which in practice it does.

## Reporting from a session

Installed sessions do this on their own. By hand:

```sh
agentdash report --status question \
  --name "pool-leak-502s" \
  --tag debugging \
  --summary "Three sentences the user sees." \
  --context "Four sentences only the ranking agent sees."
```

`--status` is one of `working`, `done`, `question`, `blocked`, in three tiers.
`working` needs nothing. `done` is quiet — it shows as finished and never starts
the red timer. `question` and `blocked` mean you are genuinely being waited on,
start the timer, and compete for the top of the dashboard. Finished work fills
whatever room is left above the fold rather than competing for it, so a session
that ended cleanly is never mistaken for one stuck on a missing credential. `--name` is two or three words naming the work
(`pool-leak-502s`, not `crisp-otter`); without one the row falls back to the
working directory, and before a session has reported at all it gets a memorable
generated name so the row is never nameless. Summaries longer than three sentences (or
contexts longer than four) are clipped, with a note on stderr.

## The cat

An orange cat lives in the gaps between the cards. It obeys three rules.

**It yields.** It has its own lane, above every card, so it structurally cannot
cover a summary, a tag or a waiting timer. It stops moving entirely while a
decision is expanded, and it ignores a session whose timer has already gone red
— the point is the warning before the line is crossed, not noise after.

**It costs nothing.** Eight frames a second at most, and only the two lines it
occupies are redrawn — the rest of the frame is not rebuilt. It freezes
completely when the machine is under memory pressure. Measured: 240 frames cost
19 ms of CPU, about 0.06% of wall clock. The header carries the cat's own cost
next to the ranker's, which is both a joke and the proof.

**It turns off without an argument.** `{"cat": false}`. No dialogue, no
confirmation, no guilt text.

Beyond that it carries three things that are true but graceless to say in words:

- it **settles above the number of a session shortly before that session's timer
  turns red** — it walks over and sits on that session's cell in the token
  strip, a soft early warning that costs nothing to ignore;
- its **pace tracks how many sessions are open**, ambling at two and trotting at
  six, so you read your own fragmentation in peripheral vision without a number
  appearing anywhere — and past five it also **looks worried**, a sweat drop
  behind its ear, which is the same signal in a register you can read at a
  glance rather than only in motion;
- it **gets sleepy** late at night, after a long day, or when work is being sent
  back to sessions repeatedly.

There is deliberately no fourth *signal*. The worried face is not one: it is the
session count again, which the pace already carries. And worry never runs at the
same time as sleepiness or petting — a sleeping cat that is also sweating says
two things at once and therefore neither.

**Showing it to someone.** The sleepiness signal is real, which makes it awkward
to demonstrate: you cannot summon a nine-hour day on request. `w` in the
dashboard, or `agentdash cat wake|sleep|auto`, forces it either way. While an
override is on the header reads `cat 0.16% (awake: forced)` for as long as it
lasts, and it expires by itself after thirty minutes — a silently overridden
signal would be worse than no signal at all.

**Petting it is the only interaction in this tool with no consequence.** Every
other key is a decision, an approval, a commitment. Hovering the cat sends
hearts and changes nothing — not the ordering, not a session, not a byte of
state. That contrast is the point, not the hearts.

Mouse motion reporting is on only because the cat is; the single thing motion
can do is pet it. `m` turns reporting off if you would rather select text with
the mouse, and turning the cat off turns reporting off with it.

## Configuration

Optional, at `~/.agent-dashboard/config.json`. Anything you leave out keeps its
default.

```json
{
  "top_n": 3,                    // rows kept expanded above the fold
  "blocked_red_seconds": 3600,   // when the waiting time turns red
  "rank_debounce_seconds": 5,    // updates batched into one rerank
  "ranker_model": "sonnet",      // or "haiku" for a cheaper, blunter ranker
  "ranking_enabled": true,       // false: use the built-in heuristic only
  "rank_min_interval_seconds": 20,  // floor between two model calls
  "rank_max_per_hour": 60,          // rolling ceiling; heuristic order beyond it
  "defer_under_memory_pressure": true,
  "min_available_percent": 12,      // pause ranking below this much free memory
  "cat": true                       // the cat
}
```

`+` and `-` in the dashboard write `top_n` back to this file, so the setting
sticks.

## Keys

| Key | Does |
| --- | --- |
| `1`-`9` | open or close that session's ranker-only context |
| hover the cat | hearts, and nothing else |
| `a` | show every session in full / return to the fold |
| `+` `-` | change how many rows stay expanded |
| `r` | force a rerank now (costs one model call) |
| `o` | bring the top session's iTerm2 window to the front |
| `m` | mouse reporting, off by default; on, a click jumps to that window |
| `w` | force the cat awake, then asleep, then back to the real signal |
| `?` | help |
| `q` | quit |

## Troubleshooting

```sh
agentdash doctor     # checks every moving part and says what to fix
agentdash status     # the current state as plain text
```

Common ones:

- **No colours.** iTerm2's Python API must be on: Settings → General → Magic →
  Enable Python API, then restart iTerm2. `agentdash doctor` reports this.
- **Colours fight each other.** Another iTerm2 AutoLaunch script may also be
  recolouring windows. The installer detects those and offers to move them
  aside, into `~/.agent-dashboard/disabled-iterm-scripts/`. `agentdash uninstall`
  puts them back.
- **iTerm2 says "Cannot Run Script ... is malformed" on launch.** Something in
  `Scripts/AutoLaunch/` is a directory rather than a `.py` file. Versions before
  1.0.1 stashed disabled scripts there; `agentdash install` migrates them out
  and `agentdash doctor` flags it.
- **`agentdash: command not found`.** The launcher goes in `~/.local/bin`. Add
  it to your `PATH`, or open a new shell — the installed snippet adds it.
- **Rows never appear.** Check `agentdash doctor` for the hook count, and that
  the sessions were started after installing.

## Tests

```sh
./tests/run_all.sh               # everything
python3 tests/test_layout.py     # width safety at 40-200 cols, hover stability
python3 tests/test_input.py      # SGR mouse decoding, focus reporting, keys
python3 tests/test_limits.py     # rank budget, worker herd control
python3 tests/test_pressure.py   # standing down under memory pressure
python3 tests/test_retrofit.py   # handing instructions to sessions that lack them
python3 tests/test_usage.py      # token metering from real transcript files
python3 tests/test_cat.py        # the cat's three rules, as rules
python3 tests/test_bridge.py     # containerised sessions, offline (no docker needed)
python3 tests/test_flow.py       # windows, colours, reports, hooks, reaping
python3 tests/render_demo.py 100 # render a synthetic roster; --hover --all --stale --empty
```

None of them need iTerm2, a Claude session, or the network.
