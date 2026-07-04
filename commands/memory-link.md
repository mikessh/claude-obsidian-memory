---
description: Associate this repo's Claude memory with an Obsidian vault folder and do the first sync
argument-hint: <vault-folder-path>
---

Use the `obsidian-memory-sync` skill to **associate** the current repo with the Obsidian
vault folder at: $ARGUMENTS

Steps:
1. Resolve the repo root (the current working directory's repo, or cwd if not a git repo)
   and this session's Claude memory dir (from the system prompt's memory path).
2. Run the skill's `init` to write `.claude/obsidian-sync.json`, then `push` both the
   `memory` and `claude_md` notes so the vault folder is populated.
3. If it's a git repo, ensure `.claude/obsidian-sync.json` is gitignored (it holds a
   machine-local absolute vault path).
4. Offer to generate the core-Obsidian **org kit** (a `.base` view + a dashboard note) in
   the vault folder so the memory notes are browsable on mobile.

If no vault path was given in $ARGUMENTS, ask for one.
