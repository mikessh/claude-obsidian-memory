#!/usr/bin/env python3
"""Two-way sync between a repo's Claude memory (MEMORY.md + memory/*.md,
plus CLAUDE.md) and two mirror notes in an Obsidian vault folder.

ponytail: conflicts (both sides changed since last sync) are never
auto-merged — this script reports them and the caller (SKILL.md flow)
must ask the user which side wins, then call push/pull with --force.
Section-level auto-merge would be the upgrade if whole-note conflicts
turn out to be too coarse in practice.

Usage:
  sync.py init   --repo DIR --memory DIR --vault DIR
  sync.py status --repo DIR [--memory DIR]
  sync.py push   --repo DIR [--memory DIR] --note {memory,claude_md} [--force]
  sync.py pull   --repo DIR [--memory DIR] --note {memory,claude_md} [--force]
  sync.py selftest
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

CONFIG_REL = Path(".claude/obsidian-sync.json")
INDEX_RE = re.compile(r"^- \[(?P<title>[^\]]+)\]\((?P<file>[^)]+)\)")
SECTION_RE = re.compile(r"^## (?P<title>.+?)(?: <!-- file: (?P<file>[^ ]+) -->)?\s*$")


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def split_frontmatter(text):
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[1:i]), "\n".join(lines[i + 1:]).strip("\n")
    return "", text.strip("\n")


def load_config(repo):
    path = repo / CONFIG_REL
    if not path.exists():
        sys.exit(f"no {CONFIG_REL} in {repo} — run `init` first")
    return json.loads(path.read_text())


def save_config(repo, cfg):
    path = repo / CONFIG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n")


def repo_name(repo):
    return repo.resolve().name


def build_memory_body(memory_dir):
    """Concatenate MEMORY.md-indexed files into one mirror body. Returns
    (body_text, index_entries) where index_entries is [(title, file), ...]."""
    index_path = memory_dir / "MEMORY.md"
    entries = []
    if index_path.exists():
        for line in index_path.read_text().splitlines():
            m = INDEX_RE.match(line)
            if m:
                entries.append((m.group("title"), m.group("file")))
    parts = []
    for title, fname in entries:
        fpath = memory_dir / fname
        if not fpath.exists():
            continue
        _, body = split_frontmatter(fpath.read_text())
        parts.append(f"## {title} <!-- file: {fname} -->\n\n{body}\n")
    return "\n".join(parts), entries


def wrap_note(body, repo, note_type, extra_note=""):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fm = f"---\nsource_repo: {repo.resolve()}\ntype: {note_type}\nsynced: {now}\n---\n"
    return fm + (f"\n{extra_note}\n" if extra_note else "") + "\n" + body


MEMORY_NOTE_HEADER = (
    "> [!note] Auto-generated mirror of Claude Code's private memory for this repo.\n"
    "> Edit content below each heading freely — sync pulls edits back into the repo.\n"
    "> Don't rename `## ` headings or remove the `<!-- file: -->` markers, or the\n"
    "> mapping back to the source file breaks.\n"
)


def cmd_init(args):
    repo, memory, vault = Path(args.repo), Path(args.memory), Path(args.vault)
    vault.mkdir(parents=True, exist_ok=True)
    name = repo_name(repo)
    cfg = {
        "vault_dir": str(vault.resolve()),
        "memory_dir": str(memory.resolve()),
        "linked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "notes": {
            "memory": {"vault_file": f"{name}-claude-memory.md", "last_hash_repo": "", "last_hash_vault": ""},
            "claude_md": {"vault_file": f"{name}-CLAUDE.md", "last_hash_repo": "", "last_hash_vault": ""},
        },
    }
    save_config(repo, cfg)
    print(json.dumps({"config": str(repo / CONFIG_REL), "vault_dir": str(vault.resolve())}))


def note_state(repo_content, vault_content, last_repo_hash, last_vault_hash):
    repo_changed = sha(repo_content) != last_repo_hash
    vault_changed = sha(vault_content) != last_vault_hash
    if not last_repo_hash and not last_vault_hash:
        return "init"
    if repo_changed and vault_changed:
        return "conflict"
    if repo_changed:
        return "push"
    if vault_changed:
        return "pull"
    return "no_change"


def current_bodies(repo, memory_dir, cfg):
    vault = Path(cfg["vault_dir"])
    claude_md = repo / "CLAUDE.md"
    repo_claude_md = claude_md.read_text() if claude_md.exists() else ""
    memory_body, _entries = build_memory_body(memory_dir)

    vault_memory_file = vault / cfg["notes"]["memory"]["vault_file"]
    vault_claude_file = vault / cfg["notes"]["claude_md"]["vault_file"]
    _, vault_memory_body = split_frontmatter(vault_memory_file.read_text()) if vault_memory_file.exists() else ("", "")
    vault_memory_body = vault_memory_body.replace(MEMORY_NOTE_HEADER, "").strip()
    _, vault_claude_body = split_frontmatter(vault_claude_file.read_text()) if vault_claude_file.exists() else ("", "")

    return {
        "memory": (memory_body.strip(), vault_memory_body.strip()),
        "claude_md": (repo_claude_md.strip(), vault_claude_body.strip()),
    }


def cmd_status(args):
    repo = Path(args.repo)
    cfg = load_config(repo)
    memory_dir = Path(args.memory) if args.memory else Path(cfg["memory_dir"])
    bodies = current_bodies(repo, memory_dir, cfg)

    result = {}
    for note in ("memory", "claude_md"):
        repo_body, vault_body = bodies[note]
        n = cfg["notes"][note]
        result[note] = {
            "state": note_state(repo_body, vault_body, n["last_hash_repo"], n["last_hash_vault"]),
            "vault_file": n["vault_file"],
        }

    # flag sections present in last-synced memory index but missing from the
    # current vault body (candidate deletions) — never auto-deleted.
    vault_memory_file = Path(cfg["vault_dir"]) / cfg["notes"]["memory"]["vault_file"]
    if vault_memory_file.exists():
        _, vbody = split_frontmatter(vault_memory_file.read_text())
        present_files = set(m.group("file") for line in vbody.splitlines()
                             if line.startswith("## ") for m in [SECTION_RE.match(line)] if m and m.group("file"))
        _, entries = build_memory_body(memory_dir)
        removed = [fname for _title, fname in entries if fname not in present_files]
        if removed:
            result["removed_in_vault"] = removed

    print(json.dumps(result, indent=2))


def apply_push(repo, memory_dir, cfg, note):
    vault = Path(cfg["vault_dir"])
    bodies = current_bodies(repo, memory_dir, cfg)
    repo_body, _ = bodies[note]
    vault_file = vault / cfg["notes"][note]["vault_file"]
    if note == "memory":
        text = wrap_note(MEMORY_NOTE_HEADER + "\n" + repo_body, repo, "claude-memory-mirror")
    else:
        text = wrap_note(repo_body, repo, "claude-claude-md-mirror")
    vault_file.write_text(text)
    h = sha(repo_body)
    cfg["notes"][note]["last_hash_repo"] = h
    cfg["notes"][note]["last_hash_vault"] = h


def apply_pull(repo, memory_dir, cfg, note):
    vault = Path(cfg["vault_dir"])
    vault_file = vault / cfg["notes"][note]["vault_file"]
    _, vault_body = split_frontmatter(vault_file.read_text())
    if note == "claude_md":
        body = vault_body.strip() + "\n"
        (repo / "CLAUDE.md").write_text(body)
        h = sha(vault_body.strip())
        cfg["notes"][note]["last_hash_repo"] = h
        cfg["notes"][note]["last_hash_vault"] = h
        return

    body = vault_body.replace(MEMORY_NOTE_HEADER, "").strip()
    sections = re.split(r"(?m)^(?=## )", body)
    index_path = memory_dir / "MEMORY.md"
    index_lines = index_path.read_text().splitlines() if index_path.exists() else []
    known_files = {m.group("file") for l in index_lines if (m := INDEX_RE.match(l))}

    new_index_entries = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        header, _, rest = section.partition("\n")
        m = SECTION_RE.match(header)
        if not m:
            continue
        title, fname = m.group("title"), m.group("file")
        new_body = rest.strip()
        if fname:
            fpath = memory_dir / fname
            frontmatter = ""
            if fpath.exists():
                frontmatter, _old_body = split_frontmatter(fpath.read_text())
            if not frontmatter:
                slug = fname[:-3] if fname.endswith(".md") else fname
                frontmatter = f"name: {slug}\ndescription: {title}\nmetadata:\n  type: project"
                new_index_entries.append((title, fname))
            fpath.write_text(f"---\n{frontmatter}\n---\n\n{new_body}\n")
        else:
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "untitled"
            fname = f"{slug}.md"
            frontmatter = f"name: {slug}\ndescription: {title}\nmetadata:\n  type: project"
            (memory_dir / fname).write_text(f"---\n{frontmatter}\n---\n\n{new_body}\n")
            new_index_entries.append((title, fname))

    if new_index_entries:
        for title, fname in new_index_entries:
            if fname not in known_files:
                index_lines.append(f"- [{title}]({fname}) — added via vault sync")
        index_path.write_text("\n".join(index_lines) + "\n")

    memory_body, _ = build_memory_body(memory_dir)
    h_repo = sha(memory_body.strip())
    h_vault = sha(body)
    cfg["notes"][note]["last_hash_repo"] = h_repo
    cfg["notes"][note]["last_hash_vault"] = h_vault


def cmd_push(args):
    repo = Path(args.repo)
    cfg = load_config(repo)
    memory_dir = Path(args.memory) if args.memory else Path(cfg["memory_dir"])
    bodies = current_bodies(repo, memory_dir, cfg)
    repo_body, vault_body = bodies[args.note]
    n = cfg["notes"][args.note]
    state = note_state(repo_body, vault_body, n["last_hash_repo"], n["last_hash_vault"])
    if state == "conflict" and not args.force:
        sys.exit(f"{args.note}: conflict — resolve manually or pass --force to overwrite vault with repo content")
    apply_push(repo, memory_dir, cfg, args.note)
    save_config(repo, cfg)
    print(f"pushed {args.note} -> {cfg['notes'][args.note]['vault_file']}")


def cmd_pull(args):
    repo = Path(args.repo)
    cfg = load_config(repo)
    memory_dir = Path(args.memory) if args.memory else Path(cfg["memory_dir"])
    bodies = current_bodies(repo, memory_dir, cfg)
    repo_body, vault_body = bodies[args.note]
    n = cfg["notes"][args.note]
    state = note_state(repo_body, vault_body, n["last_hash_repo"], n["last_hash_vault"])
    if state == "conflict" and not args.force:
        sys.exit(f"{args.note}: conflict — resolve manually or pass --force to overwrite repo with vault content")
    apply_pull(repo, memory_dir, cfg, args.note)
    save_config(repo, cfg)
    print(f"pulled {args.note} <- {cfg['notes'][args.note]['vault_file']}")


def selftest():
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    try:
        repo, memory, vault = tmp / "repo", tmp / "repo/memory", tmp / "vault"
        memory.mkdir(parents=True)
        (repo / "CLAUDE.md").write_text("# Test project\nSome content.\n")
        (memory / "MEMORY.md").write_text("- [User role](user_role.md) — test\n")
        (memory / "user_role.md").write_text("---\nname: user_role\ndescription: test\nmetadata:\n  type: user\n---\n\nUser is a tester.\n")

        class A:
            pass
        a = A(); a.repo, a.memory, a.vault = str(repo), str(memory), str(vault)
        cmd_init(a)
        cfg = load_config(repo)

        a.note = "memory"; a.force = False
        cmd_push(a)
        a.note = "claude_md"
        cmd_push(a)

        vault_memory_file = vault / cfg["notes"]["memory"]["vault_file"]
        text = vault_memory_file.read_text()
        assert "User is a tester." in text
        edited = text.replace("User is a tester.", "User is a tester. Edited on phone.")
        vault_memory_file.write_text(edited)

        a.note = "memory"
        cmd_pull(a)
        new_body = (memory / "user_role.md").read_text()
        assert "Edited on phone." in new_body
        assert "name: user_role" in new_body

        vault_new_section = vault_memory_file.read_text() + "\n## Brand new topic\n\nCaptured on phone.\n"
        vault_memory_file.write_text(vault_new_section)
        cmd_pull(a)
        assert (memory / "brand-new-topic.md").exists()
        assert "added via vault sync" in (memory / "MEMORY.md").read_text()

        print("selftest OK")
    finally:
        shutil.rmtree(tmp)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("init",):
        sp = sub.add_parser(name)
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
        sp.add_argument("--note", required=True, choices=["memory", "claude_md"])
        sp.add_argument("--force", action="store_true")
        sp.set_defaults(func=fn)

    sub.add_parser("selftest").set_defaults(func=lambda args: selftest())

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
