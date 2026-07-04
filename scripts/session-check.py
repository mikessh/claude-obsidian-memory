#!/usr/bin/env python3
"""SessionStart hook: if the current repo is linked to an Obsidian vault folder,
print a one-line reminder so the session knows it can /memory-sync.

Silent (exit 0, no output) when there's no association marker — most repos.
ponytail: reads only the marker JSON; never touches the vault (iCloud stat could
stall) and never auto-syncs — syncing is an explicit /memory-sync choice.
"""
import json
import sys
from pathlib import Path

marker = Path.cwd() / ".claude" / "obsidian-sync.json"
if not marker.exists():
    sys.exit(0)

try:
    cfg = json.loads(marker.read_text())
    vault = cfg.get("vault_dir", "?")
    notes = ", ".join(n["vault_file"] for n in cfg.get("notes", {}).values())
    print(
        f"[obsidian-memory] This repo's Claude memory is linked to Obsidian vault "
        f"folder: {vault} (notes: {notes}). Run /memory-sync to reconcile changes."
    )
except Exception:
    # a malformed marker shouldn't break session startup
    sys.exit(0)
