---
description: Sync this repo's Claude memory with its associated Obsidian vault folder (status → push/pull → cross-link)
argument-hint: "[analyse]"
---

Use the `obsidian-memory-sync` skill to sync the current repo with its associated Obsidian
vault folder.

1. Run the skill's `status` for this repo. If there's no `.claude/obsidian-sync.json`, tell
   the user the repo isn't linked yet and suggest `/memory-link <vault-path>`.
2. For each note (`memory`, `claude_md`) act on its state:
   - `push` / `pull` — run it.
   - `conflict` — show a short diff of both sides and ask which wins (never auto-resolve),
     then `push --force` or `pull --force`.
   - `removed_in_vault` — ask before deleting the memory file + its `MEMORY.md` line.
3. If $ARGUMENTS contains `analyse` (or after a sync with real changes), do the
   cross-link/analyse step: scan the vault folder for related notes and propose `[[links]]`,
   confirming before editing notes beyond the two mirror files.
