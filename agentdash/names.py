"""Short, punchy, memorable session names.

Two syllables each side, hyphenated: `brisk-otter`, `wry-comet`. Deterministic
from the Claude session id so a session keeps its name across restarts, with a
collision walk so no two *live* sessions ever share one.
"""
import hashlib
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
