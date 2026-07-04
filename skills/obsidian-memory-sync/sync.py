#!/usr/bin/env python3
"""Two-way sync between a repo's Claude memory and an Obsidian vault folder.

Model (per-fact, so Obsidian core features light up):
  repo side                              vault side  <vault_dir>/<repo>/
    memory/MEMORY.md      (index)   -->    MEMORY.md      (dashboard: rollup + base embed)
    memory/<slug>.md      (fact)    <-->   <slug>.md      (fact note, queryable frontmatter)
    CLAUDE.md                       <-->   CLAUDE.md.md   (repo-context mirror)
                                           memory.base    (generated Bases dashboard)

Why per-fact notes: Obsidian Bases (core since 1.9), Graph color-by-type,
Backlinks, and core `[type:value]` search all operate on individual notes'
frontmatter Properties. One concatenated note can't drive any of them.

Frontmatter transform (repo fact <-> vault fact):
  repo:   name / description / metadata.type            (the memory-system schema)
  vault:  type / repo / name / description / created /   (flattened + enriched so
          last_synced / tags: [memory, repo/<slug>]       Bases/Search/Graph work)
Body is copied verbatim. created is preserved across syncs; last_synced stamps push.

ponytail: conflicts (both sides changed since last sync) are never auto-merged —
reported per-file; caller decides and re-runs with --force. iCloud has no conflict
resolution and Obsidian won't hot-reload a note open in the editor, so this assumes
a single-writer discipline (see SKILL.md); File Recovery + git are the safety nets.

Usage:
  sync.py init   --repo DIR --memory DIR --vault DIR
  sync.py status --repo DIR [--memory DIR]
  sync.py push   --repo DIR [--memory DIR] [--only SLUG|claude_md] [--force]
  sync.py pull   --repo DIR [--memory DIR] [--only SLUG|claude_md] [--force]
  sync.py selftest
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

CONFIG_REL = Path(".claude/obsidian-sync.json")
INDEX_RE = re.compile(r"^- \[(?P<title>[^\]]+)\]\((?P<file>[^)]+)\)(?:\s+—\s+(?P<hook>.*))?")
TYPES = {"user", "feedback", "project", "reference"}


def sha(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "untitled"


def split_frontmatter(text):
    """Return (frontmatter_lines_str, body_str). Empty frontmatter if none."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[1:i]), "\n".join(lines[i + 1:]).strip("\n")
    return "", text.strip("\n")


def fm_get(fm, key):
    m = re.search(rf"(?m)^{re.escape(key)}:\s*(.*)$", fm)
    return m.group(1).strip() if m else ""


def fm_get_type(fm):
    # matches `metadata:\n  type: X` or a flat `type: X`
    m = re.search(r"(?m)^\s*type:\s*(\S+)\s*$", fm)
    return m.group(1).strip() if m else ""


# --- repo fact <-> parsed fields ---------------------------------------------

def parse_repo_fact(text, fallback_slug):
    fm, body = split_frontmatter(text)
    return {
        "name": fm_get(fm, "name") or fallback_slug,
        "description": fm_get(fm, "description"),
        "type": fm_get_type(fm),
        "body": body,
    }


def build_repo_fact(f):
    t = f["type"] if f["type"] in TYPES else "project"
    return (
        f"---\nname: {f['name']}\ndescription: {f['description']}\n"
        f"metadata:\n  type: {t}\n---\n\n{f['body'].strip()}\n"
    )


# --- vault fact <-> parsed fields --------------------------------------------

def build_vault_fact(f, repo, created, last_synced):
    t = f["type"] if f["type"] in TYPES else "project"
    return (
        "---\n"
        f"type: {t}\n"
        f"repo: {repo}\n"
        f"name: {f['name']}\n"
        f"description: {f['description']}\n"
        f"created: {created}\n"
        f"last_synced: {last_synced}\n"
        f"tags: [memory, repo/{repo}]\n"
        "---\n\n"
        f"{f['body'].strip()}\n"
    )


def parse_vault_fact(text, fallback_slug):
    fm, body = split_frontmatter(text)
    return {
        "name": fm_get(fm, "name") or fallback_slug,
        "description": fm_get(fm, "description"),
        "type": fm_get(fm, "type"),
        "created": fm_get(fm, "created"),
        "last_synced": fm_get(fm, "last_synced"),
        "body": body,
    }


def norm_claude_vault(text):
    """Vault CLAUDE.md mirror minus its injected callout — the repo-equivalent
    body, so both sides hash to the same thing when unchanged."""
    body = split_frontmatter(text)[1]
    return re.sub(r"(?m)^> \[!note\].*\n?\n?", "", body).strip()


def fact_canon(f):
    """Canonical content of a fact for change-detection: the fields that round-trip
    both ways (name/description/type/body) — NOT the vault-only dates, which change
    every push. So editing a note's description or type is detected, not just its body."""
    return "\n".join([f.get("name", ""), f.get("description", ""),
                      f.get("type", ""), f.get("body", "").strip()])


# --- config -------------------------------------------------------------------

def load_config(repo):
    path = repo / CONFIG_REL
    if not path.exists():
        sys.exit(f"no {CONFIG_REL} in {repo} — run `init` first")
    return json.loads(path.read_text())


def save_config(repo, cfg):
    path = repo / CONFIG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n")


def repo_slug(repo):
    return slugify(repo.resolve().name)


def vault_repo_dir(cfg):
    return Path(cfg["vault_dir"]) / cfg["repo_subdir"]


def vault_rel_folder(vault_dir):
    """Vault-root-relative path of vault_dir, for a Bases file.inFolder() filter.
    Finds the vault root by walking up to the dir containing `.obsidian`."""
    vault_dir = vault_dir.resolve()
    root = vault_dir
    while root != root.parent:
        if (root / ".obsidian").exists():
            try:
                return str(vault_dir.relative_to(root))
            except ValueError:
                break
        root = root.parent
    return vault_dir.name  # fallback: last path component


# --- index --------------------------------------------------------------------

def read_index(memory_dir):
    """Return list of (title, file, hook) from MEMORY.md, in order."""
    path = memory_dir / "MEMORY.md"
    out = []
    if path.exists():
        for line in path.read_text().splitlines():
            m = INDEX_RE.match(line)
            if m:
                out.append((m.group("title"), m.group("file"), m.group("hook") or ""))
    return out


def index_files(memory_dir):
    return {f for _t, f, _h in read_index(memory_dir)}


def append_index(memory_dir, title, fname, hook="added via vault sync"):
    path = memory_dir / "MEMORY.md"
    lines = path.read_text().splitlines() if path.exists() else []
    lines.append(f"- [{title}]({fname}) — {hook}")
    path.write_text("\n".join(lines) + "\n")


def remove_index_line(memory_dir, fname):
    path = memory_dir / "MEMORY.md"
    if not path.exists():
        return
    kept = [l for l in path.read_text().splitlines()
            if not ((m := INDEX_RE.match(l)) and m.group("file") == fname)]
    path.write_text("\n".join(kept) + ("\n" if kept else ""))


# --- state classification -----------------------------------------------------

def classify(repo_body, vault_body, rec):
    repo_changed = sha(repo_body) != rec.get("last_hash_repo", "")
    vault_changed = sha(vault_body) != rec.get("last_hash_vault", "")
    if not rec.get("last_hash_repo") and not rec.get("last_hash_vault"):
        return "init"
    if repo_changed and vault_changed:
        return "conflict"
    if repo_changed:
        return "push"
    if vault_changed:
        return "pull"
    return "no_change"


def gather(repo, memory_dir, cfg):
    """Collect per-fact and claude_md repo/vault bodies + records."""
    vdir = vault_repo_dir(cfg)
    items = {}

    # every real fact file is a sync unit — indexed or not (don't silently drop
    # an un-indexed memory file), plus any note created on the vault side.
    keys = set(index_files(memory_dir))
    keys |= {p.name for p in memory_dir.glob("*.md") if p.name != "MEMORY.md"}
    if vdir.exists():
        for vf in vdir.glob("*.md"):
            if vf.name in ("MEMORY.md", cfg["claude_md"]["vault_file"]):
                continue
            keys.add(vf.name)
    for fname in sorted(keys):
        rpath = memory_dir / fname
        vpath = vdir / fname
        repo_body = fact_canon(parse_repo_fact(rpath.read_text(), fname[:-3])) if rpath.exists() else ""
        vault_body = fact_canon(parse_vault_fact(vpath.read_text(), fname[:-3])) if vpath.exists() else ""
        items[fname] = {
            "kind": "fact", "repo_path": rpath, "vault_path": vpath,
            "repo_body": repo_body, "vault_body": vault_body,
            "rec": cfg["facts"].get(fname, {}),
        }

    cpath = repo / "CLAUDE.md"
    vpath = vdir / cfg["claude_md"]["vault_file"]
    repo_body = cpath.read_text() if cpath.exists() else ""
    vault_body = norm_claude_vault(vpath.read_text()) if vpath.exists() else ""
    items["claude_md"] = {
        "kind": "claude_md", "repo_path": cpath, "vault_path": vpath,
        "repo_body": repo_body, "vault_body": vault_body, "rec": cfg["claude_md"],
    }
    return items


# --- commands -----------------------------------------------------------------

def cmd_init(args):
    repo, memory, vault = Path(args.repo), Path(args.memory), Path(args.vault)
    slug = repo_slug(repo)
    (vault / slug).mkdir(parents=True, exist_ok=True)
    cfg = {
        "vault_dir": str(vault.resolve()),
        "repo_subdir": slug,
        "memory_dir": str(memory.resolve()),
        "repo_root": str(repo.resolve()),
        "linked_at": date.today().isoformat(),
        "claude_md": {"vault_file": "CLAUDE.md.md", "last_hash_repo": "", "last_hash_vault": ""},
        "facts": {},
    }
    save_config(repo, cfg)
    print(json.dumps({"config": str(repo / CONFIG_REL), "vault_repo_dir": str(vault / slug)}))


def cmd_status(args):
    repo = Path(args.repo)
    cfg = load_config(repo)
    memory_dir = Path(args.memory) if args.memory else Path(cfg["memory_dir"])
    items = gather(repo, memory_dir, cfg)
    result, removed = {}, []
    for key, it in items.items():
        if it["kind"] == "fact" and it["rec"]:
            if not it["repo_path"].exists():
                removed.append({"file": key, "gone_from": "repo"})
                continue
            if not it["vault_path"].exists():
                removed.append({"file": key, "gone_from": "vault"})
                continue
        result[key] = classify(it["repo_body"], it["vault_body"], it["rec"])
    out = {"facts": {k: v for k, v in result.items() if k != "claude_md"},
           "claude_md": result.get("claude_md", "no_change")}
    if removed:
        out["removed"] = removed
    print(json.dumps(out, indent=2))


def do_push(repo, memory_dir, cfg, key, it):
    if it["kind"] == "claude_md":
        it["vault_path"].parent.mkdir(parents=True, exist_ok=True)
        body = it["repo_body"].strip()
        it["vault_path"].write_text(
            "---\ntype: claude-md-mirror\n"
            f"repo: {cfg['repo_subdir']}\nlast_synced: {date.today().isoformat()}\n"
            "tags: [memory, claude-md]\n---\n\n"
            "> [!note] Mirror of this repo's CLAUDE.md. Edits here sync back on pull.\n\n"
            f"{body}\n"
        )
        h = sha(it["repo_body"])
        cfg["claude_md"]["last_hash_repo"] = h
        cfg["claude_md"]["last_hash_vault"] = h
        return
    f = parse_repo_fact(it["repo_path"].read_text(), key[:-3])
    created = date.today().isoformat()
    if it["vault_path"].exists():
        created = parse_vault_fact(it["vault_path"].read_text(), key[:-3]).get("created") or created
    it["vault_path"].parent.mkdir(parents=True, exist_ok=True)
    it["vault_path"].write_text(build_vault_fact(f, cfg["repo_subdir"], created, date.today().isoformat()))
    h = sha(fact_canon(f))
    cfg["facts"].setdefault(key, {})
    cfg["facts"][key].update(last_hash_repo=h, last_hash_vault=h)


def do_pull(repo, memory_dir, cfg, key, it):
    if it["kind"] == "claude_md":
        body = norm_claude_vault(it["vault_path"].read_text())
        (repo / "CLAUDE.md").write_text(body + "\n")
        h = sha(body)
        cfg["claude_md"]["last_hash_repo"] = h
        cfg["claude_md"]["last_hash_vault"] = h
        return
    v = parse_vault_fact(it["vault_path"].read_text(), key[:-3])
    is_new = not it["repo_path"].exists()
    it["repo_path"].parent.mkdir(parents=True, exist_ok=True)
    it["repo_path"].write_text(build_repo_fact(v))
    if is_new and key not in index_files(memory_dir):
        append_index(memory_dir, v["description"] or v["name"], key)
    h = sha(fact_canon(v))
    cfg["facts"].setdefault(key, {})
    cfg["facts"][key].update(last_hash_repo=h, last_hash_vault=h)


def run_direction(args, direction):
    repo = Path(args.repo)
    cfg = load_config(repo)
    memory_dir = Path(args.memory) if args.memory else Path(cfg["memory_dir"])
    items = gather(repo, memory_dir, cfg)
    fn = do_push if direction == "push" else do_pull
    act_states = ("init", "push") if direction == "push" else ("init", "pull")
    done = []
    for key, it in items.items():
        if args.only and args.only != key:
            continue
        state = classify(it["repo_body"], it["vault_body"], it["rec"])
        if state == "conflict" and not args.force:
            print(f"SKIP {key}: conflict — resolve or pass --force")
            continue
        # only write when this direction's SOURCE side actually has the change
        if not args.force and not args.only and state not in act_states:
            continue
        src_body = it["repo_body"] if direction == "push" else it["vault_body"]
        if not src_body.strip() and not args.force:
            continue  # e.g. a vault-only new fact has no repo body to push, and vice versa
        fn(repo, memory_dir, cfg, key, it)
        done.append(key)
    if direction == "push":
        regenerate_dashboard(repo, memory_dir, cfg)
    save_config(repo, cfg)
    print(f"{direction}ed: {', '.join(done) if done else '(nothing)'}")


def cmd_push(args):
    run_direction(args, "push")


def cmd_pull(args):
    run_direction(args, "pull")


def cmd_sync(args):
    """Both directions in one call: push repo-side changes, then pull vault-side
    changes. Conflicts are skipped (reported) in each pass, same as push/pull."""
    args.only = None
    run_direction(args, "push")
    run_direction(args, "pull")


def cmd_archive(args):
    """Reversibly trim a fact: move the repo file to memory/_archive/, drop its
    MEMORY.md line, remove the vault mirror, forget it in the marker. The archived
    copy is preserved (restore = move it back out of _archive/ and push). Used by
    the compress flow; the judgment of WHAT to archive is the skill's, not this."""
    repo = Path(args.repo)
    cfg = load_config(repo)
    memory_dir = Path(args.memory) if args.memory else Path(cfg["memory_dir"])
    slug = args.only
    src = memory_dir / slug
    if src.exists():
        (memory_dir / "_archive").mkdir(exist_ok=True)
        src.rename(memory_dir / "_archive" / slug)  # non-recursive glob won't remirror it
    remove_index_line(memory_dir, slug)
    vpath = vault_repo_dir(cfg) / slug
    if vpath.exists():
        vpath.unlink()
    cfg["facts"].pop(slug, None)
    save_config(repo, cfg)
    regenerate_dashboard(repo, memory_dir, cfg)
    print(f"archived {slug} -> memory/_archive/{slug}; removed vault mirror")


def cmd_restore(args):
    """Undo an archive: move memory/_archive/<slug> back into active memory,
    re-index it, and re-mirror to the vault."""
    repo = Path(args.repo)
    cfg = load_config(repo)
    memory_dir = Path(args.memory) if args.memory else Path(cfg["memory_dir"])
    slug = args.only
    src = memory_dir / "_archive" / slug
    if not src.exists():
        sys.exit(f"no archived memory/_archive/{slug} to restore")
    src.rename(memory_dir / slug)
    f = parse_repo_fact((memory_dir / slug).read_text(), slug[:-3])
    if slug not in index_files(memory_dir):
        append_index(memory_dir, f["description"] or f["name"], slug, "restored from archive")
    items = gather(repo, memory_dir, cfg)
    do_push(repo, memory_dir, cfg, slug, items[slug])
    regenerate_dashboard(repo, memory_dir, cfg)
    save_config(repo, cfg)
    print(f"restored {slug} from archive and re-synced")


# --- generated dashboard: memory.base + MEMORY.md rollup ----------------------

def regenerate_dashboard(repo, memory_dir, cfg):
    vdir = vault_repo_dir(cfg)
    vdir.mkdir(parents=True, exist_ok=True)
    rel = vault_rel_folder(vdir)

    # memory.base — ONE view (By type). groupBy is an OBJECT (property+direction),
    # not a string (verified against help.obsidian.md/bases/syntax). No date math
    # or view-level sort (both version-sensitive); folder-scoped filter.
    base = (
        "filters:\n"
        "  and:\n"
        f'    - file.inFolder("{rel}")\n'
        '    - note.type != ""\n'
        "views:\n"
        "  - type: table\n"
        "    name: By type\n"
        "    groupBy:\n"
        "      property: note.type\n"
        "      direction: ASC\n"
        "    order:\n"
        "      - file.name\n"
        "      - note.description\n"
        "      - note.last_synced\n"
    )
    (vdir / "memory.base").write_text(base)

    counts = {t: 0 for t in sorted(TYPES)}
    rows = []
    for vf in sorted(vdir.glob("*.md")):
        if vf.name in ("MEMORY.md", cfg["claude_md"]["vault_file"]):
            continue
        v = parse_vault_fact(vf.read_text(), vf.stem)
        if v["type"] in counts:
            counts[v["type"]] += 1
        rows.append((v.get("last_synced", ""), v["type"], vf.stem, v["description"]))
    rows.sort(reverse=True)
    total = sum(counts.values())

    lines = [
        "---", "type: memory-dashboard", f"repo: {cfg['repo_subdir']}",
        f"last_synced: {date.today().isoformat()}", "tags: [memory, dashboard]", "---", "",
        f"# Claude memory — {cfg['repo_subdir']}", "",
        "> [!info] Auto-generated dashboard. The rollup and embedded Base below are",
        "> regenerated on every push. Edit fact notes directly — those sync back.", "",
        "## Rollup", "",
        f"- **{total}** facts: " + (", ".join(f"{n} {t}" for t, n in counts.items() if n) or "none yet"),
        "", "## Dashboard (Bases)", "",
        "![[memory.base]]", "",
        "> If the embedded view is empty, open `memory.base` and adjust the filter —",
        f'> Bases syntax evolves; the intended scope is `file.inFolder("{rel}")` + has `type`.',
        "", "## Recently synced", "",
    ]
    for ls, t, stem, desc in rows[:15]:
        lines.append(f"- `{ls}` · **{t}** · [[{stem}]]" + (f" — {desc}" if desc else ""))
    lines += ["", "## All facts", ""]
    for _ls, _t, stem, _d in sorted(rows, key=lambda r: r[2]):
        lines.append(f"- [[{stem}]]")
    (vdir / "MEMORY.md").write_text("\n".join(lines) + "\n")


# --- selftest -----------------------------------------------------------------

def selftest():
    import contextlib
    import io
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    try:
        repo, memory, vault = tmp / "repo", tmp / "repo/memory", tmp / "vault"
        memory.mkdir(parents=True)
        (vault / ".obsidian").mkdir(parents=True)  # so vault_rel_folder resolves
        (repo / "CLAUDE.md").write_text("# Test project\nSome context.\n")
        (memory / "MEMORY.md").write_text("- [User is a tester](user_role.md) — who\n")
        (memory / "user_role.md").write_text(
            "---\nname: user_role\ndescription: User is a tester\nmetadata:\n  type: user\n---\n\nUser is a tester.\n")

        class A:
            pass
        a = A(); a.repo = str(repo); a.memory = str(memory); a.vault = str(vault)
        a.only = None; a.force = False
        cmd_init(a)
        cfg = load_config(repo)
        vdir = vault / cfg["repo_subdir"]

        # an un-indexed fact file must still be mirrored (no silent drop)
        (memory / "orphan.md").write_text(
            "---\nname: orphan\ndescription: not in the index\nmetadata:\n  type: project\n---\n\nOrphan body.\n")

        cmd_push(a)
        assert (vdir / "orphan.md").exists(), "un-indexed memory file still mirrored"
        vf = vdir / "user_role.md"
        text = vf.read_text()
        assert "type: user" in text and "repo: repo" in text, "vault frontmatter enriched"
        assert "User is a tester." in text
        assert (vdir / "memory.base").exists(), "base generated"
        assert "![[memory.base]]" in (vdir / "MEMORY.md").read_text(), "dashboard embeds base"
        assert (vdir / "CLAUDE.md.md").exists(), "claude_md mirrored"

        vf.write_text(text.replace("User is a tester.\n", "User is a tester. Edited on phone.\n"))
        cmd_pull(a)
        rt = (memory / "user_role.md").read_text()
        assert "Edited on phone." in rt, "vault edit pulled into repo fact"
        assert "metadata:\n  type: user" in rt, "repo nested type preserved"

        (vdir / "new-fact.md").write_text(
            "---\ntype: reference\nrepo: repo\nname: new-fact\ndescription: A new ref\n"
            "created: 2026-07-04\nlast_synced: 2026-07-04\ntags: [memory, repo/repo]\n---\n\nSee the docs.\n")
        cmd_pull(a)
        assert (memory / "new-fact.md").exists(), "new vault note -> new repo fact"
        assert "new-fact.md" in (memory / "MEMORY.md").read_text(), "index updated"

        (memory / "user_role.md").write_text(build_repo_fact(
            {"name": "user_role", "description": "User is a tester", "type": "user", "body": "Repo side."}))
        vf.write_text(vf.read_text().replace("Edited on phone.", "Vault side."))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_push(a)
        assert "conflict" in buf.getvalue(), "conflict detected and skipped"

        a.force = True
        cmd_push(a)
        a.force = False
        assert "Repo side." in (vdir / "user_role.md").read_text(), "force push wins"

        # archive (compress) — reversible trim
        a.only = "orphan.md"
        cmd_archive(a)
        assert not (memory / "orphan.md").exists(), "archived file left active memory"
        assert (memory / "_archive" / "orphan.md").exists(), "archived copy preserved"
        assert not (vdir / "orphan.md").exists(), "vault mirror removed on archive"
        assert "orphan.md" not in load_config(repo)["facts"], "marker forgot archived fact"

        # restore — undo the archive
        cmd_restore(a)
        a.only = None
        assert (memory / "orphan.md").exists(), "restored file back in active memory"
        assert not (memory / "_archive" / "orphan.md").exists(), "archive copy moved out"
        assert (vdir / "orphan.md").exists(), "vault mirror re-created on restore"
        assert "orphan.md" in load_config(repo)["facts"], "marker knows restored fact"

        # sync (both directions) — leaves everything no_change afterward
        cmd_sync(a)
        st = {**gather(repo, memory, load_config(repo))}
        for k, it in st.items():
            assert classify(it["repo_body"], it["vault_body"], it["rec"]) in ("no_change", "init"), \
                f"{k} not settled after sync"

        # frontmatter-only edit (description) must be detected — used to hash body only
        ur = memory / "user_role.md"
        ur.write_text(ur.read_text().replace("description: User is a tester",
                                             "description: User is a QA tester"))
        rec = gather(repo, memory, load_config(repo))["user_role.md"]
        assert classify(rec["repo_body"], rec["vault_body"], rec["rec"]) == "push", \
            "frontmatter-only (description) change detected"
        cmd_push(a)
        assert "QA tester" in (vdir / "user_role.md").read_text(), "description edit reached vault"

        print("selftest OK")
    finally:
        shutil.rmtree(tmp)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--memory", required=True)
    sp.add_argument("--vault", required=True)
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("status")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--memory")
    sp.set_defaults(func=cmd_status)

    for name, fn in (("push", cmd_push), ("pull", cmd_pull)):
        sp = sub.add_parser(name)
        sp.add_argument("--repo", required=True)
        sp.add_argument("--memory")
        sp.add_argument("--only", help="a single fact filename (e.g. foo.md) or 'claude_md'")
        sp.add_argument("--force", action="store_true")
        sp.set_defaults(func=fn)

    sp = sub.add_parser("sync")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--memory")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_sync)

    for name, fn in (("archive", cmd_archive), ("restore", cmd_restore)):
        sp = sub.add_parser(name)
        sp.add_argument("--repo", required=True)
        sp.add_argument("--memory")
        sp.add_argument("--only", required=True, help="fact filename, e.g. foo.md")
        sp.set_defaults(func=fn)

    sub.add_parser("selftest").set_defaults(func=lambda args: selftest())
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
