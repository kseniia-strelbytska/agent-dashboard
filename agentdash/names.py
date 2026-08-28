"""Short, punchy, memorable session names.

Two syllables each side, hyphenated: `brisk-otter`, `wry-comet`. Deterministic
from the Claude session id so a session keeps its name across restarts, with a
collision walk so no two *live* sessions ever share one.
"""
import hashlib
import os
import re
from typing import Iterable

ADJECTIVES = (
    "brisk", "wry", "sly", "bold", "crisp", "keen", "lush", "swift", "grim",
    "calm", "deft", "fond", "hazy", "vast", "loud", "meek", "nimble", "odd",
    "prime", "quick", "rash", "sharp", "tame", "vivid", "warm", "zesty",
    "amber", "cobalt", "coral", "dusky", "ember", "frost", "gilded", "hollow",
    "iron", "jade", "lunar", "mossy", "noble", "onyx", "plush", "quiet",
    "rusty", "solar", "tidal", "umber", "velvet", "wild", "azure", "brave",
)

NOUNS = (
    "otter", "comet", "finch", "heron", "lynx", "moth", "newt", "owl", "pike",
    "quail", "raven", "shrew", "tapir", "viper", "wren", "yak", "adder",
    "bison", "crane", "dingo", "egret", "ferret", "gecko", "hawk", "ibis",
    "jackal", "kite", "lemur", "marten", "narwhal", "osprey", "puffin",
    "quokka", "robin", "stoat", "thrush", "urchin", "vole", "walrus", "zebu",
    "anchor", "beacon", "cinder", "delta", "ember", "fathom", "gully",
    "harbor", "inlet", "kernel",
)


_SLUG_OK = re.compile(r"[^a-z0-9]+")
MAX_LEN = 24

# Words that say nothing about what a session is doing.
_NOISE = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with",
    "some", "stuff", "things", "work", "working", "task", "session", "claude",
}


def slugify(text: str, limit: int = MAX_LEN) -> str:
    """Turn free text into a short kebab-case label, or '' if nothing survives."""
    words = [w for w in _SLUG_OK.sub(" ", (text or "").lower()).split()
             if w and w not in _NOISE]
    if not words:
        return ""
    out = words[0][:limit]
    for word in words[1:]:
        if len(out) + 1 + len(word) > limit:
            break
        out += "-" + word
    return out.strip("-")


def describe(supplied: str = "", cwd: str = "", tag: str = "",
             limit: int = MAX_LEN) -> str:
    """Best available label for what a session is actually doing.

    The session itself knows best, so an explicitly supplied name wins. Failing
    that, the directory it is working in at least says where, which beats a
    random animal. `generate()` remains the last resort so a row always has a
    name even before the first report.
    """
    picked = slugify(supplied, limit)
    if picked:
        return picked
    folder = os.path.basename(os.path.normpath(cwd)) if cwd else ""
    base = slugify(folder, limit)
    if base and tag:
        room = limit - len(base) - 1
        suffix = slugify(tag, max(3, room))
        if suffix and room >= 3:
            return "%s-%s" % (base, suffix)
    return base


def looks_generated(name: str) -> bool:
    """True for a placeholder like `brisk-otter`.

    Rows created before name_generated existed carry no flag, so fall back to
    recognising the shape: both halves drawn from our own word lists.
    """
    parts = (name or "").split("-")
    return (len(parts) == 2
            and parts[0] in ADJECTIVES
            and parts[1] in NOUNS)


def unique(name: str, taken) -> str:
    """Disambiguate a descriptive name that two sessions arrived at."""
    taken = {t.lower() for t in taken}
    if name.lower() not in taken:
        return name
    for n in range(2, 100):
        suffix = "-%d" % n
        candidate = name[:MAX_LEN - len(suffix)].rstrip("-") + suffix
        if candidate.lower() not in taken:
            return candidate
    return name


def _seed(key: str) -> int:
    return int(hashlib.blake2b(key.encode(), digest_size=8).hexdigest(), 16)


def generate(key: str, taken: Iterable[str] = ()) -> str:
    """Deterministic name for `key`, walked forward until it clears `taken`."""
    taken = {t.lower() for t in taken}
    n = _seed(key)
    total = len(ADJECTIVES) * len(NOUNS)
    for step in range(total):
        idx = (n + step * 7919) % total
        name = "%s-%s" % (ADJECTIVES[idx % len(ADJECTIVES)], NOUNS[idx // len(ADJECTIVES) % len(NOUNS)])
        if name not in taken:
            return name
    return "session-%s" % (n % 9973)
