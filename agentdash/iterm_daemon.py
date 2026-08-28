"""iTerm2 window watcher: assigns a unique palette colour to every window.

Runs inside iTerm2's own Python runtime (as an AutoLaunch script) because that
is the only runtime with the `iterm2` package and a live connection to the app.

Responsibilities:
  1. Watch for windows opening/closing and keep state.json's window table in
     sync, allocating a colour no other *concurrently open* window is using and
     freeing it the moment the window closes.
  2. Paint each window's sessions with its colour (background tint).
  3. Answer `resolve` queries over a unix socket so a freshly-started shell can
     learn which window it is in and what colour it was given.
"""
import asyncio
import fcntl
import json
import os
import sys
import time
import traceback

import iterm2

from . import bridge, config, state

NEUTRAL = "#0B0B0D"          # the dashboard window stays out of the palette
# The session monitors wake us the instant a window opens or closes, so the poll
# is only a safety net for anything they miss. It starts tight and backs off
# while nothing changes, rather than hammering iTerm2 over IPC all day.
POLL_MIN_INTERVAL = 0.7
POLL_MAX_INTERVAL = 6.0
POLL_BACKOFF = 1.5
HEARTBEAT_INTERVAL = 5.0


def _log(msg: str) -> None:
    config.ensure_dirs()
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(config.DAEMON_LOG, "a") as fh:
            fh.write(line)
    except OSError:
        pass
    sys.stderr.write(line)


def _claim_singleton():
    """Hold an exclusive lock for the lifetime of the process.

    A manually started daemon outlives an iTerm2 restart (the API client
    reconnects), and iTerm2 then autolaunches a second one. Two daemons would
    fight over the socket and the colour table, so the loser exits quietly.
    Returns the open fd, which must stay open; None if another daemon has it.
    """
    config.ensure_dirs()
    fd = os.open(str(config.HOME / "daemon.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def _color(hex_value: str) -> "iterm2.Color":
    v = hex_value.lstrip("#")
    return iterm2.Color(int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))


class Daemon:
    def __init__(self, connection, app):
        self.connection = connection
        self.app = app
        self._applied = {}       # iterm session uuid -> colour hex last painted
        self._sess_to_window = {}
        self._colours = {}       # window_id -> colour
        self._ttys = {}          # tty path -> window_id
        self._wake = asyncio.Event()

    # -- iTerm2 side ----------------------------------------------------------

    def _dashboard_sessions(self):
        data = state.read()
        return set(data.get("meta", {}).get("dashboard_iterm_sessions") or [])

    async def sync(self) -> bool:
        """Reconcile with iTerm2. Returns True when anything actually changed."""
        try:
            await self.app.async_refresh()
        except Exception:
            pass
        live = {}
        sess_to_window = {}
        objects = {}
        ttys = {}
        for window in self.app.terminal_windows:
            wid = window.window_id
            uuids = []
            for tab in window.tabs:
                for session in tab.sessions:
                    uuids.append(session.session_id)
                    sess_to_window[session.session_id] = wid
                    objects[session.session_id] = session
                    # The tty is how a containerised session is traced back to
                    # the host window that is driving it.
                    try:
                        tty = await session.async_get_variable("tty")
                    except Exception:
                        tty = None
                    if tty:
                        ttys[tty] = wid
            live[wid] = uuids
        self._ttys = ttys
        self._sess_to_window = sess_to_window

        dash = self._dashboard_sessions()
        # A window hosting the dashboard is excluded from palette allocation so
        # it never steals a colour from a working window.
        dash_windows = {sess_to_window[s] for s in dash if s in sess_to_window}
        allocatable = {w: s for w, s in live.items() if w not in dash_windows}

        colours = state.sync_windows(allocatable)
        for wid in dash_windows:
            colours[wid] = NEUTRAL
        changed = colours != self._colours
        self._colours = colours

        for uuid, session in objects.items():
            wanted = colours.get(sess_to_window.get(uuid))
            if not wanted:
                continue
            if self._applied.get(uuid) == wanted:
                continue
            await self._paint(session, wanted)
            self._applied[uuid] = wanted
            changed = True
        for uuid in [u for u in self._applied if u not in objects]:
            del self._applied[uuid]
            changed = True

        for wid, colour in colours.items():
            if colour != NEUTRAL:
                state.mark_painted(wid, colour)
        return changed

    async def _paint(self, session, colour_hex):
        """Tint one session. LocalWriteOnlyProfile keeps the change scoped to
        this session instead of mutating the shared profile on disk."""
        try:
            change = iterm2.LocalWriteOnlyProfile()
            change.set_background_color(_color(colour_hex))
            change.set_use_cursor_guide(False)
            await session.async_set_profile_properties(change)
            await session.async_set_variable("user.agentdash_color", colour_hex)
        except Exception as exc:
            _log("paint failed for %s: %s" % (session.session_id, exc))

    # -- socket side ----------------------------------------------------------

    async def _handle_client(self, reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not raw:
                return
            req = json.loads(raw.decode())
            reply = await self._dispatch(req)
        except Exception as exc:
            reply = {"ok": False, "error": str(exc)}
        try:
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        except OSError:
            pass
        finally:
            try:
                writer.close()
            except OSError:
                pass

    async def _dispatch(self, req):
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "pid": os.getpid(), "windows": len(self._colours),
                    "version": config.VERSION}
        if op == "resolve":
            uuid = req.get("iterm_session") or ""
            # The shell can start before our poll has noticed the new session.
            for _ in range(12):
                wid = self._sess_to_window.get(uuid)
                if wid:
                    return {"ok": True, "window_id": wid,
                            "color": self._colours.get(wid) or "",
                            "is_dashboard": self._colours.get(wid) == NEUTRAL}
                await self.sync()
                await asyncio.sleep(0.12)
            return {"ok": False, "error": "unknown iterm session %s" % uuid}
        if op == "repaint":
            self._applied.clear()
            await self.sync()
            return {"ok": True, "windows": len(self._colours)}
        if op == "focus":
            uuid = req.get("iterm_session") or ""
            for window in self.app.terminal_windows:
                for tab in window.tabs:
                    for session in tab.sessions:
                        if session.session_id == uuid:
                            await window.async_activate()
                            await tab.async_select()
                            await session.async_activate()
                            return {"ok": True}
            return {"ok": False, "error": "session %s is not open" % uuid}
        if op == "windows":
            return {"ok": True, "windows": self._colours}
        return {"ok": False, "error": "unknown op %r" % op}

    async def _serve(self):
        try:
            if config.DAEMON_SOCK.exists():
                config.DAEMON_SOCK.unlink()
        except OSError:
            pass
        config.ensure_dirs()
        server = await asyncio.start_unix_server(self._handle_client, path=str(config.DAEMON_SOCK))
        os.chmod(str(config.DAEMON_SOCK), 0o600)
        _log("socket listening at %s" % config.DAEMON_SOCK)
        async with server:
            await server.serve_forever()

    # -- loops ----------------------------------------------------------------

    async def _poll_loop(self):
        interval = POLL_MIN_INTERVAL
        while True:
            try:
                changed = await self.sync()
            except Exception:
                changed = False
                _log("sync error:\n%s" % traceback.format_exc())
            interval = (POLL_MIN_INTERVAL if changed
                        else min(POLL_MAX_INTERVAL, interval * POLL_BACKOFF))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
                self._wake.clear()
                interval = POLL_MIN_INTERVAL   # an event fired: be responsive again
            except asyncio.TimeoutError:
                pass

    async def _new_session_loop(self):
        async with iterm2.NewSessionMonitor(self.connection) as mon:
            while True:
                await mon.async_get()
                self._wake.set()

    async def _termination_loop(self):
        async with iterm2.SessionTerminationMonitor(self.connection) as mon:
            while True:
                await mon.async_get()
                self._wake.set()

    async def _heartbeat_loop(self):
        pid = os.getpid()
        while True:
            def mutate(data):
                data["meta"]["daemon_pid"] = pid
                data["meta"]["daemon_heartbeat"] = time.time()
                data["meta"]["daemon_version"] = config.VERSION
                if self._ttys:
                    data["meta"]["ttys"] = self._ttys
                return True
            try:
                state.update(mutate)
                state.reap()
                bridge.ingest()
            except Exception:
                _log("heartbeat error:\n%s" % traceback.format_exc())
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def run(self):
        _log("daemon starting (pid %d, agentdash %s)" % (os.getpid(), config.VERSION))
        await asyncio.gather(
            self._serve(),
            self._poll_loop(),
            self._new_session_loop(),
            self._termination_loop(),
            self._heartbeat_loop(),
        )


async def _main(connection):
    app = await iterm2.async_get_app(connection)
    await Daemon(connection, app).run()


def run():
    global _LOCK_FD
    _LOCK_FD = _claim_singleton()
    if _LOCK_FD is None:
        _log("another daemon already holds the lock; exiting")
        return
    iterm2.run_forever(_main)


_LOCK_FD = None
