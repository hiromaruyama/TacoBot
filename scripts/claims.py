#!/usr/bin/env python3
"""TacoBot claim tool — keeps parallel agents/branches from editing the same files.

    claims.py status                      who owns what right now
    claims.py check  [PATHS...|--changed] exit 1 if PATHS overlap another branch's claim or diff
    claims.py claim  PATHS... --task TXT  write coordination/claims/<branch>.toml
    claims.py release                     delete this branch's claim file
    claims.py sync                        git fetch + rebuild the cache used by the hook
    claims.py hook                        Claude Code PreToolUse hook (reads JSON on stdin)

"Another branch" = every remote branch on origin plus every local branch (worktrees), except
main and the current branch. For each we read (a) its own claim file and (b) the files it has
actually changed vs main, so even agents that forgot to claim are detected.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import getpass
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

MAIN = "main"
CLAIMS_DIR = "coordination/claims"
LANES_FILE = "coordination/lanes.toml"


# ---------------------------------------------------------------- git helpers
def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip() if r.returncode == 0 else ""


def repo_root() -> Path:
    return Path(git("rev-parse", "--show-toplevel"))


def current_branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD", check=False) or "HEAD"


def cache_path() -> Path:
    common = Path(git("rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = repo_root() / common
    return common / "tacobot-claims.json"


def base_ref() -> str:
    for ref in (f"origin/{MAIN}", MAIN):
        if git("rev-parse", "--verify", "--quiet", ref, check=False):
            return ref
    return "HEAD"


def claim_file_for(branch: str) -> str:
    return f"{CLAIMS_DIR}/{branch.replace('/', '__')}.toml"


def other_branches(me: str) -> dict[str, str]:
    """branch name -> ref to read it from. Remote wins over local for the same name."""
    out: dict[str, str] = {}
    for line in git("for-each-ref", "--format=%(refname:short)", "refs/heads", check=False).splitlines():
        out[line] = line
    for line in git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin", check=False).splitlines():
        name = line.removeprefix("origin/")
        if name in ("HEAD", "origin"):
            continue
        out[name] = line
    for skip in (MAIN, me, "HEAD"):
        out.pop(skip, None)
    return out


# ---------------------------------------------------------------- lanes / patterns
def load_lanes(root: Path) -> dict:
    p = root / LANES_FILE
    return tomllib.loads(p.read_text()) if p.exists() else {"shared": [], "free": [], "lanes": {}}


def matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2])):
        return True
    return fnmatch.fnmatch(path, pattern)


def patterns_overlap(a: str, b: str, files: list[str]) -> bool:
    """Do two glob patterns (or plain paths) cover any common file?"""
    if a == b or matches(a, b) or matches(b, a):
        return True
    return any(matches(f, a) and matches(f, b) for f in files)


def lane_of(branch: str, lanes: dict) -> str | None:
    for name, spec in lanes.get("lanes", {}).items():
        if branch.startswith(spec.get("prefix", f"{name}/")):
            return name
    return None


# ---------------------------------------------------------------- index of other branches
def read_claim(ref: str, branch: str) -> dict | None:
    txt = git("show", f"{ref}:{claim_file_for(branch)}", check=False)
    if not txt:
        return None
    try:
        return tomllib.loads(txt)
    except tomllib.TOMLDecodeError:
        return None


def build_index(fetch: bool) -> dict:
    if fetch:
        git("fetch", "--prune", "--quiet", "origin", check=False)
    me = current_branch()
    base = base_ref()
    idx: dict = {"built": dt.datetime.now().isoformat(timespec="seconds"), "me": me, "branches": {}}
    for branch, ref in other_branches(me).items():
        claim = read_claim(ref, branch)
        changed = [f for f in git("diff", "--name-only", f"{base}...{ref}", check=False).splitlines()
                   if f and not f.startswith(CLAIMS_DIR + "/")]
        if not claim and not changed:
            continue  # merged or empty branch
        idx["branches"][branch] = {
            "claim": (claim or {}).get("paths", []),
            "task": (claim or {}).get("task", ""),
            "owner": (claim or {}).get("owner", ""),
            "changed": changed,
        }
    try:
        cache_path().write_text(json.dumps(idx, indent=2))
    except OSError:
        pass
    return idx


def load_index() -> dict:
    p = cache_path()
    if p.exists():
        idx = json.loads(p.read_text())
        if idx.get("me") == current_branch():
            return idx
    return build_index(fetch=False)


def conflicts(paths: list[str], idx: dict, files: list[str]) -> list[str]:
    msgs = []
    for branch, info in idx["branches"].items():
        who = f"{branch}" + (f" ({info['owner']}: {info['task']})" if info.get("task") else "")
        for p in paths:
            for c in info["claim"]:
                if patterns_overlap(p, c, files):
                    msgs.append(f"  {p}  ⟷  claimed as '{c}' by {who}")
            for c in info["changed"]:
                if matches(c, p) or c == p:
                    msgs.append(f"  {p}  ⟷  already modified ('{c}') on {who}")
    return sorted(set(msgs))


def tracked_files() -> list[str]:
    return git("ls-files", check=False).splitlines()


def my_changed_files() -> list[str]:
    base = base_ref()
    files = set(git("diff", "--name-only", f"{base}...HEAD", check=False).splitlines())
    files |= set(git("diff", "--name-only", "HEAD", check=False).splitlines())
    return sorted(f for f in files if f and not f.startswith(CLAIMS_DIR + "/"))


# ---------------------------------------------------------------- commands
def cmd_status(args) -> int:
    idx = build_index(fetch=not args.no_fetch)
    print(f"🌮 TacoBot claims — you are on '{idx['me']}'")
    if idx["me"] == MAIN:
        print("   ⚠  You are on main. Create a branch before editing: git checkout -b <lane>/<task>")
    if not idx["branches"]:
        print("   No other active branches. Everything is free.")
    for b, info in idx["branches"].items():
        print(f"\n  ▸ {b}  {('— ' + info['task']) if info['task'] else '(no claim file!)'}")
        for c in info["claim"]:
            print(f"      claims   {c}")
        for c in info["changed"][:15]:
            print(f"      changed  {c}")
    return 0


def cmd_check(args) -> int:
    idx = build_index(fetch=not args.no_fetch)
    paths = my_changed_files() if args.changed else args.paths
    if not paths:
        print("Nothing to check.")
        return 0
    found = conflicts(paths, idx, tracked_files())
    if found:
        print("❌ Overlap with other branches — coordinate before editing:")
        print("\n".join(found))
        return 1
    print(f"✅ No overlap for {len(paths)} path(s).")
    return 0


def write_claim(path: Path, data: dict) -> None:
    q = lambda s: json.dumps(s)  # JSON string quoting is valid TOML basic-string quoting
    lines = [f"{k} = {q(v)}" for k, v in data.items() if k != "paths"]
    lines.append("paths = [\n" + "".join(f"  {q(p)},\n" for p in data["paths"]) + "]")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def cmd_claim(args) -> int:
    root, me = repo_root(), current_branch()
    if me in (MAIN, "HEAD"):
        print("❌ Don't claim on main. git checkout -b <lane>/<task> first.")
        return 1
    lanes = load_lanes(root)
    lane = lane_of(me, lanes)
    if lane is None:
        print(f"❌ Branch '{me}' has no lane prefix. Use one of: "
              + ", ".join(s['prefix'] for s in lanes['lanes'].values()))
        return 1
    idx = build_index(fetch=not args.no_fetch)
    found = conflicts(args.paths, idx, tracked_files())
    if found and not args.force:
        print("❌ Can't claim — overlaps:\n" + "\n".join(found))
        return 1
    lane_paths = lanes["lanes"][lane].get("paths", [])
    for p in args.paths:
        if any(patterns_overlap(p, s, []) for s in lanes.get("readonly", [])):
            print(f"❌ {p} is read-only upstream code. Don't claim or edit it.")
            return 1
        if any(matches(p, s) or p == s for s in lanes.get("shared", [])) and lane != "shared":
            print(f"❌ {p} is SHARED. Change it on a separate shared/<what> branch.")
            return 1
        if lane_paths and not any(patterns_overlap(p, lp, tracked_files()) for lp in lane_paths):
            print(f"⚠  {p} is outside lane '{lane}'. Allowed, but mention it in your PR.")
    f = root / claim_file_for(me)
    old = tomllib.loads(f.read_text()) if f.exists() else {}
    data = {
        "branch": me,
        "lane": lane,
        "task": args.task or old.get("task", ""),
        "owner": args.owner or old.get("owner") or os.environ.get("TACOBOT_AGENT") or getpass.getuser(),
        "created": old.get("created") or dt.datetime.now().isoformat(timespec="minutes"),
        "paths": sorted(set(old.get("paths", [])) | set(args.paths)),
    }
    write_claim(f, data)
    print(f"✅ Claimed {len(data['paths'])} path(s) in {f.relative_to(root)}")
    print("   Now: git add coordination/claims && git commit -m 'claim: …' && git push -u origin HEAD")
    return 0


def cmd_release(args) -> int:
    f = repo_root() / claim_file_for(current_branch())
    if f.exists():
        f.unlink()
        print(f"✅ Released {f.name}. Commit and push.")
    else:
        print("No claim file for this branch.")
    return 0


def cmd_sync(args) -> int:
    idx = build_index(fetch=True)
    print(f"🔄 Synced {len(idx['branches'])} active branch(es).")
    mine = my_changed_files()
    found = conflicts(mine, idx, tracked_files()) if mine else []
    if found:
        print("⚠  Your changes overlap other branches:\n" + "\n".join(found))
        return 1
    return 0


def cmd_hook(args) -> int:
    """PreToolUse hook. exit 2 = block the edit and show stderr to Claude."""
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    fp = (payload.get("tool_input") or {}).get("file_path") or (payload.get("tool_input") or {}).get("notebook_path")
    if not fp:
        return 0
    root = repo_root()
    try:
        rel = str(Path(fp).resolve().relative_to(root.resolve()))
    except ValueError:
        return 0  # outside repo
    me = current_branch()
    lanes = load_lanes(root)
    if any(matches(rel, p) for p in lanes.get("readonly", [])):
        print(f"BLOCKED: {rel} is read-only (upstream portal-makermods code, see AGENTS.md §3). Don't edit it; "
              f"put TacoBot changes in tacobot/ or ask the human to propose the fix upstream.", file=sys.stderr)
        return 2
    if any(matches(rel, p) for p in lanes.get("free", [])):
        return 0
    if me == MAIN:
        print(f"BLOCKED: you're on main. Create a branch first: git checkout -b <lane>/<task>, "
              f"then run python scripts/claims.py claim {rel}", file=sys.stderr)
        return 2
    if lane_of(me, lanes) != "shared" and any(matches(rel, p) for p in lanes.get("shared", [])):
        print(f"BLOCKED: {rel} is a SHARED file (see AGENTS.md §3). Change it on a separate "
              f"shared/<what> branch, or ask the human.", file=sys.stderr)
        return 2
    found = conflicts([rel], load_index(), [])
    if found:
        print("BLOCKED: another branch owns this file:\n" + "\n".join(found) +
              "\nDo not work around this. Pick other work or tell the human. "
              "(If you think the info is stale: python scripts/claims.py sync)", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-fetch", action="store_true", help="don't git fetch (offline)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    c = sub.add_parser("check")
    c.add_argument("paths", nargs="*")
    c.add_argument("--changed", action="store_true", help="check files this branch changed")
    c = sub.add_parser("claim")
    c.add_argument("paths", nargs="+")
    c.add_argument("--task", default="")
    c.add_argument("--owner", default="")
    c.add_argument("--force", action="store_true", help="claim despite overlap (needs owner's OK)")
    sub.add_parser("release")
    sub.add_parser("sync")
    sub.add_parser("hook")
    args = ap.parse_args(argv)
    return {"status": cmd_status, "check": cmd_check, "claim": cmd_claim,
            "release": cmd_release, "sync": cmd_sync, "hook": cmd_hook}[args.cmd](args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"claims.py: {e}", file=sys.stderr)
        sys.exit(0 if "hook" in sys.argv else 1)
