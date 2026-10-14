#!/usr/bin/env python3
"""Install lightweight CI and its owner entrypoint link; never execute verification commands."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

from check_repositories import GitHub, ReadError, SECTION, declared_ci_links, declared_links, section


WORKFLOW = ".github/workflows/ci.yml"
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
PYTHON = "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"
NODE = "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1"


def render(commands, paths, branch="main", python=None, node=None, timeout=5):
    if not commands or any(not c.strip() for c in commands):
        raise ValueError("explicit nonempty owner verification commands are required")
    if not paths or any(not p or p.startswith(("/", "!")) or ".." in p.split("/") for p in paths):
        raise ValueError("explicit relative positive change paths are required")
    if not 1 <= timeout <= 10:
        raise ValueError("lightweight CI timeout must be 1..10 minutes")
    if python and node:
        raise ValueError("choose one toolchain; review a custom workflow for multiple toolchains")
    if any(v and not re.fullmatch(r"\d+(?:\.\d+){0,2}", v) for v in (python, node)):
        raise ValueError("toolchain versions must be explicit numeric versions")
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    paths = list(dict.fromkeys([*paths, WORKFLOW, "README.md", "AGENTS.md"]))
    lines = ["name: CI", "", "on:"]
    for event in ("push", "pull_request"):
        lines += [f"  {event}:", f"    branches: [{quote(branch)}]", "    paths:"]
        lines += ["      - " + quote(p) for p in paths]
    lines += ["", "permissions:", "  contents: read", "", "concurrency:",
              "  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
              "  cancel-in-progress: true", "", "jobs:", "  verify:", "    runs-on: ubuntu-latest",
              f"    timeout-minutes: {timeout}", "    steps:", "      - uses: " + CHECKOUT,
              "        with:", "          persist-credentials: false"]
    if python or node:
        lines += ["      - uses: " + (PYTHON if python else NODE), "        with:",
                  "          " + ("python-version" if python else "node-version") + ": " + quote(python or node)]
        if node:
            lines += ["          package-manager-cache: false"]
    lines += ["      - name: Run owner verification", "        run: " + quote("\n".join(commands))]
    return "\n".join(lines) + "\n"


def install(repository, entrypoint, workflow, github):
    root = Path(repository).resolve()
    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, timeout=10)
        if result.returncode:
            raise ValueError("target Git identity could not be verified")
        return result.stdout.strip()
    if Path(git("rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("target must be the repository root")
    if not re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)meenseek/[A-Za-z0-9_.-]+/?",
                        git("remote", "get-url", "origin"), re.I):
        raise ValueError("target origin must identify a meenseek GitHub repository")
    if entrypoint not in {"README.md", "AGENTS.md"}:
        raise ValueError("choose the existing README.md or AGENTS.md owner entrypoint")
    source = root / entrypoint
    for name in ("README.md", "AGENTS.md"):
        p = root / name
        if p.is_symlink():
            raise ValueError("entrypoint symlinks are not supported")
        if p.exists() and (not p.is_file() or p.stat().st_size > 1024 * 1024):
            raise ValueError("entrypoint must be a bounded regular UTF-8 file")
    before = source.read_bytes()
    original = before.decode("utf-8")
    other = root / ("AGENTS.md" if entrypoint == "README.md" else "README.md")
    if section(original) is None or (other.exists() and section(other.read_text()) is not None):
        raise ValueError("declare connections in one existing owner entrypoint before installing CI")
    matches = list(SECTION.finditer(original))
    if len(matches) != 1:
        raise ValueError("owner section layout needs manual assessment")
    updated = original if WORKFLOW in declared_ci_links(section(original)) else (
        original[:matches[0].end()] + f"\n\nCI: [자동 검증]({WORKFLOW})\n" + original[matches[0].end():])
    if WORKFLOW not in declared_ci_links(section(updated)):
        raise ValueError("CI link would not be discoverable in the owner section")
    destination = root / WORKFLOW
    for directory in (root / ".github", destination.parent):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError("workflow directory must be local to the target repository")
    if os.path.lexists(destination):
        raise ValueError("existing CI is preserved; review its owner configuration instead")
    if destination.parent.exists() and any(p.suffix in {".yml", ".yaml"} for p in destination.parent.iterdir()):
        raise ValueError("existing workflows are preserved; assess CI with their owner before adding another")
    budget = github.actions_budget("meenseek")
    if budget.get("status") != "confirmed":
        raise ValueError("Actions zero-dollar stop is unverified or disabled; no files were installed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = []
    installed = False
    try:
        for parent, data, mode in ((destination.parent, workflow.encode(), 0o644),
                                   (root, updated.encode(), stat.S_IMODE(source.stat().st_mode))):
            fd, filename = tempfile.mkstemp(prefix=".temporary-ci-", dir=parent)
            temporary.append(Path(filename))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(filename, mode)
        os.link(temporary[0], destination)  # Exclusive create, including concurrent CI installs.
        installed = True
        if source.is_symlink() or source.read_bytes() != before:
            raise ValueError("owner entrypoint changed; CI installation rolled back")
        os.replace(temporary[1], source)
    except Exception:
        if installed:
            destination.unlink()
        raise
    finally:
        for filename in temporary:
            filename.unlink(missing_ok=True)
    return {"workflow": str(destination), "entrypoint": str(source), "actions_budget": budget,
            "workflow_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "next": "Review and commit both files; publish under existing approval, then require exact-commit hosted CI success"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--entrypoint", choices=["README.md", "AGENTS.md"], required=True)
    parser.add_argument("--command", action="append", required=True, help="Repeat for owner install/check commands, in order")
    parser.add_argument("--path", action="append", required=True, help="Repeat for code/config/lockfile inputs; no negative patterns")
    parser.add_argument("--branch", default="main", help="Actual target default branch")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--python-version")
    group.add_argument("--node-version")
    parser.add_argument("--timeout-minutes", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true", help="Render only; no files, Git mutation or network requests")
    args = parser.parse_args()
    try:
        workflow = render(args.command, args.path, args.branch, args.python_version, args.node_version, args.timeout_minutes)
        if args.dry_run:
            print(workflow, end="")
        else:
            print(json.dumps(install(args.repository, args.entrypoint, workflow, GitHub()), ensure_ascii=False, indent=2))
    except (ValueError, ReadError, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
