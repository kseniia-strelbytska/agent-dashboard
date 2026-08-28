# agentdash - sourced from ~/.zshrc / ~/.bashrc by the installer.
#
# Registers this iTerm2 window with the window daemon and exports the palette
# colour it was assigned, so a Claude session started here already knows which
# colour it is shown in on the dashboard.

# Make the launcher reachable even in a shell that has not been reconfigured.
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) [ -d "$HOME/.local/bin" ] && PATH="$HOME/.local/bin:$PATH" && export PATH ;;
esac

_agentdash_register() {
  [ -n "$AGENTDASH_COLOR" ] && return 0
  [ -z "$ITERM_SESSION_ID" ] && return 0
  case "$TERM_PROGRAM" in
    iTerm.app) ;;
    *) return 0 ;;
  esac

  _ad_bin="${AGENTDASH_BIN:-}"
  if [ -z "$_ad_bin" ] || [ ! -x "$_ad_bin" ]; then
    _ad_bin="$(command -v agentdash 2>/dev/null)"
  fi
  [ -z "$_ad_bin" ] && return 0

  _ad_out="$("$_ad_bin" window-register --shell 2>/dev/null)"
  case "$_ad_out" in
    export*) eval "$_ad_out" ;;
  esac
  unset _ad_bin _ad_out
}

# Only interactive shells: scripts and hooks inherit the environment anyway.
case "$-" in
  *i*) _agentdash_register ;;
esac
