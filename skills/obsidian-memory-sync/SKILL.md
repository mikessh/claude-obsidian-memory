---
name: obsidian-memory-sync
description: Associate this repo's Claude memory (this session's private MEMORY.md/memory/*.md and the repo's committed CLAUDE.md) with a folder in the user's Obsidian vault, then two-way sync and cross-link it. Use when the user gives a vault path to link a repo to, asks to "sync memory to obsidian", "associate this repo with my vault", or asks to sync/pull/push/analyse an existing association.
---

# Obsidian Memory Sync

Bridges two systems that are normally separate: Claude Code's per-repo memory
(`~/.claude/projects/<encoded-cwd>/`, this session's system prompt tells you
the exact path) and the repo's committed `CLAUDE.md`, mirrored into two notes
in an Obsidian vault folder the user names. Sync is two-way: edits made on
the phone in Obsidian flow back into the repo/memory, and vice versa.

All mechanical work (hashing, diffing, section parsing, frontmatter
preservation) is done by `sync.py` in this skill's directory — always shell
out to it rather than hand-editing the mirror notes or memory files
yourself. Judgment calls (conflicts, deletions, cross-linking) are yours.

## Associate (first time for this repo)

The user gives a vault folder path (e.g. `~/vaults/personal/projects/foo`).

```
python3 ~/.claude/skills/obsidian-memory-sync/sync.py init \
  --repo <repo_root> --memory <this-session's-memory-dir> --vault <vault_path>
```

This writes `<repo_root>/.claude/obsidian-sync.json` (the association
marker — vault path, memory dir, and per-note sync hashes) and creates the
vault folder if needed. Note filenames are `<repo-basename>-claude-memory.md`
and `<repo-basename>-CLAUDE.md`, so multiple repos can point at the same
vault folder without colliding.

Then do the initial push (see below) for both notes so the vault folder has
content immediately.

If the repo is a git repo, check `.gitignore` and add
`.claude/obsidian-sync.json` if it's not already covered — it holds an
absolute local vault path, machine-specific state that shouldn't be shared
unless the user says otherwise.

## Sync (every subsequent invocation)

No path needed — the marker file has it.

```
python3 ~/.claude/skills/obsidian-memory-sync/sync.py status --repo <repo_root>
```

Returns JSON per note (`memory`, `claude_md`) with `state`:

- `init` / `no_change` — nothing to do.
- `push` — repo changed since last sync, vault didn't: run
  `push --repo <repo_root> --note <name>`.
- `pull` — vault changed, repo didn't: run
  `pull --repo <repo_root> --note <name>`.
- `conflict` — **both** changed since last sync. Never auto-resolve. Read
  both sides (`cat` the vault note; read the CLAUDE.md or memory files),
  show the user a short diff summary, and ask (AskUserQuestion) which side
  wins — or offer to hand-merge. Then run `push --force` or `pull --force`
  accordingly. Only pass `--force` once the user has chosen.

`status` also reports `removed_in_vault`: memory files whose `## ` section
disappeared from the vault note (user deleted it on their phone). **Never
delete the memory file automatically** — ask the user first, then remove it
and its `MEMORY.md` index line yourself if they confirm.

New `## ` sections added directly in the vault note (no `<!-- file: -->`
marker) are picked up automatically on `pull` as new memory files — this is
expected two-way behavior, not a conflict.

## Analyse (cross-link) — on request, or after a sync with real changes

This step is judgment, not scripted. Glob the vault folder (and, loosely,
sibling notes one level up if the folder is nested in a topic area) for
other `*.md` notes. Compare their titles/content against what's in the two
mirror notes just synced. Where there's a genuine topical overlap, propose
`[[Note title]]` links — show the proposed links to the user before writing
them, since this edits notes beyond the two mirror files. Follow the
`obsidian` skill's linking and frontmatter conventions.

Don't cross-link speculatively against the whole vault by default — that's
unbounded and mostly noise. Scope to the target folder unless the user asks
for a wider sweep.

## Self-check

`python3 ~/.claude/skills/obsidian-memory-sync/sync.py selftest` round-trips
push → phone-edit → pull → new-section-pull in a scratch dir. Re-run it
after editing `sync.py`.
