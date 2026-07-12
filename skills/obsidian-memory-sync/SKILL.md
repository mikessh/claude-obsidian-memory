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
  MEMORY.md       generated dashboard: rollup + ![[memory.base]] + outgoing links
```

`sync.py` flattens the repo schema (`name`/`description`/`metadata.type`) into vault
frontmatter (`type`, `repo`, `created`, `last_synced`, `tags: [memory, repo/<repo>]`) on
push and re-nests it on pull. Bodies are copied verbatim. This frontmatter is load-bearing:
Bases `groupBy`, core `[type:value]` search, and Graph color-groups all read it.

## Associate (first time for this repo)

The user gives a vault folder path (e.g. `~/vaults/personal/projects/foo`).

```
python3 <skill>/sync.py init --repo <repo_root> --memory <session-memory-dir> --vault <vault_path>
python3 <skill>/sync.py push --repo <repo_root>
```

`init` writes `<repo_root>/.claude/obsidian-sync.json` (the association marker: vault path,
per-repo subdir, memory dir, `hash_scheme`, per-fact sync hashes), **auto-gitignores it** (it
holds a machine-local absolute path), and **refuses to overwrite** an existing marker unless
you pass `--force` (which drops every recorded hash). `push` populates the vault folder and
generates `memory.base` + `MEMORY.md`. Then run `sync.py doctor --repo <repo_root>` to confirm
the vault is reachable (Full Disk Access) and the association is healthy.

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
- `not_in_index` / `indexed_but_missing` — `MEMORY.md` drifted from the files on disk. The
  fact still syncs (files are the truth); fix the index line.
- `hash_scheme_mismatch` — the marker was written by a different `sync.py`. It refuses to
  run rather than report every fact as a conflict; re-baseline once with `push --force`
  (repo authoritative) or `pull --force` (vault authoritative).

`--only` **narrows** scope to one fact; only `--force` overrides the direction guard.

When there are non-conflicting changes on *both* sides, `python3 <skill>/sync.py sync --repo
<repo_root>` does push + pull in one call (conflicts still skipped and reported).

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
core since Obsidian 1.9) and the `MEMORY.md` dashboard that embeds it and links out to every fact.
Caveat: **Bases YAML syntax is version-sensitive** — if the embedded view is empty, open
`memory.base` in the app and adjust the `file.inFolder(...)` filter; the dashboard note says
so. Only add extra views (by repo, recently-synced) after verifying syntax on the live app —
don't ship date-math filters blind.

## Summarize (core — you write it)

Core Obsidian can't summarize. But you already own the writes, so when the user wants a
summary, write a short prose rollup **into the `## Rollup` section of the vault `MEMORY.md`**
(or a `## Summary` you add): counts per type, the few most important facts, per-repo
one-liners. That covers "summarize" with zero plugins and nothing leaving the device.

## Compress (trim old/irrelevant memory + make it readable)

For "compress", "trim memory", "clean up the notes", or `/memory-compress`. Judgment — the
script only provides the safe, reversible `archive` primitive; deciding *what* to trim and
*how* to rewrite is yours.

1. Read every active fact note (`memory/*.md`, skip `_archive/`). Classify each:
   - **Trim** — superseded by a newer fact, a one-off that won't recur, or no longer true.
     Dates (`created`/`last_synced`) are a hint, not a rule; recency ≠ relevance.
   - **Merge** — two notes that are really one fact → combine into one, archive the other.
   - **Rewrite** — terse agent-shorthand → clean prose: a one-sentence summary line, then
     short readable paragraphs. Preserve **every** real fact and every `[[link]]` — compress
     phrasing, never information.
2. **Show the user the whole proposal and confirm before applying.** Never archive silently —
   same rule as the memory system's "never drop a capture".
3. Apply: rewrite bodies in the repo `memory/*.md` files; for each trim run
   `python3 <skill>/sync.py archive --repo <repo_root> --only <slug>.md` (moves the file to
   `memory/_archive/`, drops its `MEMORY.md` line, removes the vault mirror — fully reversible
   with `sync.py restore --repo <repo_root> --only <slug>.md`). Then `push` to re-sync.
4. Update the vault `MEMORY.md` `## Rollup` prose to reflect the compacted state.

Conservative by default (when unsure, keep + rewrite rather than archive); `/memory-compress
aggressive` leans toward trimming. This is the **per-repo** synced memory — the global
cross-repo memory is the `consolidate-memory` skill's domain; don't conflate them.

If the vault is under git, suggest committing it before a compress run so the whole thing is
revertable with one `git checkout`; `git diff` is also the cleanest way to review what changed.
The tool itself stays git-agnostic (undo is `restore` + `push --force`); a sandboxed/iCloud
vault may block git access anyway, so this is a user habit, not something to automate.

## Organize a whole vault — audit, Atlas, cross-links

For "organize my vault", "make an audit", "cross-link the projects", "map of content". These
operate **across all repos** mirrored into one vault (discovered under `~/vcs` `~/work`), not
just the current repo.

- `python3 <skill>/sync.py audit --repo <R>` — read-only vault report: per-repo CLAUDE.md size
  (flags unsectioned **walls**), fact counts, **orphan** notes, **dangling** links, the existing
  cross-repo link graph, project **clusters**, and cross-repo link **gaps**. Start here.
- `python3 <skill>/sync.py map --repo <R>` — generate the one-way **Atlas** in `<vault>/_maps/`:
  a Map of Content + a hub per cluster + a hub per repo (linking its condensed notes + siblings).
  Never synced back; the condensed per-repo notes stay untouched. Curate quality in two data
  files the generator reads (so regeneration preserves it): `_maps/clusters.json`
  (`{cluster: {desc, repos}}`) and `_maps/summaries.json` (`{repo: one-liner}`).
- `python3 <skill>/sync.py map --repo <R> --enrich` — dry-run the fact cross-linking; add
  `--apply` to write a `**Related projects:**` footer into each fact that names a sibling repo
  (idempotent, non-destructive; modifies repo memory, so `push` afterwards to mirror).
- **Unwrap a wall**: for a big `CLAUDE.md`, hand-write `<vault>/_maps/<repo> — guide.md` — a
  readable, sectioned, cross-linked digest. `map` links it from the repo hub and never
  overwrites it (that's the "condensed mirror + unwrapped guide" split).

The condensed layer (round-tripped `CLAUDE.md.md` + fact notes) is the source of truth and is
never reformatted; the Atlas is the human-readable presentation on top.

## Analyse / cross-link (on request, or after a sync with real changes)

Judgment, not scripted. Glob the vault folder (and loosely, sibling notes one level up) for
notes on the same topic as the just-synced facts, and propose `[[wikilinks]]`. Show proposed
links before writing them (this edits notes beyond the mirror). Follow the `obsidian` skill's
linking/frontmatter conventions. Scope to the target folder unless asked to sweep wider.

If the user wants *semantic* cross-linking beyond string matches, the one community plugin
worth recommending is **Smart Connections** (local, keyless, on-device embeddings, runs on
iOS) — it degrades gracefully (notes stay plain markdown without it). Everything else
(Templater, Copilot, Omnisearch) is optional desktop polish; don't push it.

## Troubleshooting — `sync.py doctor`

When a sync misbehaves, run `python3 <skill>/sync.py doctor --repo <repo_root>` first. It's a
read-only health check (it does perform the one safe recurring repair — gitignoring the
marker; pass `--no-fix` to skip) that reports every failure mode this plugin has hit, each
with its fix:

- **vault access — FAIL**: the vault is on iCloud and macOS **Full Disk Access** isn't granted
  to your terminal app. Grant it in *System Settings ▸ Privacy & Security ▸ Full Disk Access*,
  then restart the app. Until then the vault is unreadable and no sync can run — `sync.py`
  exits with this message rather than mis-classifying every note.
- **hash scheme — FAIL**: the marker was written by a different `sync.py` (e.g. a stale
  installed plugin cache). Re-baseline once: `push --force` (repo wins) or `pull --force`
  (vault wins). If the *installed* plugin is stale, `/plugin update claude-obsidian-memory`.
- **gitignore — WARN/auto-fixed**: the marker must never be committed.
- **memory dir — WARN**: `MEMORY.md` drifted from the files on disk (a fact isn't indexed, or
  an index line points at a missing file). Sync still works (files are the truth); fix the index.

## Self-check

`python3 <skill>/sync.py selftest` round-trips the whole surface in a scratch dir —
init → push → pull → `--only` scoping → conflict → force → archive → restore → sync — and
asserts sibling-metadata preservation, YAML-scalar round-tripping, and the hash-scheme guard.
Re-run after editing `sync.py`.
