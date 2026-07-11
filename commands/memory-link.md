---
description: Associate this repo's Claude memory with an Obsidian vault folder and do the first sync
argument-hint: <vault-folder-path>
---

Use the `obsidian-memory-sync` skill to **associate** the current repo with the Obsidian
vault folder at: $ARGUMENTS

Steps:
1. Resolve the repo root (the current working directory's repo, or cwd if not a git repo)
   and this session's Claude memory dir (from the system prompt's memory path).
2. Run the skill's `init` to write `.claude/obsidian-sync.json`, then `push` to populate the
   vault folder with one note per fact, the `CLAUDE.md` mirror, and the generated org kit
   (`memory.base` + `MEMORY.md` dashboard).
3. Run `sync.py doctor --repo <repo_root>` to confirm the association is healthy (init already
   gitignores the marker and checks vault access / Full Disk Access).

If no vault path was given in $ARGUMENTS, ask for one.
