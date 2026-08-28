#!/bin/sh
# agentdash container reporter.
#
# Installed inside a container by `agentdash bridge install`. The container has
# no Python and cannot see the host's state directory, so this writes a JSON
# record into a spool directory on a shared mount and returns immediately. The
# host daemon ingests the spool into the dashboard.
#
# Deliberately POSIX sh: containers cannot be assumed to have anything else.

set -u
STATE_DIR="${AGENTDASH_CONTAINER_HOME:-$HOME/.agentdash}"
mkdir -p "$STATE_DIR/sessions" 2>/dev/null || true

status=""; name=""; tag=""; summary=""; context=""; sid="${CLAUDE_CODE_SESSION_ID:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --status)  status="${2:-}"; shift 2 ;;
    --name)    name="${2:-}"; shift 2 ;;
    --tag)     tag="${2:-}"; shift 2 ;;
    --summary) summary="${2:-}"; shift 2 ;;
    --context) context="${2:-}"; shift 2 ;;
    --session-id) sid="${2:-}"; shift 2 ;;
    --cwd)     cwd_override="${2:-}"; shift 2 ;;
    *) shift ;;
  esac
done

[ -n "$status" ] || { echo "agentdash: --status is required" >&2; exit 2; }
[ -n "$sid" ] || { echo "agentdash: no session id (CLAUDE_CODE_SESSION_ID unset)" >&2; exit 2; }

# Pick the first spool directory we can actually write to. The Bash tool runs
# inside a bubblewrap sandbox whose only writable path is the project itself,
# so the shared root is tried first and the project is the reliable fallback.
spool_dir() {
  for d in "${AGENTDASH_SPOOL:-}" /work/.agentdash-spool "$PWD/.agentdash-spool"; do
    [ -n "$d" ] || continue
    if mkdir -p "$d" 2>/dev/null && [ -w "$d" ]; then printf '%s' "$d"; return 0; fi
  done
  return 1
}

esc() {
  printf '%s' "$1" | tr '\n\r\t' '   ' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

SPOOL="$(spool_dir)" || { echo "agentdash: no writable spool directory" >&2; exit 1; }

now=$(date +%s)
file="$SPOOL/$now-$$-$(od -An -N3 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n' || echo 0).json"

{
  printf '{"v":1,"kind":"report","session_id":"%s"' "$(esc "$sid")"
  printf ',"status":"%s"' "$(esc "$status")"
  printf ',"cwd":"%s"' "$(esc "${cwd_override:-$PWD}")"
  printf ',"at":%s' "$now"
  [ -n "$name" ]    && printf ',"name":"%s"' "$(esc "$name")"
  [ -n "$tag" ]     && printf ',"tag":"%s"' "$(esc "$tag")"
  [ -n "$summary" ] && printf ',"summary":"%s"' "$(esc "$summary")"
  [ -n "$context" ] && printf ',"ranker_context":"%s"' "$(esc "$context")"
  printf '}\n'
} > "$file.tmp" 2>/dev/null && mv "$file.tmp" "$file" 2>/dev/null || {
  echo "agentdash: could not write the spool record" >&2; exit 1; }

# Remember that this session has reported, so the hooks stop handing it the
# instructions.
: > "$STATE_DIR/sessions/$sid.reported" 2>/dev/null || true
rm -f "$STATE_DIR/sessions/$sid.stopped" 2>/dev/null || true
echo "posted: $status${name:+ ($name)} -> dashboard spool"
