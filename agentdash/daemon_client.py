"""Tiny newline-delimited-JSON client for the iTerm2 window daemon."""
import json
import socket
import time
from typing import Optional

from . import config


class DaemonUnavailable(RuntimeError):
    pass


def request(payload: dict, timeout: float = 2.0) -> dict:
    if not config.DAEMON_SOCK.exists():
        raise DaemonUnavailable("daemon socket not present at %s" % config.DAEMON_SOCK)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(config.DAEMON_SOCK))
        sock.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        deadline = time.time() + timeout
        while b"\n" not in buf:
            if time.time() > deadline:
                raise DaemonUnavailable("timed out waiting for daemon reply")
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        if not buf.strip():
            raise DaemonUnavailable("daemon closed the connection without replying")
        return json.loads(buf.split(b"\n", 1)[0].decode())
    except (OSError, ValueError) as exc:
        raise DaemonUnavailable(str(exc))
    finally:
        sock.close()


def ping(timeout: float = 1.0) -> Optional[dict]:
    try:
        return request({"op": "ping"}, timeout=timeout)
    except DaemonUnavailable:
        return None


def resolve_window(iterm_session: str, timeout: float = None) -> dict:
    """Ask the daemon which window owns `iterm_session`, and its colour."""
    timeout = timeout or config.WINDOW_REGISTER_TIMEOUT
    return request({"op": "resolve", "iterm_session": iterm_session}, timeout=timeout)


def repaint(timeout: float = 5.0) -> dict:
    return request({"op": "repaint"}, timeout=timeout)
