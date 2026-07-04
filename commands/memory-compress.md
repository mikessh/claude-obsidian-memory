---
description: Trim stale/irrelevant memory and rewrite terse fact notes into readable prose, then re-sync
argument-hint: "[aggressive]"
---

Run the `obsidian-memory-sync` skill's **Compress** flow for the current repo:

1. Read every active fact note (`memory/*.md`, skip `_archive/`). For each, judge:
   - **Trim** → stale (superseded by a newer fact), one-off that won't recur, or no longer
     true. Use `created`/`last_synced` dates as a *hint*, not a rule.
   - **Merge** → two notes that are really one fact; combine, archive the redundant one.
   - **Rewrite** → terse agent-shorthand → clean prose (a one-sentence summary line, then
     short readable paragraphs). Preserve **every** real fact and every `[[link]]`.
2. Show the user the full proposal (archive list, merges, rewrites) and get confirmation
   BEFORE applying anything. Never archive silently.
3. Apply: rewrite bodies in the repo memory files; `sync.py archive --only <slug>.md` for
   each trim (reversible — moves to `memory/_archive/`); then `push` to re-sync.
4. Update the prose `## Rollup` in the vault `MEMORY.md` to reflect the compacted state.

`aggressive` in $ARGUMENTS = lean toward trimming more; default is conservative (when unsure,
keep and rewrite rather than archive). This is the per-repo synced memory; the global
cross-repo memory is the `consolidate-memory` skill's job — don't touch that here.
