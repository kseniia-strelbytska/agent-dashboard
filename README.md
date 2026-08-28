# agentdash

A live monitor for concurrent Claude Code sessions, built for iTerm2 on macOS.

Every iTerm2 window gets its own colour. Every Claude session running in one of
those windows gets a row on a dashboard, in that colour, with a short name, a
tag for the kind of work it last did, and three sentences on what changed since
you last looked. Only the two or three sessions that actually need you are shown
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

Hover any row to reveal the private context that session wrote for the ranking
agent.

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

**Resource budget.** Each rank spawns a `claude` process, which is not small, so
the ranking agent runs under hard caps as well as the debounce: a floor between
consecutive model calls, a rolling hourly ceiling, and a bound on how long one
worker will loop. Past any of those the ordering falls back to the heuristic and
the header says so, rather than the tool quietly spending your machine. Hooks
skip spawning a worker when one is already running, so a busy machine does not
get a herd of short-lived processes. The window daemon backs its poll off while
nothing changes, and the dashboard re-parses state only when the file has
actually moved. Measured idle cost of the daemon: about 25 MB and 0.1% CPU.

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

## Reporting from a session

Installed sessions do this on their own. By hand:

```sh
agentdash report --status question \
  --tag debugging \
  --summary "Three sentences the user sees." \
  --context "Four sentences only the ranking agent sees."
```

`--status` is one of `working`, `done`, `question`, `blocked`. Only `working`
means nothing is needed from you. Summaries longer than three sentences (or
contexts longer than four) are clipped, with a note on stderr.

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
  "min_available_percent": 12       // pause ranking below this much free memory
}
```

`+` and `-` in the dashboard write `top_n` back to this file, so the setting
sticks.

## Keys

| Key | Does |
| --- | --- |
| hover | reveal a session's ranker-only context |
| `a` | show every session in full / return to the fold |
| `+` `-` | change how many rows stay expanded |
| `r` | force a rerank now (costs one model call) |
| `o` | bring the top session's iTerm2 window to the front |
| click | bring that session's iTerm2 window to the front |
| `m` | toggle mouse reporting — turn it off to select text with the mouse |
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
python3 tests/test_flow.py       # windows, colours, reports, hooks, reaping
python3 tests/render_demo.py 100 # render a synthetic roster; --hover --all --stale --empty
```

None of them need iTerm2, a Claude session, or the network.
