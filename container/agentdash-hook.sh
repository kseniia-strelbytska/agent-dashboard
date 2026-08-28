#!/bin/sh
# agentdash container hook entrypoint: agentdash-hook <event>
#
# Mirrors the host hooks for a session that cannot reach the host: it spools
# lifecycle events, and hands the reporting instructions to a session that has
# ended a turn without reporting.
#
# There is no Python and no jq in here, so stdin is scraped with sed. The two
# fields we need - session_id and cwd - are plain string values, and the
# instruction payload is pre-escaped by the host installer so this script only
# has to cat it.

set -u
event="${1:-}"
STATE_DIR="${AGENTDASH_CONTAINER_HOME:-$HOME/.agentdash}"
SESSIONS="$STATE_DIR/sessions"
mkdir -p "$SESSIONS" 2>/dev/null || true

payload=$(cat 2>/dev/null | tr -d '\n\r')
field() {
  printf '%s' "$payload" | sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
}
sid="$(field session_id)"; [ -n "$sid" ] || sid="${CLAUDE_CODE_SESSION_ID:-}"
cwd="$(field cwd)"; [ -n "$cwd" ] || cwd="$PWD"
[ -n "$sid" ] || exit 0

spool_dir() {
  for d in "${AGENTDASH_SPOOL:-}" /work/.agentdash-spool "$cwd/.agentdash-spool"; do
    [ -n "$d" ] || continue
    if mkdir -p "$d" 2>/dev/null && [ -w "$d" ]; then printf '%s' "$d"; return 0; fi
  done
  return 1
}
esc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

spool() {
  SPOOL="$(spool_dir)" || return 0
  now=$(date +%s)
  f="$SPOOL/$now-$$-$(od -An -N3 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n' || echo 0).json"
  printf '{"v":1,"kind":"hook","event":"%s","session_id":"%s","cwd":"%s","at":%s}\n' \
    "$(esc "$event")" "$(esc "$sid")" "$(esc "$cwd")" "$now" > "$f.tmp" 2>/dev/null \
    && mv "$f.tmp" "$f" 2>/dev/null || true
}

# The instructions, pre-escaped as a JSON string body by the host installer.
emit_instructions() {
  [ -f "$STATE_DIR/instructions.jsonfrag" ] || return 0
  printf '{"hookSpecificOutput":{"hookEventName":"%s","additionalContext":"' "$1"
  cat "$STATE_DIR/instructions.jsonfrag"
  printf '"}}'
}

case "$event" in
  session-start)
    spool
    emit_instructions SessionStart
    ;;
  user-prompt)
    spool
    # Exactly one injection per silent turn: `stop` arms it, this disarms it.
    if [ ! -f "$SESSIONS/$sid.reported" ] && [ -f "$SESSIONS/$sid.stopped" ]; then
      rm -f "$SESSIONS/$sid.stopped" 2>/dev/null || true
      emit_instructions UserPromptSubmit
    fi
    ;;
  stop|notification)
    : > "$SESSIONS/$sid.stopped" 2>/dev/null || true
    spool
    ;;
  session-end)
    rm -f "$SESSIONS/$sid.reported" "$SESSIONS/$sid.stopped" 2>/dev/null || true
    spool
    ;;
  *) ;;
esac
exit 0
