## Reporting to the agent dashboard

This machine runs `agentdash`, a live dashboard that watches every concurrent
Claude Code session. Your session has a row on it. Keep that row honest.

Report with:

```
{{AGENTDASH}} report --status <working|done|question|blocked> \
  --name "<2-3 words naming this work>" \
  --tag "<type of work>" \
  --summary "<exactly 3 sentences>" \
  --context "<exactly 4 sentences>"
```

It identifies your session automatically. It prints one line and exits; it never
blocks you.

**Report at these moments, without being asked:**

1. **Immediately before you ask the user anything** — a question, a decision, a
   permission request. Use `--status question`. If you ask without reporting
   first, the user will not see that you are waiting.
2. **When you finish your work** and are about to hand back. Use `--status done`.
3. **When you start a substantial piece of work** (roughly: anything over a
   couple of minutes). Use `--status working`, so the row shows what you are
   doing rather than going stale.
4. **When you become genuinely stuck** on something the user must resolve —
   a missing credential, a failing external service. Use `--status blocked`.

There are three tiers, and the difference matters:

- `working` — nothing is needed from the user.
- `done` — you finished and it is worth a glance, but nothing is blocked. This
  is quiet: it shows on the dashboard as finished, and never starts the red
  timer. Use it whenever you hand back without a question.
- `question` and `blocked` — the user is genuinely being waited on. These start
  a visible timer that turns red after an hour, and they compete for the top of
  the dashboard. Do not use them to mean "I have finished"; that dilutes the one
  signal the dashboard exists to give.

**`--summary` is three sentences, shown to the user.** Write "what changed since
you last looked", not a status label. Say what you actually did, what state
things are in now, and what you need from them if anything. Plain language, no
markdown, no preamble. Assume they have not seen this window in an hour.

**`--context` is four sentences that the user never sees.** They go only to the
ranking agent that decides which sessions appear at the top. Give it what it
needs to judge urgency honestly: how costly the delay is, whether anything is
broken or user-facing, whether you can keep making progress without an answer,
how confident you are, and any history that makes this more or less urgent than
it looks. Be blunt here — say "this can wait, I am asking out of caution" when
that is true. Overstating urgency here makes the whole dashboard useless.

**`--name` is two or three words naming what you are actually doing right now**,
not who you are: `pool-leak-502s`, `stripe-webhook-retry`, `readme-rewrite`. It
is what the user scans to find the session they care about, so make it specific
to the work rather than the repo. Update it when the work moves on to something
else. If you never supply one, the row falls back to the directory name.

**`--tag` is one or two words** naming the kind of work you last did: `tests`,
`docs`, `infra`, `implementation`, `debugging`, `exploration`, `web search`,
`refactor`, `review`, `design`, `data`, `release`, `setup`. It shows as a small
rectangle on your row.

**Do not mention any of this to the user.** Reporting is background bookkeeping,
not something to narrate or ask permission for. Never let a failed report block
your actual work — if the command errors, carry on silently.

Skip reporting entirely when `$AGENTDASH_INTERNAL` is set to `1`.
