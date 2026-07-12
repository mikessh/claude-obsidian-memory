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
  repo:   name / description / metadata.type (+ any sibling metadata keys, preserved)
  vault:  type / repo / name / description / created / last_synced / tags
Body is copied verbatim. `created` is preserved across syncs; `last_synced` stamps push.
A pull NEVER drops sibling repo frontmatter (node_type, originSessionId, ...).

ponytail: conflicts (both sides changed since last sync) are never auto-merged —
reported per-file; caller decides and re-runs with --force. `--only` NARROWS scope,
it does not force. iCloud has no conflict resolution and Obsidian won't hot-reload a
note open in the editor, so this assumes a single-writer discipline (see SKILL.md);
File Recovery + git are the safety nets.

The marker records `hash_scheme`; a marker written by a different scheme is refused
with instructions rather than silently reporting every fact as a conflict.

`init` auto-gitignores the marker (it holds a machine-local path). `doctor` health-checks
an association and reports every recurring failure mode (Full Disk Access block, stale
hash scheme, index drift, un-gitignored marker) with its fix.

Usage:
  sync.py init    --repo DIR --memory DIR --vault DIR [--force]
  sync.py doctor  --repo DIR [--memory DIR] [--no-fix]
  sync.py status  --repo DIR [--memory DIR]
  sync.py push    --repo DIR [--memory DIR] [--only SLUG|claude_md] [--force]
  sync.py pull    --repo DIR [--memory DIR] [--only SLUG|claude_md] [--force]
  sync.py sync    --repo DIR [--memory DIR] [--force]      # push then pull
  sync.py archive --repo DIR [--memory DIR] --only SLUG    # reversible trim
  sync.py restore --repo DIR [--memory DIR] --only SLUG    # undo an archive
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
HASH_SCHEME = 2  # bump whenever fact_canon()/hashing changes
BANNER = "<!--obsidian-memory-banner-->"


def sha(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "untitled"


# --- YAML scalar read/write (stdlib only; json is a valid YAML double-quoted style) ---

def _q(s):
    """Emit a YAML-safe scalar: quote only when a plain scalar would be ambiguous."""
    if s == "" or s[0] in "[{-\"'>|*&!%@`?," or ": " in s or " #" in s or s.endswith(":") or "\n" in s:
        return json.dumps(s)
    return s


def _unq(s):
    """Read a scalar back: strip one matching quote pair, unescape."""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        if s[0] == '"':
            try:
                return json.loads(s)
            except Exception:
                return s[1:-1]
        return s[1:-1].replace("''", "'")
    return s


def split_frontmatter(text):
    """Return (frontmatter_lines_str, body_str). Empty frontmatter if none."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[1:i]), "\n".join(lines[i + 1:]).strip("\n")
    return "", text.strip("\n")


def _is_continuation(line):
    """A folded-scalar continuation: indented, and not itself a `key: value` pair."""
    return line[:1] in (" ", "\t") and not re.match(r"^\s+\S+:(\s|$)", line)


def fm_get(fm, key):
    """Top-level scalar by key, joining folded continuation lines."""
    lines = fm.split("\n")
    pat = re.compile(rf"^{re.escape(key)}:\s*(.*)$")
    for i, l in enumerate(lines):
        m = pat.match(l)
        if not m:
            continue
        val = m.group(1)
        j = i + 1
        while j < len(lines) and _is_continuation(lines[j]):
            val += " " + lines[j].strip()
            j += 1
        return _unq(val)
    return ""


def fm_get_type(fm):
    """`metadata:\\n  type: X` (repo) or a flat `type: X` (vault)."""
    m = re.search(r"(?m)^\s*type:\s*(.*)$", fm)
    return _unq(m.group(1)) if m else ""


def eff_type(f):
    """The type actually written AND hashed — never coerced away from the source
    value, so `status` can't report a phantom change (only a missing type defaults)."""
    return f["type"] or "project"


# --- repo fact <-> parsed fields ---------------------------------------------

def parse_repo_fact(text, fallback_slug):
    fm, body = split_frontmatter(text)
    return {
        "name": fm_get(fm, "name") or fallback_slug,
        "description": fm_get(fm, "description"),
        "type": fm_get_type(fm),
        "body": body,
    }


def _replace_scalar(lines, key, value, nested=False):
    """Replace `key: ...` (+ its folded continuations) in-place. Returns True if found."""
    pat = re.compile(rf"^(\s*){re.escape(key)}:\s*(.*)$") if nested else re.compile(rf"^{re.escape(key)}:\s*(.*)$")
    for i, l in enumerate(lines):
        m = pat.match(l)
        if not m:
            continue
        indent = m.group(1) if nested else ""
        j = i + 1
        while j < len(lines) and _is_continuation(lines[j]):
            j += 1
        lines[i:j] = [f"{indent}{key}: {value}"]
        return True
    return False


def build_repo_fact(f, existing=None):
    """Rewrite a repo fact, PRESERVING every frontmatter key we don't own
    (node_type, originSessionId, ...). Only name/description/metadata.type change."""
    t = eff_type(f)
    if existing:
        fm, _ = split_frontmatter(existing)
        if fm.strip():
            lines = fm.split("\n")
            if not _replace_scalar(lines, "name", _q(f["name"])):
                lines.insert(0, f"name: {_q(f['name'])}")
            if not _replace_scalar(lines, "description", _q(f["description"])):
                lines.insert(1, f"description: {_q(f['description'])}")
            if not _replace_scalar(lines, "type", t, nested=True):
                lines.append(f"metadata:\n  type: {t}")
            return "---\n" + "\n".join(lines) + "\n---\n\n" + f["body"].strip() + "\n"
    return (
        f"---\nname: {_q(f['name'])}\ndescription: {_q(f['description'])}\n"
        f"metadata:\n  type: {t}\n---\n\n{f['body'].strip()}\n"
    )


# --- vault fact <-> parsed fields --------------------------------------------

def build_vault_fact(f, repo, created, last_synced):
    return (
        "---\n"
        f"type: {eff_type(f)}\n"
        f"repo: {repo}\n"
        f"name: {_q(f['name'])}\n"
        f"description: {_q(f['description'])}\n"
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
    """Vault CLAUDE.md mirror minus ONLY our injected banner span (exact sentinel,
    at the head) — never the user's own `> [!note]` callouts."""
    body = split_frontmatter(text)[1]
    if body.lstrip().startswith(BANNER):
        body = body.lstrip()[len(BANNER):]
        end = body.find(BANNER)
        if end != -1:
            body = body[end + len(BANNER):]
    return body.strip()


def fact_canon(f):
    """Canonical content for change-detection: the fields that round-trip both ways
    (name/description/effective-type/body) — NOT the vault-only dates."""
    return "\n".join([f.get("name", ""), f.get("description", ""),
                      eff_type(f), f.get("body", "").strip()])


# --- config -------------------------------------------------------------------

def load_config(repo, require_scheme=True):
    path = repo / CONFIG_REL
    if not path.exists():
        sys.exit(f"no {CONFIG_REL} in {repo} — run `init` first")
    try:
        cfg = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"corrupt marker {path}: {e}")
    scheme = cfg.get("hash_scheme", 1)
    if scheme != HASH_SCHEME:
        msg = (f"marker uses hash_scheme {scheme}, this sync.py expects {HASH_SCHEME}.\n"
               f"Every fact would look like a conflict. Re-baseline once, choosing an authority:\n"
               f"  push --force   (repo wins)   |   pull --force   (vault wins)")
        if require_scheme:
            sys.exit(msg)
        cfg["_scheme_mismatch"] = scheme
    return cfg


def save_config(repo, cfg):
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}
    cfg["hash_scheme"] = HASH_SCHEME
    path = repo / CONFIG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n")


def repo_slug(repo):
    return slugify(repo.resolve().name)


def ensure_gitignored(repo):
    """The marker holds a machine-local absolute vault path, so it must not be
    committed. If `repo` is a git repo and the marker isn't already ignored, append
    it to .gitignore. Idempotent. Returns a short human status."""
    if not (repo / ".git").exists():
        return "repo is not under git — nothing to gitignore"
    import subprocess
    try:
        r = subprocess.run(["git", "check-ignore", "-q", str(CONFIG_REL)],
                           cwd=str(repo), capture_output=True, timeout=10)
        if r.returncode == 0:
            return "marker already gitignored"
    except (OSError, subprocess.SubprocessError):
        pass  # git unavailable — fall back to editing .gitignore directly
    gi = repo / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    if str(CONFIG_REL) in existing.splitlines():
        return "marker already in .gitignore"
    with gi.open("a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"\n# Claude <-> Obsidian memory sync marker (machine-local absolute paths)\n{CONFIG_REL}\n")
    return "added marker to .gitignore"


def vault_readable(vault_dir):
    """Probe whether the vault is reachable. Returns (ok, reason). Never raises."""
    try:
        d = Path(vault_dir)
        if not d.exists():
            return False, "vault_dir does not exist"
        list(d.iterdir())          # the operation macOS TCC actually blocks
        return True, "readable"
    except PermissionError:
        return False, ("blocked by macOS Full Disk Access — grant it to your terminal app in "
                       "System Settings ▸ Privacy & Security ▸ Full Disk Access, then restart it")
    except OSError as e:
        return False, str(e)


def vault_repo_dir(cfg):
    return Path(cfg["vault_dir"]) / cfg["repo_subdir"]


def vault_root(vault_dir):
    """Walk up to the dir containing `.obsidian`. None if not found."""
    root = Path(vault_dir).resolve()
    while True:
        try:
            if (root / ".obsidian").exists():
                return root
        except OSError as e:  # TCC / permissions — surface, don't silently fall back
            sys.exit(f"cannot access {root}: {e}\nGrant Full Disk Access to your terminal.")
        if root == root.parent:
            return None
        root = root.parent


def vault_rel_folder(vault_dir):
    """Vault-root-relative path, for a Bases file.inFolder() filter."""
    vault_dir = Path(vault_dir).resolve()
    root = vault_root(vault_dir)
    if root:
        try:
            return str(vault_dir.relative_to(root))
        except ValueError:
            pass
    return vault_dir.name


# --- index --------------------------------------------------------------------

def read_index(memory_dir):
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


def _safe_title(title):
    """Index titles must survive INDEX_RE round-trip: no brackets/parens."""
    return re.sub(r"[\[\]()]", "", title).strip() or "untitled"


def append_index(memory_dir, title, fname, hook="added via vault sync"):
    path = memory_dir / "MEMORY.md"
    lines = path.read_text().splitlines() if path.exists() else []
    lines.append(f"- [{_safe_title(title)}]({fname}) — {hook}")
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
    vdir = vault_repo_dir(cfg)
    items = {}
    keys = set(index_files(memory_dir))
    keys |= {p.name for p in memory_dir.glob("*.md") if p.name != "MEMORY.md"}
    if vdir.exists():
        for vf in vdir.glob("*.md"):
            if vf.name in ("MEMORY.md", cfg["claude_md"]["vault_file"]):
                continue
            keys.add(vf.name)
    for fname in sorted(keys):
        rpath, vpath = memory_dir / fname, vdir / fname
        repo_body = fact_canon(parse_repo_fact(rpath.read_text(), fname[:-3])) if rpath.exists() else ""
        vault_body = fact_canon(parse_vault_fact(vpath.read_text(), fname[:-3])) if vpath.exists() else ""
        items[fname] = {"kind": "fact", "repo_path": rpath, "vault_path": vpath,
                        "repo_body": repo_body, "vault_body": vault_body,
                        "rec": cfg["facts"].get(fname, {})}

    cpath = repo / "CLAUDE.md"
    vpath = vdir / cfg["claude_md"]["vault_file"]
    items["claude_md"] = {
        "kind": "claude_md", "repo_path": cpath, "vault_path": vpath,
        "repo_body": cpath.read_text() if cpath.exists() else "",
        "vault_body": norm_claude_vault(vpath.read_text()) if vpath.exists() else "",
        "rec": cfg["claude_md"],
    }
    return items


# --- commands -----------------------------------------------------------------

def cmd_init(args):
    repo, memory, vault = Path(args.repo), Path(args.memory), Path(args.vault)
    if (repo / CONFIG_REL).exists() and not args.force:
        sys.exit(f"{repo/CONFIG_REL} already exists — pass --force to re-link "
                 f"(this DROPS all recorded sync hashes)")
    if vault_root(vault) is None:
        sys.exit(f"--vault {vault} is not inside an Obsidian vault (no .obsidian found up-tree)")
    slug = repo_slug(repo)
    (vault / slug).mkdir(parents=True, exist_ok=True)
    save_config(repo, {
        "vault_dir": str(vault.resolve()),
        "repo_subdir": slug,
        "memory_dir": str(memory.resolve()),
        "repo_root": str(repo.resolve()),
        "linked_at": date.today().isoformat(),
        "claude_md": {"vault_file": "CLAUDE.md.md", "last_hash_repo": "", "last_hash_vault": ""},
        "facts": {},
    })
    gi = ensure_gitignored(repo)   # the chore I kept forgetting — now automatic
    print(json.dumps({"config": str(repo / CONFIG_REL), "vault_repo_dir": str(vault / slug),
                      "gitignore": gi}))


def cmd_status(args):
    repo = Path(args.repo)
    cfg = load_config(repo, require_scheme=False)
    memory_dir = Path(args.memory) if args.memory else Path(cfg["memory_dir"])
    items = gather(repo, memory_dir, cfg)
    result, removed = {}, []
    for key, it in items.items():
        if it["kind"] == "fact" and it["rec"]:
            if not it["repo_path"].exists():
                removed.append({"file": key, "gone_from": "repo"}); continue
            if not it["vault_path"].exists():
                removed.append({"file": key, "gone_from": "vault"}); continue
        result[key] = classify(it["repo_body"], it["vault_body"], it["rec"])
    out = {"facts": {k: v for k, v in result.items() if k != "claude_md"},
           "claude_md": result.get("claude_md", "no_change")}
    if removed:
        out["removed"] = removed
    # index integrity (B4)
    on_disk = {p.name for p in memory_dir.glob("*.md") if p.name != "MEMORY.md"}
    idx = index_files(memory_dir)
    if on_disk - idx:
        out["not_in_index"] = sorted(on_disk - idx)
    if idx - on_disk:
        out["indexed_but_missing"] = sorted(idx - on_disk)
    if "_scheme_mismatch" in cfg:
        out["hash_scheme_mismatch"] = {"marker": cfg["_scheme_mismatch"], "expected": HASH_SCHEME,
                                       "remedy": "push --force (repo wins) or pull --force (vault wins)"}
    print(json.dumps(out, indent=2))


def cmd_doctor(args):
    """Health-check an association, reporting every recurring failure mode with its
    fix. Read-only except for the one safe, repeatedly-needed repair: gitignoring
    the marker (skip with --no-fix)."""
    repo = Path(args.repo)
    cfg = load_config(repo, require_scheme=False)
    memory_dir = Path(args.memory) if args.memory else Path(cfg["memory_dir"])
    checks = []

    def add(ok, name, detail):
        checks.append(("PASS" if ok else ("WARN" if ok is None else "FAIL"), name, detail))

    add(True, "marker", f"{repo / CONFIG_REL} (linked {cfg.get('linked_at','?')}, "
                        f"subdir {cfg['repo_subdir']})")

    if "_scheme_mismatch" in cfg:
        add(False, "hash scheme", f"marker is scheme {cfg['_scheme_mismatch']}, sync.py is "
            f"{HASH_SCHEME} → every fact would look like a conflict. Re-baseline once: "
            f"`push --force` (repo wins) or `pull --force` (vault wins).")
    else:
        add(True, "hash scheme", f"v{HASH_SCHEME}")

    if memory_dir.exists():
        files = {p.name for p in memory_dir.glob("*.md") if p.name != "MEMORY.md"}
        idx = index_files(memory_dir)
        drift = (files - idx) | (idx - files)
        add(None if drift else True, "memory dir",
            f"{len(files)} fact files at {memory_dir}" +
            (f"; INDEX DRIFT: {sorted(drift)} (run `status`, fix MEMORY.md)" if drift else ""))
    else:
        add(False, "memory dir", f"{memory_dir} does not exist")

    if (repo / ".git").exists():
        gi = ensure_gitignored(repo) if getattr(args, "fix", True) else "check .gitignore manually"
        add("already" in gi or "added" in gi, "gitignore",
            f"{gi} (marker holds a machine-local absolute path)")
    else:
        add(None, "gitignore", "repo not under git — marker won't be committed anyway")

    # probe the per-repo SUBFOLDER (what the plugin actually reads/writes) — macOS TCC is
    # per-path, so the subfolder can be fine even when listing the parent vault is blocked.
    vdir = vault_repo_dir(cfg)
    probe = vdir if vdir.exists() else Path(cfg["vault_dir"])
    ok, reason = vault_readable(probe)
    add(ok, "vault access", f"{probe} — {reason}")
    if ok and vault_root(cfg["vault_dir"]) is None:
        add(None, "vault root", "no .obsidian found up-tree — the Bases filter may use the wrong path")

    width = max(len(n) for _s, n, _d in checks)
    print(f"obsidian-memory doctor — {repo.name}")
    for status, name, detail in checks:
        print(f"  [{status:4}] {name.ljust(width)}  {detail}")
    if any(s == "FAIL" for s, _n, _d in checks):
        sys.exit(1)


def discover_associations(roots):
    """Find every repo association marker under `roots`. Read-only; dedupes repos
    that share a memory dir (symlinked checkouts)."""
    import os
    out = []
    for root in roots:
        root = Path(os.path.expanduser(root))
        if not root.exists():
            continue
        try:
            markers = list(root.glob("**/.claude/obsidian-sync.json"))
        except OSError:
            continue
        for m in markers:
            try:
                cfg = json.loads(m.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            out.append({"repo": m.parent.parent, "vault_dir": cfg["vault_dir"],
                        "subdir": cfg["repo_subdir"], "memory_dir": Path(cfg["memory_dir"])})
    seen = {}
    for a in out:
        seen.setdefault(str(a["memory_dir"]), a)
    return list(seen.values())


def vault_family(vault_dir):
    parts = Path(vault_dir).parts
    for key in ("academy", "industry", "personal", "old"):
        if key in parts:
            return key
    return Path(vault_dir).name


def _wikilinks(text):
    return {m.strip() for m in re.findall(r"\[\[([^\]|#]+)", text)}


def _tokens(subdir):
    """Significant name tokens for clustering (drop years/version noise)."""
    return {t for t in re.split(r"[-_\s]+", subdir.lower())
            if t and not t.isdigit() and t not in ("ms", "client", "benchmark", "comparison", "iedb")}


def cmd_audit(args):
    """Vault-wide content audit: inventory, link hygiene, project clusters, and
    cross-repo link gaps. Read-only. The judgment layer (skill) uses this to plan a
    refactor. Scopes to the vault family of --repo unless --all."""
    roots = args.roots or ["~/vcs", "~/work"]
    assoc = discover_associations(roots)
    if not assoc:
        sys.exit(f"no associations found under {roots}")
    fam = None if args.all else vault_family(load_config(Path(args.repo), require_scheme=False)["vault_dir"])
    fams = {}
    for a in assoc:
        fams.setdefault(vault_family(a["vault_dir"]), []).append(a)

    report = {}
    for family, repos in sorted(fams.items()):
        if fam and family != fam:
            continue
        # gather everyone's notes/text/links once
        info = {}
        stem_owner = {}
        for a in repos:
            notes = [p for p in a["memory_dir"].glob("*.md") if p.name != "MEMORY.md"] \
                if a["memory_dir"].exists() else []
            text = "\n".join(p.read_text() for p in notes)
            cm = a["repo"] / "CLAUDE.md"
            info[a["subdir"]] = {
                "a": a, "notes": notes, "text": text,
                "stems": {p.stem for p in notes},
                "outlinks": _wikilinks(text),
                "claude_md": cm.read_text() if cm.exists() else None,
                "tokens": _tokens(a["subdir"]),
            }
            for s in info[a["subdir"]]["stems"]:
                stem_owner[s] = a["subdir"]

        # per-repo hygiene + cross-repo signals
        per, gaps = {}, []
        for sub, d in info.items():
            inbound = {o for o, dd in info.items() if o != sub and (d["stems"] & dd["outlinks"])}
            outbound = {stem_owner[s] for s in d["outlinks"] if s in stem_owner and stem_owner[s] != sub}
            dangling = sorted(s for s in d["outlinks"] if s not in stem_owner)
            orphans = sorted(p.stem for p in d["notes"]
                             if not (_wikilinks(p.read_text()) & stem_owner.keys())
                             and p.stem not in {s for o, dd in info.items() if o != sub for s in dd["outlinks"]})
            cm = d["claude_md"]
            wall = bool(cm) and len(cm) > 8000 and cm.count("\n## ") + cm.count("\n# ") < 6
            per[sub] = {
                "claude_md_chars": len(cm) if cm else 0,
                "claude_md_headings": (cm.count("\n#") if cm else 0),
                "wall": wall,
                "facts": len(d["notes"]),
                "orphans": orphans,
                "dangling": dangling,
                "links_out_to": sorted(outbound),
                "links_in_from": sorted(inbound),
            }
            # textual cross-mention not yet linked
            for other, od in info.items():
                if other == sub or other in outbound:
                    continue
                if od["tokens"] and any(re.search(rf"\b{re.escape(t)}\b", d["text"], re.I) for t in od["tokens"]):
                    gaps.append((sub, other))

        # clusters by shared name token OR shared 3-char name prefix (catches
        # tcren2/tcren-ms, vdjmatch/vdjtools/vdjdb families that share no full token)
        clusters = {}
        for sub, d in info.items():
            keys = set(d["tokens"])
            alpha = re.sub(r"[^a-z]", "", sub.lower())
            if len(alpha) >= 3:
                keys.add(alpha[:3] + "*")
            for k in keys:
                clusters.setdefault(k, set()).add(sub)
        clusters = {t: sorted(v) for t, v in clusters.items() if len(v) > 1}
        # collapse keys with identical member sets, preferring a real token over a prefix
        by_members = {}
        for t, v in clusters.items():
            by_members.setdefault(tuple(v), []).append(t)
        clusters = {sorted(ks, key=lambda k: ("*" in k, len(k)))[0]: list(v)
                    for v, ks in by_members.items()}

        report[family] = {"repos": sorted(info), "per": per, "gaps": sorted(set(gaps)), "clusters": clusters}

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return
    for family, r in report.items():
        print(f"\n{'='*66}\nVAULT: {family}  ({len(r['repos'])} repos)\n{'='*66}")
        if r["clusters"]:
            print("  clusters (shared name token):")
            for t, subs in sorted(r["clusters"].items(), key=lambda kv: -len(kv[1])):
                print(f"    {t:12} {', '.join(subs)}")
        print("  repos:")
        for sub in r["repos"]:
            p = r["per"][sub]
            cm = f"CLAUDE.md {p['claude_md_chars']}c/{p['claude_md_headings']}h" + (" ⚠WALL" if p["wall"] else "") if p["claude_md_chars"] else "no CLAUDE.md"
            print(f"    ▸ {sub}")
            print(f"        {cm} | {p['facts']} facts | links out→{p['links_out_to'] or '—'} in←{p['links_in_from'] or '—'}")
            if p["orphans"]:
                print(f"        orphans (no links): {', '.join(p['orphans'][:8])}" + (" …" if len(p['orphans']) > 8 else ""))
            if p["dangling"]:
                print(f"        dangling links: {', '.join(p['dangling'][:8])}" + (" …" if len(p['dangling']) > 8 else ""))
        if r["gaps"]:
            print("  cross-repo GAPS (A mentions B by name, not linked):")
            for a, b in r["gaps"]:
                print(f"    {a} → {b}")


def _repo_summary(a):
    """One-line descriptor: CLAUDE.md's first prose line, else the top fact's description."""
    cm = a["repo"] / "CLAUDE.md"
    if cm.exists():
        for line in cm.read_text().splitlines():
            s = line.strip()
            if s and not s.startswith(("#", "---", "<!--", ">")):
                return s[:160]
    if a["memory_dir"].exists():
        for p in sorted(a["memory_dir"].glob("*.md")):
            if p.name == "MEMORY.md":
                continue
            d = fm_get(split_frontmatter(p.read_text())[0], "description")
            if d:
                return d[:160]
    return "(no summary)"


def _repo_facts(a):
    """[(stem, type, description)] for a repo's facts, sorted by type then stem."""
    out = []
    if a["memory_dir"].exists():
        for p in sorted(a["memory_dir"].glob("*.md")):
            if p.name == "MEMORY.md":
                continue
            f = parse_repo_fact(p.read_text(), p.stem)
            out.append((p.stem, eff_type(f), f["description"]))
    return sorted(out, key=lambda t: (t[1], t[0]))


def cmd_map(args):
    """Generate the one-way 'Atlas' layer in <vault_root>/_maps/: a Map of Content,
    a hub per project cluster, and a hub per repo (linking its condensed notes + cluster
    siblings). Regenerable — index/link structure only, so re-running is safe. Reads an
    optional <vault_root>/_maps/clusters.json to override the auto (name-token) clusters
    with curated semantic ones: {"cluster name": ["repo-subdir", ...], ...}."""
    roots = args.roots or ["~/vcs", "~/work"]
    assoc = discover_associations(roots)
    fam_want = None if args.all else vault_family(load_config(Path(args.repo), require_scheme=False)["vault_dir"])
    fams = {}
    for a in assoc:
        fams.setdefault(vault_family(a["vault_dir"]), []).append(a)

    for family, repos in sorted(fams.items()):
        if fam_want and family != fam_want:
            continue
        by_sub = {a["subdir"]: a for a in repos}
        vroot = vault_root(repos[0]["vault_dir"])
        if vroot is None:
            print(f"skip {family}: no .obsidian root found"); continue
        maps = vroot / "_maps"
        maps.mkdir(exist_ok=True)

        # curated one-liners survive regeneration: _maps/summaries.json {subdir: "..."}
        sfile = maps / "summaries.json"
        summ = json.loads(sfile.read_text()) if sfile.exists() else {}

        def summary(sub):
            return summ.get(sub) or _repo_summary(by_sub[sub])

        # clusters: curated file wins, else auto (shared token / 3-char prefix).
        # a cluster value may be a plain list OR {"desc": "...", "repos": [...]}.
        cdesc = {}
        spec = maps / "clusters.json"
        if spec.exists():
            clusters = {}
            for k, v in json.loads(spec.read_text()).items():
                repos = v["repos"] if isinstance(v, dict) else v
                if isinstance(v, dict) and v.get("desc"):
                    cdesc[k] = v["desc"]
                clusters[k] = [s for s in repos if s in by_sub]
        else:
            clusters = {}
            for sub, a in by_sub.items():
                keys = _tokens(sub) | ({re.sub(r"[^a-z]", "", sub.lower())[:3] + "*"} if len(sub) >= 3 else set())
                for k in keys:
                    clusters.setdefault(k, []).append(sub)
            clusters = {k: sorted(set(v)) for k, v in clusters.items() if len(set(v)) > 1}
        clustered = {s for subs in clusters.values() for s in subs}
        singletons = sorted(set(by_sub) - clustered)

        def cluster_of(sub):
            return next((c for c, subs in clusters.items() if sub in subs), None)

        # --- repo hubs ---
        for sub, a in sorted(by_sub.items()):
            sibs = [s for s in clusters.get(cluster_of(sub) or "", []) if s != sub]
            facts = _repo_facts(a)
            L = [f"---\ntype: map\nrepo: {sub}\ntags: [memory, map]\n---", "",
                 f"# {sub}", "", f"> {summary(sub)}", ""]
            c = cluster_of(sub)
            if c:
                L.append(f"**Cluster:** [[{c}]]" + (f" · siblings: " + " ".join(f"[[{s}]]" for s in sibs) if sibs else ""))
            if (maps / f"{sub} — guide.md").exists():
                L.append(f"**Guide:** [[{sub} — guide]] (unwrapped, human-readable)")
            if (a["repo"] / "CLAUDE.md").exists():
                L.append(f"**Project context:** see the condensed `CLAUDE.md` mirror in this repo's vault folder.")
            L += ["", "## Facts", ""]
            cur = None
            for stem, t, desc in facts:
                if t != cur:
                    L.append(f"### {t}"); cur = t
                L.append(f"- [[{stem}]]" + (f" — {desc}" if desc else ""))
            if not facts:
                L.append("*(no memory facts yet)*")
            (maps / f"{sub}.md").write_text("\n".join(L) + "\n")

        # --- cluster hubs ---
        for c, subs in sorted(clusters.items()):
            L = [f"---\ntype: map\ntags: [memory, map, cluster]\n---", "", f"# {c}", "",
                 f"> {cdesc.get(c, 'Project cluster. Members cross-link below.')}", "", "## Projects", ""]
            for sub in subs:
                L.append(f"- [[{sub}]] — {summary(sub)}")
            (maps / f"{c}.md").write_text("\n".join(L) + "\n")

        # --- Map of Content ---
        L = [f"---\ntype: map\ntags: [memory, map, moc]\n---", "", f"# {family} — Map of Content", "",
             "> Auto-generated navigation for the Claude-memory mirror. Hubs link the **condensed**",
             "> fact notes (which round-trip to each repo). `map` rewrites these hubs, so curate the",
             "> one-liners in `_maps/summaries.json` and clusters/descriptions in `_maps/clusters.json`;",
             "> re-running preserves them. Hand-written `<repo> — guide.md` notes are never overwritten.", ""]
        if clusters:
            L += ["## Clusters", ""]
            for c, subs in sorted(clusters.items()):
                L.append(f"- **[[{c}]]** — " + " ".join(f"[[{s}]]" for s in subs))
        if singletons:
            L += ["", "## Standalone", ""]
            for s in singletons:
                L.append(f"- [[{s}]] — {summary(s)}")
        (maps / f"{family}.md").write_text("\n".join(L) + "\n")

        print(f"{family}: wrote _maps/ ({len(by_sub)} repo hubs, {len(clusters)} clusters) at {maps}")

        if getattr(args, "enrich", False):
            enrich_facts(by_sub, apply=getattr(args, "apply", False))


def enrich_facts(by_sub, apply=False):
    """Add a '**Related projects:**' footer to each fact whose body names another
    repo in the same vault, linking that repo's Atlas hub. Idempotent (re-run replaces
    the footer). Modifies repo memory only — a subsequent `push` mirrors it to the vault.
    Dry-run unless apply=True. In-context, non-destructive (appends, never rewrites prose)."""
    # match a repo by its exact subdir, plus a couple of obvious short aliases
    alias = {"tcren2": "2026-tcren2", "aldan3": "aldan3-client"}
    term2hub = {s.lower(): s for s in by_sub}
    term2hub.update({a: h for a, h in alias.items() if h in by_sub})
    changed, preview = 0, []
    for sub, a in sorted(by_sub.items()):
        if not a["memory_dir"].exists():
            continue
        for p in sorted(a["memory_dir"].glob("*.md")):
            if p.name == "MEMORY.md":
                continue
            raw = p.read_text()
            f = parse_repo_fact(raw, p.stem)
            body = re.sub(r"\n*\*\*Related projects:\*\*.*(?:\n|$)", "", f["body"]).rstrip()
            hubs = set()
            for term, hub in term2hub.items():
                if hub == sub:
                    continue
                if re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", body, re.I):
                    hubs.add(hub)
            new_body = body + ("\n\n**Related projects:** " + " ".join(f"[[{h}]]" for h in sorted(hubs)) if hubs else "")
            if new_body.strip() != f["body"].strip():
                changed += 1
                if hubs:
                    preview.append(f"    {sub}/{p.stem} → {', '.join(sorted(hubs))}")
                if apply:
                    p.write_text(build_repo_fact({**f, "body": new_body}, raw))
    print(f"  enrich: {'applied to' if apply else 'would touch'} {changed} facts"
          + ("" if apply else " (dry-run; pass --apply to write, then `push`)"))
    for line in preview[:40]:
        print(line)
    if len(preview) > 40:
        print(f"    … +{len(preview) - 40} more")


def do_push(repo, memory_dir, cfg, key, it):
    it["vault_path"].parent.mkdir(parents=True, exist_ok=True)
    if it["kind"] == "claude_md":
        body = it["repo_body"].strip()
        it["vault_path"].write_text(
            "---\ntype: claude-md-mirror\n"
            f"repo: {cfg['repo_subdir']}\nlast_synced: {date.today().isoformat()}\n"
            "tags: [memory, claude-md]\n---\n\n"
            f"{BANNER}\n> [!note] Mirror of this repo's CLAUDE.md. Edits here sync back on pull.\n{BANNER}\n\n"
            f"{body}\n")
        h = sha(it["repo_body"])
        cfg["claude_md"].update(last_hash_repo=h, last_hash_vault=h)
        return
    f = parse_repo_fact(it["repo_path"].read_text(), key[:-3])
    created = date.today().isoformat()
    if it["vault_path"].exists():
        created = parse_vault_fact(it["vault_path"].read_text(), key[:-3]).get("created") or created
    it["vault_path"].write_text(build_vault_fact(f, cfg["repo_subdir"], created, date.today().isoformat()))
    h = sha(fact_canon(f))
    cfg["facts"].setdefault(key, {}).update(last_hash_repo=h, last_hash_vault=h)


def do_pull(repo, memory_dir, cfg, key, it):
    if it["kind"] == "claude_md":
        body = norm_claude_vault(it["vault_path"].read_text())
        (repo / "CLAUDE.md").write_text(body + "\n")
        h = sha(body)
        cfg["claude_md"].update(last_hash_repo=h, last_hash_vault=h)
        return
    v = parse_vault_fact(it["vault_path"].read_text(), key[:-3])
    is_new = not it["repo_path"].exists()
    existing = None if is_new else it["repo_path"].read_text()
    it["repo_path"].parent.mkdir(parents=True, exist_ok=True)
    it["repo_path"].write_text(build_repo_fact(v, existing))   # preserves sibling metadata
    if is_new and key not in index_files(memory_dir):
        append_index(memory_dir, v["description"] or v["name"], key)
    h = sha(fact_canon(v))
    cfg["facts"].setdefault(key, {}).update(last_hash_repo=h, last_hash_vault=h)


def run_direction(args, direction):
    repo = Path(args.repo)
    cfg = load_config(repo, require_scheme=not args.force)
    memory_dir = Path(args.memory) if args.memory else Path(cfg["memory_dir"])
    items = gather(repo, memory_dir, cfg)
    fn = do_push if direction == "push" else do_pull
    act_states = ("init", "push") if direction == "push" else ("init", "pull")
    done = []
    try:
        for key, it in items.items():
            if args.only and args.only != key:
                continue
            # the SOURCE side must actually exist — never bypassed by --force (A3)
            src = it["repo_path"] if direction == "push" else it["vault_path"]
            if not src.exists() or not (it["repo_body"] if direction == "push" else it["vault_body"]).strip():
                continue
            state = classify(it["repo_body"], it["vault_body"], it["rec"])
            if state == "conflict" and not args.force:
                print(f"SKIP {key}: conflict — resolve or pass --force")
                continue
            # --only NARROWS scope; only --force overrides the direction guard (A6)
            if not args.force and state not in act_states:
                continue
            fn(repo, memory_dir, cfg, key, it)
            done.append(key)
        regenerate_dashboard(repo, memory_dir, cfg)   # both directions (B2)
    finally:
        save_config(repo, cfg)
    print(f"{direction}ed: {', '.join(done) if done else '(nothing)'}")


def cmd_push(args):
    run_direction(args, "push")


def cmd_pull(args):
    run_direction(args, "pull")


def cmd_sync(args):
    args.only = None
    run_direction(args, "push")
    run_direction(args, "pull")


def _check_slug(slug):
    if ("/" in slug or "\\" in slug or slug.startswith(".")
            or not slug.endswith(".md") or slug == "MEMORY.md"):
        sys.exit(f"invalid fact filename: {slug!r}")


def _inbound_links(memory_dir, stem, exclude):
    hits = []
    for p in memory_dir.glob("*.md"):
        if p.name in (exclude, "MEMORY.md"):
            continue
        if f"[[{stem}]]" in p.read_text():
            hits.append(p.name)
    return hits


def cmd_archive(args):
    """Reversibly trim a fact: move the repo file to memory/_archive/, drop its
    MEMORY.md line, remove the vault mirror, forget it in the marker."""
    repo = Path(args.repo)
    cfg = load_config(repo)
    memory_dir = Path(args.memory) if args.memory else Path(cfg["memory_dir"])
    slug = args.only
    _check_slug(slug)
    src = memory_dir / slug
    if not src.exists():
        sys.exit(f"no such fact: memory/{slug}")
    links = _inbound_links(memory_dir, slug[:-3], slug)
    if links:
        print(f"warning: {len(links)} inbound [[{slug[:-3]}]] link(s) from: {', '.join(links)}")
    (memory_dir / "_archive").mkdir(exist_ok=True)
    dest = memory_dir / "_archive" / slug
    if dest.exists():   # never clobber an existing archived copy
        dest = memory_dir / "_archive" / f"{slug[:-3]}.{date.today().isoformat()}.md"
    src.rename(dest)
    remove_index_line(memory_dir, slug)
    vpath = vault_repo_dir(cfg) / slug
    if vpath.exists():
        vpath.unlink()
    cfg["facts"].pop(slug, None)
    try:
        regenerate_dashboard(repo, memory_dir, cfg)
    finally:
        save_config(repo, cfg)
    print(f"archived {slug} -> {dest.relative_to(memory_dir.parent)}; removed vault mirror")


def cmd_restore(args):
    """Undo an archive: move it back into active memory, re-index, re-mirror."""
    repo = Path(args.repo)
    cfg = load_config(repo)
    memory_dir = Path(args.memory) if args.memory else Path(cfg["memory_dir"])
    slug = args.only
    _check_slug(slug)
    src = memory_dir / "_archive" / slug
    if not src.exists():
        sys.exit(f"no archived memory/_archive/{slug} to restore")
    dest = memory_dir / slug
    if dest.exists():
        sys.exit(f"memory/{slug} already exists — refusing to clobber it with the archived copy")
    f = parse_repo_fact(src.read_text(), slug[:-3])   # parse BEFORE moving (A8)
    src.rename(dest)
    if slug not in index_files(memory_dir):
        append_index(memory_dir, f["description"] or f["name"], slug, "restored from archive")
    items = gather(repo, memory_dir, cfg)
    try:
        do_push(repo, memory_dir, cfg, slug, items[slug])
        regenerate_dashboard(repo, memory_dir, cfg)
    finally:
        save_config(repo, cfg)
    print(f"restored {slug} from archive and re-synced")


# --- generated dashboard: memory.base + MEMORY.md rollup ----------------------

def regenerate_dashboard(repo, memory_dir, cfg):
    vdir = vault_repo_dir(cfg)
    vdir.mkdir(parents=True, exist_ok=True)
    rel = vault_rel_folder(vdir)

    # groupBy is an OBJECT (property+direction), not a string — verified against
    # help.obsidian.md/bases/syntax. No date math / view-level sort (version-sensitive).
    (vdir / "memory.base").write_text(
        "filters:\n  and:\n"
        f'    - file.inFolder("{rel}")\n'
        '    - note.type != ""\n'
        "views:\n  - type: table\n    name: By type\n"
        "    groupBy:\n      property: note.type\n      direction: ASC\n"
        "    order:\n      - file.name\n      - note.description\n      - note.last_synced\n")

    counts, rows = {}, []
    for vf in sorted(vdir.glob("*.md")):
        if vf.name in ("MEMORY.md", cfg["claude_md"]["vault_file"]):
            continue
        v = parse_vault_fact(vf.read_text(), vf.stem)
        counts[eff_type(v)] = counts.get(eff_type(v), 0) + 1
        rows.append((v.get("last_synced", ""), eff_type(v), vf.stem, v["description"]))
    rows.sort(reverse=True)
    total = sum(counts.values())

    lines = [
        "---", "type: memory-dashboard", f"repo: {cfg['repo_subdir']}",
        f"last_synced: {date.today().isoformat()}", "tags: [memory, dashboard]", "---", "",
        f"# Claude memory — {cfg['repo_subdir']}", "",
        "> [!info] Auto-generated dashboard, rewritten on every sync.",
        "> Edit fact notes directly — those sync back.", "",
        "## Rollup", "",
        f"- **{total}** facts: " + (", ".join(f"{n} {t}" for t, n in sorted(counts.items())) or "none yet"),
        "", "## Dashboard (Bases)", "", "![[memory.base]]", "",
        "> If the embedded view is empty, open `memory.base` and adjust the filter —",
        f'> Bases syntax evolves; the intended scope is `file.inFolder("{rel}")` + has `type`.',
        "", "## Recently synced", "",
    ]
    for ls, t, stem, desc in rows[:15]:
        lines.append(f"- `{ls}` · **{t}** · [[{stem}]]" + (f" — {desc}" if desc else ""))
    lines += ["", "## All facts", "", "*(outgoing links — these populate each fact's Backlinks pane)*", ""]
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
    quiet = lambda: contextlib.redirect_stdout(io.StringIO())

    class A:
        pass

    def mkrepo(name="repo", claude_md=True):
        root = tmp / name
        repo, memory, vault = root / "repo", root / "repo/memory", root / "vault"
        memory.mkdir(parents=True)
        (vault / ".obsidian").mkdir(parents=True)
        if claude_md:
            (repo / "CLAUDE.md").write_text("# Test project\n\n> [!note] my own callout\n\nSome context.\n")
        (memory / "MEMORY.md").write_text("- [User is a tester](user_role.md) — who\n")
        (memory / "user_role.md").write_text(
            "---\nname: user_role\ndescription: User is a tester\nmetadata:\n"
            "  node_type: memory\n  type: user\n  originSessionId: abc-123\n---\n\nUser is a tester.\n")
        a = A(); a.repo = str(repo); a.memory = str(memory); a.vault = str(vault)
        a.only = None; a.force = False
        return repo, memory, vault, a

    try:
        repo, memory, vault, a = mkrepo()
        with quiet(): cmd_init(a)
        cfg = load_config(repo); vdir = vault / cfg["repo_subdir"]
        assert cfg["hash_scheme"] == HASH_SCHEME

        # init refuses to clobber an existing marker (A7)
        try:
            with quiet(): cmd_init(a)
            raise AssertionError("init should refuse to overwrite a marker")
        except SystemExit:
            pass

        (memory / "orphan.md").write_text(
            "---\nname: orphan\ndescription: not in the index\nmetadata:\n  type: project\n---\n\nOrphan body.\n")
        with quiet(): cmd_push(a)
        assert (vdir / "orphan.md").exists(), "un-indexed memory file still mirrored"
        vf = vdir / "user_role.md"
        text = vf.read_text()
        assert "type: user" in text and "repo: repo" in text
        assert (vdir / "memory.base").exists() and "groupBy:\n      property: note.type" in (vdir / "memory.base").read_text()
        assert (vdir / "CLAUDE.md.md").exists()

        # A4: the user's own callout survives a push->pull round trip
        with quiet(): cmd_pull(a)
        assert "> [!note] my own callout" in (repo / "CLAUDE.md").read_text(), "user callout preserved"
        assert BANNER not in (repo / "CLAUDE.md").read_text()

        # A1: a vault-side edit must NOT destroy sibling repo frontmatter
        vf.write_text(text.replace("User is a tester.\n", "User is a tester. Edited on phone.\n"))
        with quiet(): cmd_pull(a)
        rt = (memory / "user_role.md").read_text()
        assert "Edited on phone." in rt
        assert "node_type: memory" in rt and "originSessionId: abc-123" in rt, "sibling metadata preserved"
        assert "type: user" in rt

        # new vault-authored note pulls in, and the dashboard refreshes on pull (B2)
        (vdir / "new-fact.md").write_text(
            "---\ntype: reference\nrepo: repo\nname: new-fact\ndescription: A new ref\n"
            "created: 2026-07-04\nlast_synced: 2026-07-04\ntags: [memory, repo/repo]\n---\n\nSee the docs.\n")
        with quiet(): cmd_pull(a)
        assert (memory / "new-fact.md").exists() and "new-fact.md" in (memory / "MEMORY.md").read_text()
        assert "[[new-fact]]" in (vdir / "MEMORY.md").read_text(), "dashboard regenerated on pull"

        # A6: --only narrows, it does NOT force. State is `pull`; a scoped push must no-op.
        vf.write_text(vf.read_text().replace("Edited on phone.", "Phone edit two."))
        a.only = "user_role.md"
        with quiet(): cmd_push(a)
        a.only = None
        assert "Phone edit two." in vf.read_text(), "--only push must not clobber a pending vault edit"

        # conflict detection + force
        (memory / "user_role.md").write_text(build_repo_fact(
            {"name": "user_role", "description": "User is a tester", "type": "user", "body": "Repo side."},
            (memory / "user_role.md").read_text()))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): cmd_push(a)
        assert "conflict" in buf.getvalue()
        a.force = True
        with quiet(): cmd_push(a)
        a.force = False
        assert "Repo side." in vf.read_text()

        # archive: validation + no clobber + inbound-link warning
        for bad in ("../CLAUDE.md", "MEMORY.md", "nope.txt"):
            a.only = bad
            try:
                with quiet(): cmd_archive(a)
                raise AssertionError(f"archive should reject {bad}")
            except SystemExit:
                pass
        a.only = "missing.md"
        try:
            with quiet(): cmd_archive(a)
            raise AssertionError("archive should reject a missing fact")
        except SystemExit:
            pass
        a.only = "orphan.md"
        with quiet(): cmd_archive(a)
        assert not (memory / "orphan.md").exists() and (memory / "_archive" / "orphan.md").exists()
        assert not (vdir / "orphan.md").exists()
        assert "orphan.md" not in load_config(repo)["facts"]

        # restore refuses to clobber a live fact, then works
        (memory / "orphan.md").write_text("---\nname: orphan\ndescription: d\nmetadata:\n  type: project\n---\n\nlive\n")
        try:
            with quiet(): cmd_restore(a)
            raise AssertionError("restore should refuse to clobber a live fact")
        except SystemExit:
            pass
        (memory / "orphan.md").unlink()
        with quiet(): cmd_restore(a)
        a.only = None
        assert (memory / "orphan.md").exists() and (vdir / "orphan.md").exists()

        # sync settles
        with quiet(): cmd_sync(a)
        for k, it in gather(repo, memory, load_config(repo)).items():
            assert classify(it["repo_body"], it["vault_body"], it["rec"]) in ("no_change", "init"), k

        # frontmatter-only (description) edit is detected
        ur = memory / "user_role.md"
        ur.write_text(ur.read_text().replace("description: User is a tester", "description: User is a QA tester"))
        rec = gather(repo, memory, load_config(repo))["user_role.md"]
        assert classify(rec["repo_body"], rec["vault_body"], rec["rec"]) == "push"
        with quiet(): cmd_push(a)
        assert "QA tester" in vf.read_text()

        # A10/A9: a description with ':' and '#' emits valid, round-trippable YAML
        tricky = "Verdict: geometry beats AF #1"
        ur.write_text(ur.read_text().replace("description: User is a QA tester", f"description: {_q(tricky)}"))
        with quiet(): cmd_push(a)
        assert parse_vault_fact(vf.read_text(), "user_role")["description"] == tricky, "tricky scalar round-trips"
        rec = gather(repo, memory, load_config(repo))["user_role.md"]
        assert classify(rec["repo_body"], rec["vault_body"], rec["rec"]) == "no_change", "no phantom churn"

        # A2: a foreign hash_scheme is refused (not reported as 17 conflicts)
        m = json.loads((repo / CONFIG_REL).read_text()); m["hash_scheme"] = 1
        (repo / CONFIG_REL).write_text(json.dumps(m))
        try:
            load_config(repo)
            raise AssertionError("stale hash_scheme should be refused")
        except SystemExit:
            pass
        assert load_config(repo, require_scheme=False)["_scheme_mismatch"] == 1
        m["hash_scheme"] = HASH_SCHEME
        (repo / CONFIG_REL).write_text(json.dumps(m))

        # A3: --force must not invent a CLAUDE.md.md when the repo has none
        repo2, memory2, vault2, a2 = mkrepo("norepo_claude", claude_md=False)
        with quiet(): cmd_init(a2)
        a2.force = True
        with quiet(): cmd_push(a2)
        cfg2 = load_config(repo2)
        assert not (vault2 / cfg2["repo_subdir"] / "CLAUDE.md.md").exists(), "no junk CLAUDE.md.md on --force"
        assert not cfg2["claude_md"]["last_hash_repo"], "no phantom claude_md hash"

        # init auto-gitignores the marker in a git repo (the recurring chore)
        repo3, memory3, vault3, a3 = mkrepo("gitrepo")
        (repo3 / ".git").mkdir()
        with quiet(): cmd_init(a3)
        assert str(CONFIG_REL) in (repo3 / ".gitignore").read_text(), "marker auto-gitignored"
        assert ensure_gitignored(repo3).startswith("marker already"), "gitignore is idempotent"

        # map: generates the Atlas from discovered associations (scope to this repo only)
        a3.all = False; a3.roots = [str(repo3.parent)]
        with quiet(): cmd_map(a3)
        vr = vault_root(load_config(repo3)["vault_dir"])
        moc = vr / "_maps" / (vault_family(str(vr)) + ".md")
        assert moc.exists() and "Map of Content" in moc.read_text(), "map wrote a MOC"
        assert (vr / "_maps" / f"{load_config(repo3)['repo_subdir']}.md").exists(), "map wrote a repo hub"

        # doctor: readable vault passes; a permission-blocked vault fails with FDA guidance
        a3.fix = True
        with quiet(): cmd_doctor(a3)   # readable fake vault -> no FAIL, no SystemExit
        cfg3 = load_config(repo3)
        ok, reason = vault_readable(cfg3["vault_dir"])
        assert ok, "fake vault is readable"
        # simulate the TCC block on a dir we can't list
        blocked = tmp / "blocked"
        blocked.mkdir(); blocked.chmod(0o000)
        try:
            bok, breason = vault_readable(blocked)
            assert not bok and "Full Disk Access" in breason, "TCC block reported with FDA fix"
        finally:
            blocked.chmod(0o755)

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
    sp.add_argument("--force", action="store_true", help="re-link, dropping recorded hashes")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("status")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--memory")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("doctor")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--memory")
    sp.add_argument("--no-fix", dest="fix", action="store_false",
                    help="report only; don't auto-gitignore the marker")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("audit")
    sp.add_argument("--repo", required=True, help="audit this repo's vault family")
    sp.add_argument("--all", action="store_true", help="audit every vault, not just this repo's")
    sp.add_argument("--roots", nargs="*", help="dirs to scan for associations (default ~/vcs ~/work)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("map")
    sp.add_argument("--repo", required=True, help="generate the Atlas for this repo's vault family")
    sp.add_argument("--all", action="store_true", help="every vault, not just this repo's")
    sp.add_argument("--roots", nargs="*", help="dirs to scan for associations (default ~/vcs ~/work)")
    sp.add_argument("--enrich", action="store_true", help="also add Related-projects cross-links into facts (dry-run)")
    sp.add_argument("--apply", action="store_true", help="with --enrich: actually write the fact footers")
    sp.set_defaults(func=cmd_map)

    for name, fn in (("push", cmd_push), ("pull", cmd_pull)):
        sp = sub.add_parser(name)
        sp.add_argument("--repo", required=True)
        sp.add_argument("--memory")
        sp.add_argument("--only", help="narrow to one fact filename (e.g. foo.md) or 'claude_md'")
        sp.add_argument("--force", action="store_true", help="override conflicts / re-baseline")
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
    try:
        args.func(args)
    except PermissionError as e:
        sys.exit(f"permission denied: {e}\n"
                 f"The Obsidian vault is likely on iCloud — grant your terminal Full Disk Access.")


if __name__ == "__main__":
    main()
