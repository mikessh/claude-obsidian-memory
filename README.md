# claude-obsidian-memory

A Claude Code plugin that mirrors a repo's **Claude memory** — this session's private
`MEMORY.md` + `memory/*.md` fact-notes, plus the repo's committed `CLAUDE.md` — into a
folder inside an **Obsidian vault**, two-way synced, so you can read, search, and organize
that memory on your Mac and iPhone.

Multiple repos can point at the same vault folder; note filenames are prefixed per repo.

## Why

Claude Code keeps per-repo memory in `~/.claude/projects/<encoded-cwd>/` and in the repo's
`CLAUDE.md`. That's invisible outside a Claude session. This plugin makes it a first-class,
searchable, linkable part of your Obsidian knowledge base — and lets you edit it on your
phone and have the edits flow back.

## Install

```
/plugin marketplace add ~/vcs/code/claude-obsidian-memory
/plugin install claude-obsidian-memory
```

(Or add the repo URL once it's pushed to a remote.)

> If you were running the standalone `~/.claude/skills/obsidian-memory-sync/` prototype,
> remove it after installing the plugin to avoid a duplicate skill name.

## Use

| Command | What it does |
|---|---|
| `/memory-link <vault-folder>` | Associate this repo with a vault folder and do the first sync. |
| `/memory-sync` | Reconcile changes (status → push/pull, conflict prompts). |
| `/memory-sync analyse` | Same, then cross-link the mirror notes to related vault notes. |

You can also just ask in natural language ("sync my memory to obsidian") — the
`obsidian-memory-sync` skill triggers on that.

### First-run

```
/memory-link ~/vaults/personal/projects/myrepo
```

Writes `.claude/obsidian-sync.json` in the repo (the association marker), creates the vault
folder, and pushes two notes:

- `<repo>-claude-memory.md` — every `MEMORY.md`-indexed fact-note, one `## ` section each.
- `<repo>-CLAUDE.md` — a mirror of the repo's `CLAUDE.md`.

### Ongoing

At the start of any session in a linked repo, a SessionStart hook prints a one-line reminder
that the repo is linked and that `/memory-sync` is available. Nothing syncs automatically —
you stay in control.

## How sync works

`skills/obsidian-memory-sync/sync.py` (stdlib only) hashes each side against the last-synced
state and classifies each note:

- **push** — repo changed, vault didn't → overwrite the vault note.
- **pull** — vault changed, repo didn't → write edits back into the memory files / `CLAUDE.md`.
- **conflict** — both changed → **never auto-merged**; the skill shows a diff and asks you
  which side wins, then forces it.
- New `## ` sections you add in the vault note become new memory files on pull.
- A `## ` section you *delete* in the vault is flagged (`removed_in_vault`) but never
  auto-deleted — you're asked first.

Frontmatter on each memory file (`name`, `description`, `metadata.type`) is preserved across
round-trips; only the body is mirrored.

## Files

```
.claude-plugin/plugin.json        plugin manifest
.claude-plugin/marketplace.json   makes it installable as a 1-plugin marketplace
skills/obsidian-memory-sync/      the skill (SKILL.md drives judgment; sync.py does mechanics)
commands/memory-link.md           /memory-link
commands/memory-sync.md           /memory-sync
hooks/hooks.json                  SessionStart link reminder
scripts/session-check.py          the hook body (silent unless the repo is linked)
```

## Self-check

```
python3 skills/obsidian-memory-sync/sync.py selftest
```

Round-trips push → phone-edit → pull → new-section-pull in a scratch dir.
