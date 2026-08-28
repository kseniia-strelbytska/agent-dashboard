"""Installer idempotency: installing three times must equal installing once,
and uninstalling must leave the user's files exactly as it found them.

Runs against a throwaway $HOME, so it never touches the real one.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHILD = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, %(root)r)
from agentdash import installer

launcher = Path(os.environ["HOME"]) / ".local" / "bin" / "agentdash"
launcher.parent.mkdir(parents=True, exist_ok=True)
launcher.write_text("#!/bin/sh\n")

zshrc = Path(os.environ["HOME"]) / ".zshrc"
zshrc.write_text("# the user's own config\nexport EDITOR=vim\n")
settings = Path(os.environ["HOME"]) / ".claude" / "settings.json"
settings.parent.mkdir(parents=True, exist_ok=True)
settings.write_text(json.dumps({
    "theme": "dark",
    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/say done"}]}]},
}, indent=2))
memory = Path(os.environ["HOME"]) / ".claude" / "CLAUDE.md"
memory.write_text("# My notes\n\nAlways use tabs.\n")

before = {p: p.read_text() for p in (zshrc, settings, memory)}

for _ in range(3):
    installer.install_hooks(launcher)
    installer.install_instructions(launcher)
    installer.install_shell_snippet(launcher)

after = json.loads(settings.read_text())
counts = {ev: sum(1 for e in after["hooks"][ev] if installer._is_ours(e))
          for ev in installer.HOOK_EVENTS}
print(json.dumps({
    "ours_per_event": counts,
    "kept_foreign_hook": any("say done" in json.dumps(e) for e in after["hooks"]["Stop"]),
    "kept_theme": after.get("theme"),
    "zshrc_blocks": zshrc.read_text().count(installer.BEGIN),
    "memory_blocks": memory.read_text().count(installer.MD_BEGIN),
    "backup_exists": settings.with_suffix(".json.agentdash-backup").exists(),
}))

installer.remove_hooks()
installer._remove_block(memory, installer.MD_BEGIN, installer.MD_END)
installer._remove_block(zshrc, installer.BEGIN, installer.END)

final = json.loads(settings.read_text())
print(json.dumps({
    "hooks_left": sum(1 for ev in final.get("hooks", {})
                      for e in final["hooks"][ev] if installer._is_ours(e)),
    "foreign_hook_survived": any("say done" in json.dumps(e)
                                 for e in final.get("hooks", {}).get("Stop", [])),
    "zshrc_restored": zshrc.read_text().strip() == before[zshrc].strip(),
    "memory_restored": memory.read_text().strip() == before[memory].strip(),
}))
''' % {"root": ROOT}

FAILURES = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILURES.append(label)


def main():
    home = tempfile.mkdtemp(prefix="agentdash-install-home-")
    env = dict(os.environ, HOME=home, AGENTDASH_HOME=os.path.join(home, ".agent-dashboard"))
    try:
        proc = subprocess.run([sys.executable, "-c", CHILD], env=env,
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            return 1
        installed, removed = (json.loads(l) for l in proc.stdout.strip().splitlines())
    finally:
        shutil.rmtree(home, ignore_errors=True)

    print("installing three times")
    check(all(n == 1 for n in installed["ours_per_event"].values()),
          "exactly one hook per event, not three (%s)" % installed["ours_per_event"])
    check(installed["kept_foreign_hook"], "an unrelated Stop hook of yours is left alone")
    check(installed["kept_theme"] == "dark", "unrelated settings survive")
    check(installed["zshrc_blocks"] == 1, "one managed block in .zshrc, not three")
    check(installed["memory_blocks"] == 1, "one managed block in CLAUDE.md, not three")
    check(installed["backup_exists"], "settings.json is backed up before editing")

    print("uninstalling")
    check(removed["hooks_left"] == 0, "every agentdash hook is removed")
    check(removed["foreign_hook_survived"], "your own hook still survives uninstall")
    check(removed["zshrc_restored"], ".zshrc is byte-for-byte as it was")
    check(removed["memory_restored"], "CLAUDE.md is byte-for-byte as it was")

    print("")
    if FAILURES:
        print("%d FAILURES" % len(FAILURES))
        return 1
    print("all installer checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
