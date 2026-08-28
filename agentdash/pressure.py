"""Machine-wide memory pressure, read straight from the kernel.

The point of this module is restraint. Ranking spawns a `claude` process, which
is a large Node process; on a Mac that is already swapping, launching one is a
worse contribution to the user's day than a slightly stale ordering. So the
ranker asks here first, and the dashboard says plainly when it has stood down.

Read via sysctlbyname through ctypes rather than shelling out to /usr/sbin/sysctl,
because this is consulted on a timer and a subprocess per check is exactly the
kind of waste this module exists to avoid.
"""
import ctypes
import ctypes.util
import time
from typing import Dict, Optional, Tuple

NORMAL, WARNING, CRITICAL, UNKNOWN = "normal", "warning", "critical", "unknown"

# kern.memorystatus_vm_pressure_level is a bitfield: 1 normal, 2 warning, 4 critical.
_LEVELS = {1: NORMAL, 2: WARNING, 4: CRITICAL}
_RANK = {NORMAL: 0, UNKNOWN: 0, WARNING: 1, CRITICAL: 2}

_CACHE_SECONDS = 2.0
_cache: Dict = {}
_libc = None


def _load_libc():
    global _libc
    if _libc is None:
        path = ctypes.util.find_library("c") or "libc.dylib"
        _libc = ctypes.CDLL(path, use_errno=True)
    return _libc


def _sysctl_int(name: str) -> Optional[int]:
    try:
        libc = _load_libc()
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        rc = libc.sysctlbyname(name.encode(), ctypes.byref(value),
                               ctypes.byref(size), None, ctypes.c_size_t(0))
        return value.value if rc == 0 else None
    except (OSError, AttributeError, ValueError):
        return None


def read(force: bool = False) -> Dict:
    """Current pressure. Cached briefly; safe to call on every tick."""
    now = time.time()
    if not force and _cache and now - _cache.get("at", 0) < _CACHE_SECONDS:
        return _cache["value"]

    raw = _sysctl_int("kern.memorystatus_vm_pressure_level")
    available = _sysctl_int("kern.memorystatus_level")
    level = _LEVELS.get(raw, UNKNOWN if raw is None else NORMAL)
    value = {
        "level": level,
        "raw": raw,
        "available_percent": available,
        "known": raw is not None or available is not None,
    }
    _cache["at"] = now
    _cache["value"] = value
    return value


def describe(state: Optional[Dict] = None) -> str:
    state = state or read()
    pct = state.get("available_percent")
    if not state.get("known"):
        return "memory pressure unknown"
    if pct is None:
        return "memory pressure %s" % state["level"]
    return "memory %s, %d%% free" % (state["level"], pct)


def should_defer(cfg: Dict, state: Optional[Dict] = None) -> Tuple[bool, Optional[str]]:
    """(defer, why). True when spawning a model process would be antisocial."""
    if not cfg.get("defer_under_memory_pressure", True):
        return False, None
    state = state or read()
    if not state.get("known"):
        return False, None            # never withhold work on a guess

    floor = int(cfg.get("min_available_percent", 12))
    pct = state.get("available_percent")
    if _RANK.get(state["level"], 0) >= _RANK[CRITICAL]:
        return True, "machine memory critical - ranking paused, heuristic order"
    if _RANK.get(state["level"], 0) >= _RANK[WARNING]:
        return True, "machine under memory pressure - ranking paused, heuristic order"
    if pct is not None and pct < floor:
        return True, ("only %d%% memory free - ranking paused, heuristic order" % pct)
    return False, None
