"""Bring the iTerm2 window hosting a given session to the front."""
from . import daemon_client


def focus(iterm_session_uuid: str) -> bool:
    try:
        reply = daemon_client.request(
            {"op": "focus", "iterm_session": iterm_session_uuid}, timeout=3.0)
    except daemon_client.DaemonUnavailable:
        return False
    return bool(reply.get("ok"))
