# Obsidian interop — what makes the memory notes better to organize, view, and summarize

Grounded in a web-sourced sweep of the current (2025–2026) Obsidian ecosystem, filtered
against the constraint that these vaults are **core-only and must work on iOS** (iCloud-synced
plain markdown, no MCP / no REST). Bottom line: **the constraint is a good fit** — Obsidian 1.9
moved a real database engine (**Bases**) into core, so the one feature that used to require a
community plugin (filterable views over frontmatter) is now core and mobile.

The plugin **generates** everything in Tier 1. Tier 2 is opt-in desktop polish you can add by
hand; the notes stay plain, readable markdown without any of it.

## Tier 1 — core Obsidian only (what the plugin sets up for you)

| Feature | Kind | Mobile | What it does for the memory |
|---|---|---|---|
| **Bases** (`memory.base`) | core ≥1.9 | yes | Live table grouped by `type` (user/feedback/project/reference). The primary dashboard. Generated on every push. |
| **Frontmatter Properties** | core | yes | `type`, `repo`, `created`, `last_synced`, `tags` on every fact note — the schema every view below reads. Generated. |
| **Core Search** | core | yes | `path:` + `[type:value]` + `tag:#repo/<repo>` filter facts precisely. Embed a `` ```query `` block in a note for a live saved search. |
| **Backlinks / Outgoing Links panes** | core | yes | The daily-driver "what references this fact" view on mobile — more legible than the graph on a phone. Works because facts carry `[[wikilinks]]`. |
| **Tags pane** | core | yes | `#repo/<repo>` + `#memory` give a zero-setup per-repo index. Generated into each note. |
| **Graph view** | core | yes | See fact clusters. Optionally add color-groups by `["type":user]` etc. (marginal — the Bases "By type" view already does this more legibly, so it's a one-time nicety, not a must). |
| **File Recovery** | core | yes | Per-device snapshot history — the recovery net if a sync clobbers a note. Enable it. |

**Summarize is also core — the agent writes it.** Obsidian can't summarize without an LLM
plugin, but Claude already owns the writes, so the skill writes a prose rollup into the vault
`MEMORY.md`. Zero plugins, nothing leaves the device. This beats reaching for a Tier-2 AI
plugin for the summarize use case.

### Caveat on Bases syntax

Bases YAML is version-sensitive. The generated `memory.base` ships **one** view (`By type`,
`groupBy` only — no date math, which is the part whose syntax changed across 1.9.x). If the
embedded view is empty on your install, open `memory.base` and fix the `file.inFolder(...)`
filter. Add "by repo" / "recently synced" views yourself once you've confirmed syntax in the
app — don't trust a hand-authored date filter unverified.

## Tier 2 — community plugins, opt-in, desktop (each breaks core-only)

Ranked by unique value for *this* job. All degrade gracefully (remove the plugin, the notes
still read fine).

| Rank | Plugin | Uniquely adds | Mobile | Verdict |
|---|---|---|---|---|
| 1 | **Smart Connections** | Local, **keyless**, on-device embeddings → suggests semantically-related facts to `[[link]]` that string-match can't find | yes (iOS) | The one worth the exception if you want smarter cross-linking. Writes a `.smart-env` index that iCloud will sync. |
| 2 | **Templater** | Folder template auto-applies a `type:`-prompting frontmatter scaffold to any note *created by hand* in the folder | yes | Only if you'll hand-create memory notes on mobile. The skill already formats notes on the repo side, so usually unnecessary. |
| 3 | **Copilot** (logancyang) | True LLM Q&A/summaries *inside* the vault ("summarize all feedback for this repo") | yes | Only if you want in-vault Q&A and accept an API key. Otherwise let Claude summarize on the repo side. |
| 4 | **Extended Graph** | Color/filter the graph natively by the `type` property | **no (desktop-only)** | Nice on desktop; can't be the mobile answer, so low priority. |

**Not worth it here:** Dataview (redundant with core Bases now, heavier on mobile),
Omnisearch (BM25 fuzzy search is overkill for a few dozen terse fact-notes core Search
already finds), Khoj / Smart Second Brain (need a running backend; Smart2Brain is desktop-only
and unmaintained), Kanban plugins (need a newer view API; core Cards-grouped-by-type substitutes).

**If you adopt exactly one:** Smart Connections.

## Sync / storage reality (shapes the plugin's single-writer design)

- **iCloud has no conflict resolution.** Concurrent or offline edits to the same note can
  silently overwrite with no recoverable copy on the phone.
- **Obsidian won't hot-reload a note that's open in its editor** — your next in-app save
  overwrites an external write to that note (lost update). On iOS the file watcher is
  unreliable regardless (sandboxing); external changes often need a close/reopen.
- **Never run two sync engines** (git or Obsidian Sync) on the same iCloud vault folder — they
  fight and cause confusing deletions.

→ The plugin assumes **single-writer** (Mac/Claude writes, phone reads-mostly), refuses to
auto-merge conflicts, and leans on core File Recovery + git as recovery layers. If you ever
want real cross-device version history and conflict handling, **Obsidian Sync** (first-party,
*not* a community plugin, so it doesn't break core-only) is the upgrade — but it replaces
iCloud, it's paid, and you must not run both.

---
*Sources: web-verified capability sweep across Bases, Dataview, Smart Connections, Copilot,
Templater, QuickAdd, Advanced/Actions URI, core Search/Graph/Canvas/File Recovery, and
iCloud/Obsidian Sync behavior, adversarially critiqued for accuracy and over-engineering.*
