---
description: Sync this repo's Claude memory with its associated Obsidian vault folder (status → push/pull → cross-link)
argument-hint: "[analyse]"
---

Use the `obsidian-memory-sync` skill to sync the current repo with its associated Obsidian
vault folder.

1. Run the skill's `status` for this repo. If there's no `.claude/obsidian-sync.json`, tell
   the user the repo isn't linked yet and suggest `/memory-link <vault-path>`. If `status`
   errors or every fact reads as `conflict`, run `sync.py doctor --repo <repo_root>` — it
   pinpoints Full Disk Access blocks, a stale hash scheme, or index drift, each with its fix.
2. Act on each fact's and `claude_md`'s state:
   - `push` / `pull` — run it (`push` also regenerates the org kit).
   - `conflict` — show a short diff of both sides and ask which wins (never auto-resolve),
     then `push --force` / `pull --force` (use `--only <slug>.md` to scope to one fact).
   - `removed` — ask before deleting the counterpart file + its `MEMORY.md` line.
3. If $ARGUMENTS contains `analyse` (or after a sync with real changes), do the
   cross-link/analyse step: scan the vault folder for related notes and propose `[[links]]`,
   confirming before editing notes beyond the mirrored ones.
