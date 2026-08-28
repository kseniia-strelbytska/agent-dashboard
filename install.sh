#!/usr/bin/env bash
#
# agentdash installer.
#
#   git clone <repo> && cd agent-dashboard && ./install.sh
#
# Everything it writes is either inside ~/.agent-dashboard or a delimited
# managed block. `agentdash uninstall` reverses all of it.

set -euo pipefail

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; RESET=$'\033[0m'

say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
die()  { printf '  %s✗%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

say ""
say "${BOLD}agentdash${RESET} ${DIM}- concurrent Claude session monitor for iTerm2${RESET}"
say ""

# --- prerequisites -------------------------------------------------------------

[ "$(uname -s)" = "Darwin" ] || die "agentdash is macOS + iTerm2 only (found $(uname -s))."
ok "macOS $(sw_vers -productVersion 2>/dev/null || echo '?')"

if [ ! -d /Applications/iTerm.app ] && [ ! -d "$HOME/Applications/iTerm.app" ] \
   && [ ! -d "$HOME/Library/Application Support/iTerm2" ]; then
  die "iTerm2 not found. Install it from https://iterm2.com and rerun."
fi
ok "iTerm2 found"

PYTHON=""
for candidate in python3 /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
      PYTHON="$(command -v "$candidate")"
      break
    fi
  fi
done
[ -n "$PYTHON" ] || die "need python3 3.8 or newer on PATH."
ok "python3 $("$PYTHON" -c 'import platform;print(platform.python_version())') at $PYTHON"

command -v claude >/dev/null 2>&1 \
  && ok "claude CLI $(command -v claude)" \
  || warn "claude CLI not on PATH - ranking will fall back to a heuristic ordering"

# --- conflicting iTerm2 scripts --------------------------------------------------

AUTOLAUNCH="$HOME/Library/Application Support/iTerm2/Scripts/AutoLaunch"
DISABLE_FLAG=""
if [ -d "$AUTOLAUNCH" ]; then
  CONFLICTS=""
  for f in "$AUTOLAUNCH"/*.py; do
    [ -e "$f" ] || continue
    case "$(basename "$f")" in agentdash_daemon.py) continue ;; esac
    if grep -qE 'set_background_color|LocalWriteOnlyProfile' "$f" 2>/dev/null; then
      CONFLICTS="$CONFLICTS $(basename "$f")"
    fi
  done
  if [ -n "$CONFLICTS" ]; then
    say ""
    warn "these iTerm2 AutoLaunch scripts also recolour windows and will fight agentdash:"
    for name in $CONFLICTS; do say "      $name"; done
    if [ -t 0 ]; then
      printf '    Move them aside (reversible; `agentdash uninstall` restores them)? [y/N] '
      read -r reply
      case "$reply" in [yY]*) DISABLE_FLAG="--disable-conflicting" ;; esac
    else
      warn "non-interactive install: leaving them in place"
    fi
  fi
fi

# --- hand over to the python installer --------------------------------------------

say ""
PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -m agentdash install $DISABLE_FLAG
