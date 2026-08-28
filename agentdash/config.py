"""Paths and tunable constants for agentdash.

Everything the tool writes lives under AGENTDASH_HOME (default ~/.agent-dashboard)
so that uninstalling is a single `rm -rf` plus the config-file edits install.sh made.
"""
import os
from pathlib import Path

VERSION = "1.0.1"
STATE_VERSION = 3


def _home() -> Path:
    override = os.environ.get("AGENTDASH_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agent-dashboard"


HOME = _home()
STATE_FILE = HOME / "state.json"
STATE_LOCK = HOME / "state.lock"
DAEMON_SOCK = HOME / "daemon.sock"
DAEMON_LOG = HOME / "logs" / "daemon.log"
RANKER_LOG = HOME / "logs" / "ranker.log"
HOOK_LOG = HOME / "logs" / "hook.log"
RANK_REQUEST = HOME / "rank.request"
RANK_LOCK = HOME / "rank.lock"
DASH_LOCK = HOME / "dashboard.lock"
CONFIG_FILE = HOME / "config.json"

# --- behaviour ---------------------------------------------------------------

# How many full-detail rows to show above the fold.
TOP_N_DEFAULT = 3

# Blocked-time threshold after which the timer renders red.
BLOCKED_RED_SECONDS = 3600

# Updates arriving within this window are batched into a single rerank.
RANK_DEBOUNCE_SECONDS = 5.0

# Ranker session hygiene. Auto-compaction keeps the session alive; these are the
# belt-and-braces limits after which we retire the session and start a fresh one.
RANKER_MAX_TURNS = 60
RANKER_MAX_AGE_SECONDS = 6 * 3600
# The dashboard warns the user once the ranker context passes these softer marks.
RANKER_STALE_TURNS = 40
RANKER_STALE_AGE_SECONDS = 4 * 3600
# A rank older than this (with pending updates) is reported as stale ordering.
RANKER_RESULT_STALE_SECONDS = 300
RANKER_MODEL = "sonnet"
RANKER_TIMEOUT_SECONDS = 120

# Spend limits on the ranking agent. Each rank spawns a `claude` process, which
# is a large Node process; on a loaded machine an unbounded loop of those is
# antisocial regardless of the token cost. These caps make the worst case
# bounded and visible rather than trusting the debounce alone.
RANK_MIN_INTERVAL_SECONDS = 20     # never two model calls closer than this
RANK_MAX_PER_HOUR = 60             # rolling ceiling; heuristic ordering beyond it
RANK_MAX_CONSECUTIVE = 40          # a single worker will not loop past this

# A session that has not been seen for this long is presumed dead and reaped.
SESSION_REAP_SECONDS = 8 * 3600
# How long the shell blocks waiting for the daemon to hand out a window colour.
WINDOW_REGISTER_TIMEOUT = 2.0

DEFAULTS = {
    "top_n": TOP_N_DEFAULT,
    "blocked_red_seconds": BLOCKED_RED_SECONDS,
    "rank_debounce_seconds": RANK_DEBOUNCE_SECONDS,
    "ranker_model": RANKER_MODEL,
    "ranking_enabled": True,
    "tint_background": True,
    "rank_min_interval_seconds": RANK_MIN_INTERVAL_SECONDS,
    "rank_max_per_hour": RANK_MAX_PER_HOUR,
}


def ensure_dirs() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "logs").mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        import json
        with open(CONFIG_FILE) as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(**changes) -> dict:
    """Merge `changes` into ~/.agent-dashboard/config.json. Best effort: a
    preference that fails to persist must never break the dashboard."""
    import json
    cfg = load_config()
    cfg.update(changes)
    stored = {k: v for k, v in cfg.items() if DEFAULTS.get(k) != v}
    try:
        ensure_dirs()
        tmp = str(CONFIG_FILE) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(stored, fh, indent=1, sort_keys=True)
        import os as _os
        _os.replace(tmp, str(CONFIG_FILE))
    except OSError:
        pass
    return cfg
