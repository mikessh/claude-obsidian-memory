# claude-obsidian-memory

<p align="center">
  <img src="assets/logo.png" alt="Claude Shannon with Obsidian-gem eyes" width="480">
</p>

A Claude Code plugin that mirrors a repo's **Claude memory** — this session's private
`MEMORY.md` + `memory/*.md` fact-notes, plus the repo's committed `CLAUDE.md` — into a
folder inside an **Obsidian vault**, two-way synced, so you can read, search, and organize
that memory on your Mac and iPhone.

Multiple repos can point at the same vault folder; each gets its own subfolder.

## Why

Claude Code keeps per-repo memory in `~/.claude/projects/<encoded-cwd>/` and in the repo's
`CLAUDE.md`. That's invisible outside a Claude session. This plugin makes it a first-class,
searchable, linkable part of your Obsidian knowledge base — and lets you edit it on your
phone and have the edits flow back.

## Install

```
/plugin marketplace add mikessh/claude-obsidian-memory
/plugin install claude-obsidian-memory
```

## Use

| Command | What it does |
|---|---|
| `/memory-link <vault-folder>` | Associate this repo with a vault folder and do the first sync. |
| `/memory-sync` | Reconcile changes (status → push/pull, conflict prompts). |
| `/memory-sync analyse` | Same, then cross-link the mirror notes to related vault notes. |
| `/memory-compress` | Trim stale/irrelevant facts (reversibly, to `memory/_archive/`) and rewrite terse notes into readable prose, then re-sync. |

You can also just ask in natural language ("sync my memory to obsidian") — the
`obsidian-memory-sync` skill triggers on that.

### First-run

```
/memory-link ~/vaults/personal/projects/myrepo
```

Writes `.claude/obsidian-sync.json` in the repo (the association marker), creates a per-repo
subfolder in the vault, and pushes **one note per fact** plus a generated org kit:

```
<vault-folder>/<repo>/
  <slug>.md       one note per memory/<slug>.md, with queryable frontmatter
  CLAUDE.md.md    mirror of the repo's CLAUDE.md
  memory.base     Bases "By type" dashboard (core Obsidian ≥1.9, works on mobile)
  MEMORY.md       dashboard: rollup + ![[memory.base]] + backlinks to every fact
```

One-note-per-fact (not one big note) is what lets Obsidian's core **Bases**, **Graph
color-by-type**, **Backlinks**, and `["type":value]` **search** operate on the memory —
see [INTEROP.md](INTEROP.md).

### Ongoing

At the start of any session in a linked repo, a SessionStart hook prints a one-line reminder
that the repo is linked and that `/memory-sync` is available. Nothing syncs automatically —
you stay in control.

## How sync works

`skills/obsidian-memory-sync/sync.py` (stdlib only) hashes each side against the last-synced
state and classifies each fact note and the `CLAUDE.md` mirror:

- **push** — repo changed, vault didn't → write the vault note + regenerate the org kit.
- **pull** — vault changed, repo didn't → write edits back into the memory files / `CLAUDE.md`.
- **conflict** — both changed → **never auto-merged**; `sync.py` refuses (`SKIP`), the skill
  shows a diff and asks which side wins, then `--force`s it.
- A new `*.md` note you create in the vault folder becomes a new memory file on pull (and
  gets a `MEMORY.md` index line).
- A fact that disappears from one side is flagged (`removed`) but never auto-deleted.

The repo schema (`name` / `description` / `metadata.type`) is flattened into queryable vault
frontmatter (`type`, `repo`, `created`, `last_synced`, `tags`) on push and re-nested on pull;
bodies are copied verbatim.

**Concurrency:** iCloud has no conflict resolution and desktop Obsidian silently overwrites
an externally-changed note that's open in its editor. Treat the Mac/Claude side as the single
writer and the phone as read-mostly; never run a second sync engine on the same vault folder.
Core File Recovery + git are the safety nets. Details in [INTEROP.md](INTEROP.md).

## Files

```
.claude-plugin/plugin.json        plugin manifest
.claude-plugin/marketplace.json   makes it installable as a 1-plugin marketplace
skills/obsidian-memory-sync/      the skill (SKILL.md drives judgment; sync.py does mechanics)
commands/memory-link.md           /memory-link
commands/memory-sync.md           /memory-sync
commands/memory-compress.md       /memory-compress
hooks/hooks.json                  SessionStart link reminder
scripts/session-check.py          the hook body (silent unless the repo is linked)
INTEROP.md                        Obsidian feature/plugin recommendations (core Tier 1, opt-in Tier 2)
```

`sync.py` subcommands: `init`, `status`, `push`, `pull`, `sync` (both directions at once),
`archive` / `restore` (reversible trim, used by compress), `selftest`.

> **Upgrading `sync.py`:** if a new version changes how content is hashed, every note will
> read as `conflict` once (old baseline vs new hash). Re-baseline with a single
> `push --force` (repo authoritative) or `pull --force` (vault authoritative) — pick the side
> whose current content is correct.

## Self-check

```
python3 skills/obsidian-memory-sync/sync.py selftest
```

Round-trips init → push → phone-edit → pull → new-vault-fact → conflict → force →
archive → restore in a scratch dir.
