---
name: obsidian-memory-sync
description: Associate this repo's Claude memory (this session's private MEMORY.md/memory/*.md and the repo's committed CLAUDE.md) with a folder in the user's Obsidian vault, then two-way sync, organize, and cross-link it. Use when the user gives a vault path to link a repo to, asks to "sync memory to obsidian", "associate this repo with my vault", or asks to sync/pull/push/organize/analyse an existing association.
---

# Obsidian Memory Sync

Bridges Claude Code's per-repo memory (`~/.claude/projects/<encoded-cwd>/`, this session's
system prompt gives the exact path) and the repo's committed `CLAUDE.md` into a folder of
notes in an Obsidian vault. Two-way: edits made in Obsidian (e.g. on the phone) flow back
into the repo/memory, and vice versa.

All mechanical work is `sync.py` in this skill's directory — always shell out to it rather
than hand-editing mirror notes or memory files. Judgment calls (conflicts, deletions,
cross-linking, prose summaries) are yours.

## Model — one vault note per fact

The memory is mirrored **per fact**, not as one blob, so Obsidian's core features work:

```
<vault_dir>/<repo>/
  <slug>.md       one note per memory/<slug>.md, with queryable frontmatter
  CLAUDE.md.md    mirror of the repo's CLAUDE.md
  memory.base     generated Bases dashboard (core, mobile) — "By type" view
  MEMORY.md       generated dashboard: rollup + ![[memory.base]] + backlinks
```

`sync.py` flattens the repo schema (`name`/`description`/`metadata.type`) into vault
frontmatter (`type`, `repo`, `created`, `last_synced`, `tags: [memory, repo/<slug>]`) on
push and re-nests it on pull. Bodies are copied verbatim. This frontmatter is load-bearing:
Bases `groupBy`, core `["type":value]` search, and Graph color-groups all read it.

## Associate (first time for this repo)

The user gives a vault folder path (e.g. `~/vaults/personal/projects/foo`).

```
python3 <skill>/sync.py init --repo <repo_root> --memory <session-memory-dir> --vault <vault_path>
python3 <skill>/sync.py push --repo <repo_root>
```

`init` writes `<repo_root>/.claude/obsidian-sync.json` (the association marker: vault path,
per-repo subdir, memory dir, per-fact sync hashes). `push` populates the vault folder and
generates `memory.base` + `MEMORY.md`. If the repo is under git, ensure
`.claude/obsidian-sync.json` is gitignored (it holds a machine-local absolute path).

## Sync (every subsequent invocation)

```
python3 <skill>/sync.py status --repo <repo_root>
```

Returns per-fact and `claude_md` states:

- `push` — repo changed → `python3 <skill>/sync.py push --repo <repo_root>` (pushes all
  changed facts and regenerates the dashboard). `--only <slug>.md` for one fact.
- `pull` — vault changed → `pull`. New `*.md` notes the user created in the vault folder
  become new memory files (and get a `MEMORY.md` index line).
- `conflict` — **both** changed since last sync. `sync.py` refuses and prints `SKIP`. Never
  auto-resolve: read both sides, show the user a short diff, ask which wins (or hand-merge),
  then `push --force` / `pull --force` (optionally `--only`).
- `removed` — a previously-synced fact vanished from one side. Never auto-delete; ask,
  then remove the counterpart + its `MEMORY.md` line yourself if confirmed.

### Concurrency discipline (important — from the sync research)

iCloud has **no** conflict resolution, and desktop Obsidian will **silently overwrite** an
external write to a note that's currently **open in its editor**. So:

- Treat the repo/Mac side as the single writer; the phone is read-mostly for memory.
- Don't push a note the user has open in Obsidian; if unsure, sync when Obsidian isn't
  focused on that note.
- Never run a second sync engine (git, Obsidian Sync) on the same iCloud vault folder.
- Core **File Recovery** (per-device snapshots) + git history are the recovery layers.

## Organize (core Obsidian — auto-generated, no plugins)

`push` already writes the mobile-safe org kit: the `memory.base` (a Bases "By type" table,
core since Obsidian 1.9) and the `MEMORY.md` dashboard that embeds it and lists backlinks.
Caveat: **Bases YAML syntax is version-sensitive** — if the embedded view is empty, open
`memory.base` in the app and adjust the `file.inFolder(...)` filter; the dashboard note says
so. Only add extra views (by repo, recently-synced) after verifying syntax on the live app —
don't ship date-math filters blind.

## Summarize (core — you write it)

Core Obsidian can't summarize. But you already own the writes, so when the user wants a
summary, write a short prose rollup **into the `## Rollup` section of the vault `MEMORY.md`**
(or a `## Summary` you add): counts per type, the few most important facts, per-repo
one-liners. That covers "summarize" with zero plugins and nothing leaving the device.

## Analyse / cross-link (on request, or after a sync with real changes)

Judgment, not scripted. Glob the vault folder (and loosely, sibling notes one level up) for
notes on the same topic as the just-synced facts, and propose `[[wikilinks]]`. Show proposed
links before writing them (this edits notes beyond the mirror). Follow the `obsidian` skill's
linking/frontmatter conventions. Scope to the target folder unless asked to sweep wider.

If the user wants *semantic* cross-linking beyond string matches, the one community plugin
worth recommending is **Smart Connections** (local, keyless, on-device embeddings, runs on
iOS) — it degrades gracefully (notes stay plain markdown without it). Everything else
(Templater, Copilot, Omnisearch) is optional desktop polish; don't push it.

## Self-check

`python3 <skill>/sync.py selftest` round-trips init → push → phone-edit → pull →
new-vault-fact → conflict → force in a scratch dir. Re-run after editing `sync.py`.
