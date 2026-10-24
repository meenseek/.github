"""Read-only task completion observations; Python 3.11+, no model or daemon.

The host freezes the scope SHA and original request identity. Hashes establish
identity, not permission, scope adequacy, reviewer independence or quality.
Those judgments belong to the existing owner and independent review boundary.
No command from an input document is executed. Unsupported observations fail.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import ssl
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler, HTTPSHandler


CATEGORIES = ("source", "usage", "deployment", "quality", "artifacts",
              "git_resources", "temporary_resources", "recovery")
MAX_BYTES = 1024 * 1024
MAX_FILES = 4096
TIMEOUT = 5
CLEANUP_KINDS = {"absent", "ref_absent", "remote_ref_absent", "worktree_absent", "process_absent"}
KINDS = {"git_source", "installed_file", "file", "absent", "ref_absent", "remote_ref_absent",
         "worktree_absent", "process_absent", "http_json", "evidence", "improvement"}


class CompletionError(ValueError):
    pass


def improvement_observer():
    spec = importlib.util.spec_from_file_location("completion_improvement", Path(__file__).with_name("check_improvement.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise CompletionError("unexpected or missing fields")


def text(value):
    if not isinstance(value, str) or not value.strip() or "\0" in value or len(value.encode()) > 4096:
        raise CompletionError("expected bounded nonempty text")
    return value


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise CompletionError("expected SHA-256")
    return value


def validate_ref_observer(kind, value):
    ref = text(value)
    if ref.startswith("refs/heads/") or (kind == "ref_absent" and ref == "refs/stash"):
        return ref
    raise CompletionError("only exact branch refs and local refs/stash are supported")


def path(value):
    value = Path(text(value))
    if not value.is_absolute() or ".." in value.parts or str(value).startswith("//"):
        raise CompletionError("paths must be absolute and normalized")
    return value


def raw_file(value, limit=MAX_BYTES, links=False):
    value = path(str(value))
    if not links:
        for part in (value, *value.parents):
            if part.is_symlink():
                raise CompletionError("symlink input refused")
    flags = os.O_RDONLY | os.O_NONBLOCK | (0 if links else os.O_NOFOLLOW)
    fd = os.open(value, flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit or (not links and info.st_nlink != 1):
            raise CompletionError("input must be a bounded regular file with one link")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise CompletionError("input too large")
    return raw


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CompletionError("duplicate JSON key")
        result[key] = value
    return result


def decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(CompletionError("non-finite JSON")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CompletionError("invalid UTF-8 JSON") from exc


def reference(ref):
    fields(ref, ("path", "sha256"))
    if sha(raw_file(ref["path"])) != digest(ref["sha256"]):
        raise CompletionError("source bytes changed: " + ref["path"])
    return ref


def read_reference(ref):
    """Parse exactly the one raw buffer whose digest was verified."""
    fields(ref, ("path", "sha256"))
    raw = raw_file(ref["path"])
    if sha(raw) != digest(ref["sha256"]):
        raise CompletionError("source bytes changed: " + ref["path"])
    return decode(raw)


def refs(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise CompletionError("expected 1..32 source references")
    for ref in values:
        reference(ref)
    return values


def observer_env():
    # Git config/SSH/proxy injection is not inherited. No input-selected executable.
    return {"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "HOME": os.environ.get("HOME", ""),
            "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1",
            "GH_PAGER": "cat"}


def run(argv, cwd=None, timeout=TIMEOUT):
    # Spool output to bounded-on-read temporary files, avoiding unbounded pipe RAM.
    import tempfile
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        try:
            done = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                                  timeout=timeout, env=observer_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CompletionError("observation unavailable or timed out") from exc
        output.seek(0)
        raw = output.read(MAX_BYTES + 1)
        if done.returncode or len(raw) > MAX_BYTES:
            raise CompletionError("observation failed or exceeded output limit")
        return raw


def git(repo, *args):
    return run([shutil.which("git", path=observer_env()["PATH"]), "--no-pager",
                "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                "-C", str(path(repo)), *args])


def source_stream_sha(stream):
    """Hash source bytes in chunks; source files have no metadata size cap."""
    hasher = hashlib.sha256()
    while chunk := stream.read(64 * 1024):
        hasher.update(chunk)
    return hasher.hexdigest()


def git_source_sha(repo, revision, filename):
    import tempfile
    argv = [shutil.which("git", path=observer_env()["PATH"]), "--no-pager",
            "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
            "-C", str(path(repo)), "show", "--no-ext-diff", "--no-textconv",
            revision + ":" + filename]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        try:
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=output,
                                  stderr=error, timeout=TIMEOUT, env=observer_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CompletionError("source observation unavailable or timed out") from exc
        if done.returncode:
            raise CompletionError("source observation failed")
        output.seek(0)
        return source_stream_sha(output)


def working_source_sha(value):
    # Preserve git_source's existing links=True working-tree semantics.
    fd = os.open(path(str(value)), os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise CompletionError("working source must be a regular file")
        return source_stream_sha(stream)


def git_source_present(repo, revision, filename):
    # A successful lookup in a valid revision distinguishes deletion from Git errors.
    return bool(git(repo, "--literal-pathspecs", "ls-tree", "-z", revision, "--", filename))


def repo_identity(repo):
    root = path(repo)
    if root.resolve() != root:
        raise CompletionError("repository path must be canonical")
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()).resolve()
    info = common.stat()
    return {"common_dir": str(common), "device": info.st_dev, "inode": info.st_ino}


def remote_identity(repo, remote):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", text(remote)):
        raise CompletionError("invalid named remote")
    location = git(repo, "remote", "get-url", remote).decode().strip()
    # No Git network transport is executed: no helpers, SSH/credential commands,
    # ext transport or insteadOf rewrites can become an observer command.
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?", location)
    if match:
        return {"url": location, "kind": "github", "repository": match[1] + "/" + match[2], "local": repo_identity(repo)}
    candidate = Path(location)
    if candidate.is_absolute() and candidate.resolve() == candidate:
        return {"url": location, "kind": "local", "local": repo_identity(repo), "destination": repo_identity(location)}
    raise CompletionError("unsupported remote: only canonical local Git or GitHub HTTPS/SSH identity is observed")


def remote_ref(repo, remote, ref, binding):
    if remote_identity(repo, remote) != binding:
        raise CompletionError("approved repository or remote identity changed")
    if not re.fullmatch(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*", text(ref)) or ".." in ref:
        raise CompletionError("invalid exact branch ref")
    if binding["kind"] == "local":
        values = git(binding["url"], "for-each-ref", "--format=%(refname) %(objectname)", ref).decode().splitlines()
        return next((line.split()[1] for line in values if line.split()[0] == ref), None)
    # gh api is a fixed authenticated read, not an input-selected command or URL.
    executable = shutil.which("gh", path=observer_env()["PATH"])
    if not executable:
        raise CompletionError("GitHub observer unavailable")
    endpoint = "repos/" + binding["repository"] + "/git/matching-refs/heads/" + ref[len("refs/heads/"):]
    values = decode(run([executable, "api", "--hostname", "github.com", "--method", "GET", endpoint]))
    if not isinstance(values, list):
        raise CompletionError("GitHub ref observation malformed")
    return next((item["object"]["sha"] for item in values if item.get("ref") == ref), None)


def environment(workspace):
    binaries = {name: shutil.which(name, path=observer_env()["PATH"]) for name in ("git", "gh", "ps")}
    ca = ssl.get_default_verify_paths().openssl_cafile
    return {"https_ca": {"path": ca, "sha256": sha(raw_file(ca, limit=128*MAX_BYTES, links=True))} if ca and Path(ca).is_file() else None,
            "workspace": str(path(workspace)), "python": str(Path(sys.executable).resolve()),
            "python_version": sys.version, "binaries": binaries,
            "binary_sha256": {k: sha(raw_file(v, limit=128*MAX_BYTES, links=True)) if v else None for k,v in binaries.items()},
            "settings_sha256": sha(canonical(observer_env()))}


def inventory(repo):
    dirty_files = set(git(repo, "ls-files", "-z", "--modified", "--others", "--exclude-standard").decode().split("\0"))
    dirty_files.update(git(repo, "diff", "--cached", "--name-only", "-z", "--no-ext-diff", "--no-textconv").decode().split("\0"))
    dirty_files = sorted(dirty_files - {""})
    index = {}
    for start in range(0, len(dirty_files), 32):
        records = git(repo, "--literal-pathspecs", "ls-files", "--stage", "-z", "--", *dirty_files[start:start + 32])
        for record in records.decode().split("\0"):
            if record:
                metadata, filename = record.split("\t", 1)
                index.setdefault(filename, []).append(metadata)
    dirty_bytes = {}
    for filename in dirty_files:
        entry = path(repo) / filename
        dirty_bytes[filename] = {"worktree": file_identity(entry) if os.path.lexists(entry) else None,
                                 "index": index.get(filename, [])}
    return {
        "identity": repo_identity(repo),
        "refs": dict(line.split(" ", 1) for line in git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/remotes").decode().splitlines()),
        "worktrees": git(repo, "worktree", "list", "--porcelain").decode().split("\n\n"),
        "dirty": git(repo, "status", "--ignore-submodules=all", "--porcelain=v1", "-z", "--untracked-files=all").decode().split("\0"),
        "dirty_bytes": dirty_bytes,
        "stash": git(repo, "stash", "list", "--format=%H").decode().splitlines(),
    }


def file_identity(entry):
    if entry.is_symlink():
        return {"symlink": os.readlink(entry)}
    return {"sha256": sha(raw_file(str(entry), limit=128*MAX_BYTES))}


def tree(root):
    root = path(root)
    if not os.path.lexists(root):
        return {}
    if root.resolve() != root or root.is_symlink() or not root.is_dir():
        raise CompletionError("artifact root must be a real directory")
    files = {str(root): {"directory": True}}
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in (*dirs, *names):
            entry = Path(directory) / name
            if entry.is_symlink() or entry.is_file():
                files[str(entry)] = file_identity(entry)
            elif entry.is_dir():
                files[str(entry)] = {"directory": True}
            else:
                raise CompletionError("unsupported filesystem resource type")
            if len(files) > MAX_FILES:
                raise CompletionError("artifact inventory exceeds limit")
    return dict(sorted(files.items()))


def owner_refs(values, recovery=None):
    """Read live contracts or the preserved original of a planned edit."""
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise CompletionError("expected 1..32 owner references")
    for ref in values:
        fields(ref, ("path", "sha256"), ("original",))
        if "original" not in ref:
            current = recovery.get(ref["path"]) if recovery else None
            reference({"path": ref["path"], "sha256": current} if current else ref)
            continue
        original = ref["original"]
        fields(original, ("repository", "file", "identity"), ("revision", "snapshot"))
        if ("revision" in original) == ("snapshot" in original):
            raise CompletionError("owner original requires exactly one Git revision or snapshot")
        relative = Path(text(original["file"]))
        if relative.is_absolute() or ".." in relative.parts or str(relative).startswith(":"):
            raise CompletionError("invalid owner source path")
        if path(ref["path"]) != path(original["repository"]) / relative:
            raise CompletionError("owner source identity differs")
        if repo_identity(original["repository"]) != original["identity"]:
            raise CompletionError("owner repository identity changed")
        if "snapshot" in original:
            snapshot = reference(original["snapshot"])
            if path(snapshot["path"]).is_relative_to(path(original["repository"])):
                raise CompletionError("owner snapshot must be preserved outside its source repository")
            if snapshot["sha256"] != digest(ref["sha256"]):
                raise CompletionError("owner snapshot differs from the frozen original bytes")
        else:
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text(original["revision"])):
                raise CompletionError("owner original requires a full Git revision")
            raw = git(original["repository"], "show", "--no-ext-diff", "--no-textconv", original["revision"] + ":" + original["file"])
            if sha(raw) != digest(ref["sha256"]):
                raise CompletionError("owner original bytes changed")
    return values


def current_owners(owners, updates, recovery=None):
    if not isinstance(updates, dict):
        raise CompletionError("owner updates must be exact path/digest expectations")
    editable = {ref["path"] for ref in owners if "original" in ref}
    if updates.keys() - editable:
        raise CompletionError("owner update was not declared in the frozen source obligations")
    owner_refs(owners, recovery)
    for ref in owners:
        default = recovery.get(ref["path"], ref["sha256"]) if recovery else ref["sha256"]
        wanted = updates.get(ref["path"], default)
        if wanted is None:
            if os.path.lexists(path(ref["path"])):
                raise CompletionError("reviewed deleted owner reappeared")
        else:
            reference({"path": ref["path"], "sha256": wanted})


def owner_recovery(scope_ref, scope, binding):
    """Reconcile exact reviewed owner drifts without replacing the frozen scope."""
    fields(binding, ("evidence", "review"))
    evidence = read_reference(binding["evidence"])
    fields(evidence, ("scope_sha256", "task_id", "session_id", "authorization", "owners"))
    if (evidence["scope_sha256"], evidence["task_id"], evidence["session_id"]) != (scope_ref["sha256"], scope["task_id"], scope["session_id"]):
        raise CompletionError("owner recovery belongs to another frozen task")
    reference(evidence["authorization"])
    reviewed = read_reference(binding["review"])
    if reviewed != {"scope_sha256": scope_ref["sha256"], "recovery_sha256": binding["evidence"]["sha256"], "status": "No Findings"}:
        raise CompletionError("independent recovery review does not cover these exact bytes")
    if not isinstance(evidence["owners"], list) or not 1 <= len(evidence["owners"]) <= len(scope["owners"]):
        raise CompletionError("recovery requires a bounded list of frozen owners")
    recovered = {}
    for owner in evidence["owners"]:
        fields(owner, ("owner_path", "repository", "identity", "original", "current", "source"))
        selected = [ref for ref in scope["owners"] if ref["path"] == owner["owner_path"]]
        if len(selected) != 1 or "original" in selected[0] or owner["owner_path"] in recovered:
            raise CompletionError("recovery requires unique unchanged frozen owners; planned edits use owner_updates")
        if repo_identity(owner["repository"]) != owner["identity"]:
            raise CompletionError("recovery owner repository identity changed")
        for key in ("original", "current"):
            value = owner[key]
            fields(value, ("revision", "file", "sha256"))
            owner_refs([{"path": owner["owner_path"], "sha256": value["sha256"], "original": {
                "repository": owner["repository"], "revision": value["revision"], "file": value["file"], "identity": owner["identity"]}}])
        if owner["original"]["sha256"] != selected[0]["sha256"] or owner["original"]["file"] != owner["current"]["file"]:
            raise CompletionError("recovery changed the original owner contract identity")
        source = owner["source"]
        fields(source, ("definition", "expected"))
        fields(source["definition"], ("id", "category", "kind", "target"))
        target = source["definition"]["target"]
        if (source["definition"]["kind"] != "git_source" or source["definition"]["category"] != "source"
                or target.get("repository") != owner["repository"] or owner["current"]["file"] not in target.get("files", [])):
            raise CompletionError("recovery requires the reviewed current owner in canonical source")
        if source["expected"].get("files", {}).get(owner["current"]["file"]) != owner["current"]["sha256"]:
            raise CompletionError("recovery source bytes differ from the reviewed owner")
        observe(source["definition"], source["expected"])
        reference({"path": owner["owner_path"], "sha256": owner["current"]["sha256"]})
        recovered[owner["owner_path"]] = owner["current"]["sha256"]
    return recovered


def source_preservation(scope_ref, scope, binding):
    """Bind exact pre-existing foreign regions without replacing source obligations."""
    fields(binding, ("evidence", "review"))
    evidence = read_reference(binding["evidence"])
    fields(evidence, ("scope_sha256", "task_id", "session_id", "previous_state_sha256", "authorization", "reason", "files"))
    if (evidence["scope_sha256"], evidence["task_id"], evidence["session_id"]) != (scope_ref["sha256"], scope["task_id"], scope["session_id"]):
        raise CompletionError("source preservation differs from the frozen scope/task/session")
    digest(evidence["previous_state_sha256"]); reference(evidence["authorization"]); text(evidence["reason"])
    if read_reference(binding["review"]) != {"scope_sha256": scope_ref["sha256"],
            "source_preservation_sha256": binding["evidence"]["sha256"], "status": "No Findings"}:
        raise CompletionError("source preservation requires review of the exact scope and evidence")
    files = evidence["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= 32:
        raise CompletionError("expected 1..32 preserved source files")
    preserved = {}
    for entry in files:
        fields(entry, ("check_id", "file", "owner", "basis", "ranges"))
        text(entry["owner"]); refs(entry["basis"])
        selected = [check for check in scope["checks"] if check["id"] == entry["check_id"] and check["kind"] == "git_source"]
        if len(selected) != 1 or selected[0]["category"] != "source":
            raise CompletionError("preservation requires an existing source obligation")
        target = selected[0]["target"]
        repo, filename = target["repository"], text(entry["file"])
        if filename not in target["files"] or filename in preserved.get(entry["check_id"], {}):
            raise CompletionError("preservation changed file coverage or repeated a file")
        owners = [owner for owner in scope["owners"] if owner.get("original", {}).get("repository") == repo
                  and owner["original"]["file"] == filename and "snapshot" in owner["original"]]
        if len(owners) != 1:
            raise CompletionError("preservation requires the existing original owner snapshot")
        owner = owners[0]
        owner_refs([owner])
        before = scope["baseline"]["repositories"].get(repo, {})
        if before.get("dirty_bytes", {}).get(filename, {}).get("worktree") != {"sha256": owner["sha256"]}:
            raise CompletionError("preserved owner was not the captured starting dirty file")
        mode, indexed = index_source_bytes(repo, before["dirty_bytes"][filename]["index"])
        revision = before["refs"].get("refs/heads/" + target["main"])
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
            raise CompletionError("preservation requires the captured main revision")
        base = git(repo, "show", "--no-ext-diff", "--no-textconv", revision + ":" + filename)
        original = raw_file(owner["original"]["snapshot"]["path"])
        if sha(original) != owner["sha256"]:
            raise CompletionError("preserved original owner bytes changed")
        ranges = entry["ranges"]
        if not isinstance(ranges, list) or not 1 <= len(ranges) <= 32:
            raise CompletionError("expected 1..32 exact preserved byte ranges")
        pairs, staged, ends = [], [], {"base": 0, "snapshot": 0}
        for region in ranges:
            fields(region, ("base", "snapshot"))
            fragments = []
            for name, raw in (("base", base), ("snapshot", original)):
                interval = region[name]
                if (not isinstance(interval, list) or len(interval) != 2 or any(type(i) is not int for i in interval)
                        or not ends[name] <= interval[0] < interval[1] <= len(raw)):
                    raise CompletionError("preserved ranges must be ordered, nonempty and disjoint")
                fragment = raw[interval[0]:interval[1]]
                if raw.find(fragment) != interval[0] or raw.rfind(fragment) != interval[0]:
                    raise CompletionError("preserved range requires unique exact context")
                fragments.append(fragment); ends[name] = interval[1]
            if fragments[0] == fragments[1]:
                raise CompletionError("preserved range must identify an existing difference")
            pairs.append(tuple(fragments))
            old, new = fragments
            old_at, new_at = indexed.find(old), indexed.find(new)
            if new_at >= 0 and new_at == indexed.rfind(new):
                if old_at >= 0 and not (new_at <= old_at and indexed.rfind(old) + len(old) <= new_at + len(new)):
                    raise CompletionError("captured index has ambiguous preserved context")
                staged.append((old, new))
            elif old_at < 0 or old_at != indexed.rfind(old) or new_at >= 0:
                raise CompletionError("captured index has partial or ambiguous preserved staging")
        if preserved_source_bytes(base, staged) != indexed:
            raise CompletionError("starting index has undeclared changes outside preserved regions")
        preserved.setdefault(entry["check_id"], {})[filename] = {"working": pairs, "index": staged, "mode": mode}
    return preserved


def index_source_bytes(repo, entries):
    if not isinstance(entries, list) or len(entries) != 1:
        raise CompletionError("preservation requires one stage-zero index entry")
    metadata = re.fullmatch(r"(100644|100755) ([0-9a-f]{40}|[0-9a-f]{64}) 0", text(entries[0]))
    if metadata is None:
        raise CompletionError("preservation requires a regular stage-zero index entry")
    return metadata[1], git(repo, "cat-file", "blob", metadata[2])


def preserved_source_bytes(published, pairs):
    """Apply only exact, unique regions; never infer ownership or use fuzzy patches."""
    result, end = bytearray(), 0
    for before, after in pairs:
        start = published.find(before)
        if start < end or published.rfind(before) != start:
            raise CompletionError("preserved source region is absent, ambiguous or overlapping")
        result.extend(published[end:start]); result.extend(after)
        end = start + len(before)
    result.extend(published[end:])
    if len(result) > MAX_BYTES:
        raise CompletionError("preserved owner exceeds the bounded owner size")
    return bytes(result)


def validate_scope(scope, recovery=None):
    fields(scope, ("task_id", "session_id", "request", "request_digest", "owners", "categories", "checks",
                   "repositories", "artifact_roots", "state_path", "baseline", "environment"))
    text(scope["task_id"]); text(scope["session_id"])
    reference(scope["request"]); digest(scope["request_digest"])
    owners = owner_refs(scope["owners"], recovery)
    fields(scope["categories"], CATEGORIES)
    if not isinstance(scope["checks"], list) or len(scope["checks"]) > 128:
        raise CompletionError("expected at most 128 checks")
    checks = {}
    for check in scope["checks"]:
        fields(check, ("id", "category", "kind", "target"))
        key = text(check["id"])
        if key in checks or check["category"] not in CATEGORIES or check["kind"] not in KINDS:
            raise CompletionError("duplicate or unsupported check")
        if not isinstance(check["target"], dict):
            raise CompletionError("check target must be an object")
        if check["kind"] in {"ref_absent", "remote_ref_absent"}:
            validate_ref_observer(check["kind"], check["target"].get("ref"))
        if check["kind"] == "improvement":
            fields(check["target"], ("path", "plan"))
            path(check["target"]["path"])
            if check["category"] != "quality":
                raise CompletionError("improvement observations belong to quality")
            improvement_observer().validate_plan(check["target"]["plan"], scope["task_id"])
        checks[key] = check
    for owner in owners:
        if "original" in owner and not any(
            check["kind"] == "git_source" and check["target"].get("repository") == owner["original"]["repository"]
            and owner["original"]["file"] in check["target"].get("files", []) for check in checks.values()
        ):
            raise CompletionError("owner edit is outside the frozen source obligations")
    for name, category in scope["categories"].items():
        fields(category, ("checks", "not_applicable"))
        expected = [key for key, check in checks.items() if check["category"] == name]
        if category["checks"] != expected:
            raise CompletionError("category coverage differs from fixed obligations")
        if expected:
            if category["not_applicable"] is not None:
                raise CompletionError("applicable category cannot be marked N/A")
        else:
            fields(category["not_applicable"], ("reason", "basis"))
            text(category["not_applicable"]["reason"])
            basis = category["not_applicable"]["basis"]
            if not isinstance(basis, list) or not 1 <= len(basis) <= 32:
                raise CompletionError("expected 1..32 N/A owner references")
            for ref in basis:
                fields(ref, ("path", "sha256"))
                if not any(ref == {"path": owner["path"], "sha256": owner["sha256"]} for owner in owners):
                    raise CompletionError("N/A requires a bound owner contract")
    for key in ("repositories", "artifact_roots"):
        if not isinstance(scope[key], list) or len(scope[key]) > 16 or len(scope[key]) != len(set(scope[key])):
            raise CompletionError("expected unique bounded path list")
        for value in scope[key]:
            path(value)
    path(scope["state_path"])
    if not isinstance(scope["environment"], dict) or scope["environment"] != environment(scope["environment"].get("workspace")):
        raise CompletionError("observer environment changed")
    fields(scope["baseline"], ("repositories", "artifact_roots"))
    if set(scope["baseline"]["repositories"]) != set(scope["repositories"]) or set(scope["baseline"]["artifact_roots"]) != set(scope["artifact_roots"]):
        raise CompletionError("starting inventory coverage differs")
    for before in scope["baseline"]["repositories"].values():
        fields(before, ("identity", "refs", "worktrees", "dirty", "dirty_bytes", "stash"))
        if not isinstance(before["refs"], dict):
            raise CompletionError("invalid starting refs")
        for name in ("worktrees", "dirty", "stash"):
            if not isinstance(before[name], list) or not all(isinstance(value, str) for value in before[name]):
                raise CompletionError("invalid starting Git inventory")
    for before in scope["baseline"]["artifact_roots"].values():
        if not isinstance(before, dict):
            raise CompletionError("invalid starting artifact inventory")
    return checks


def prepare(seed):
    """Capture starting observations. The caller reviews/fixes these exact bytes."""
    fields(seed, ("task_id", "session_id", "request", "request_digest", "owners", "categories", "checks",
                  "repositories", "artifact_roots", "state_path", "workspace"), ("owner_snapshots",))
    seed = decode(canonical(seed))
    refs(seed["owners"])
    snapshots = seed.pop("owner_snapshots", {})
    if not isinstance(snapshots, dict) or snapshots.keys() - {owner["path"] for owner in seed["owners"]}:
        raise CompletionError("owner snapshots must name exact bound owners")
    used_snapshots = set()
    for owner in seed["owners"]:
        for check in seed["checks"]:
            if check["kind"] != "git_source":
                continue
            target = check["target"]
            repo = target["repository"]
            for filename in target["files"]:
                if path(owner["path"]) != path(repo) / filename:
                    continue
                original = {"repository": repo, "file": filename, "identity": repo_identity(repo)}
                if owner["path"] in snapshots:
                    original["snapshot"] = snapshots[owner["path"]]
                    used_snapshots.add(owner["path"])
                else:
                    revision = git(repo, "rev-parse", "HEAD").decode().strip()
                    raw = git(repo, "show", "--no-ext-diff", "--no-textconv", revision + ":" + filename)
                    if sha(raw) != owner["sha256"]:
                        raise CompletionError("planned owner edit requires committed original bytes or an explicit owner snapshot")
                    original["revision"] = revision
                owner["original"] = original
    if snapshots.keys() != used_snapshots:
        raise CompletionError("owner snapshot edit is outside the frozen source obligations")
    for check in seed["checks"]:
        if check["kind"] in {"git_source", "remote_ref_absent"}:
            target = check["target"]
            target["remote_binding"] = remote_identity(target["repository"], target["remote"])
    execution = environment(seed.pop("workspace"))
    scope = {**seed, "baseline": {
        "repositories": {repo: inventory(repo) for repo in seed["repositories"]},
        "artifact_roots": {root: tree(root) for root in seed["artifact_roots"]}}, "environment": execution}
    validate_scope(scope)
    return scope


def process_identity(pid):
    if type(pid) is not int or not 1 <= pid <= 2**31 - 1:
        raise CompletionError("invalid process ID")
    done = subprocess.run([shutil.which("ps", path=observer_env()["PATH"]), "-p", str(pid), "-o", "lstart=", "-o", "command="],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT, env=observer_env())
    if done.returncode == 1 and not done.stdout:
        return None
    if done.returncode or len(done.stdout) > 8192:
        raise CompletionError("process observation unavailable")
    return done.stdout.decode().strip()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise CompletionError("HTTP redirects are not a current owner observation")


def http_expectation(expected):
    fields(expected, ("revision", "health"))
    text(expected["revision"])
    if expected["health"] is not True and (not isinstance(expected["health"], str) or not expected["health"].strip()):
        raise CompletionError("healthy expectation must be present and affirmative")
    return expected


def draft_result(scope_ref, expectations=None):
    """Collect a candidate, never acceptance or an independently reviewed result.

    HTTP expectations must come from the owner, not the running endpoint. Source
    file digests come from committed main, so dirty working bytes cannot silently
    become the intended source. Exceptions remain empty for explicit review.
    """
    scope = read_reference(scope_ref)
    recovery = None
    state_raw = raw_file(scope["state_path"]) if path(scope["state_path"]).is_file() else None
    state = decode(state_raw) if state_raw is not None else {}
    if "owner_recovery" in state or "source_preservation" in state:
        if (state.get("task_id"), state.get("session_id"), state.get("intent"), state.get("scope")) != (scope["task_id"], scope["session_id"], "closure", scope_ref):
            raise CompletionError("current resource pointer differs from frozen task")
    if "owner_recovery" in state:
        recovery = owner_recovery(scope_ref, scope, state["owner_recovery"])
    checks = validate_scope(scope, recovery)
    if "source_preservation" in state:
        source_preservation(scope_ref, scope, state["source_preservation"])
    planned = {(ref["original"]["repository"], ref["original"]["file"])
               for ref in scope["owners"] if "original" in ref}
    expectations = {} if expectations is None else decode(canonical(expectations))
    required = {key for key, check in checks.items() if check["kind"] == "http_json"}
    fields(expectations, required)
    for expected in expectations.values():
        http_expectation(expected)

    def collect(item):
        key, check = item
        kind, target = check["kind"], check["target"]
        if kind == "http_json":
            return key, expectations[key]
        if kind in CLEANUP_KINDS:
            return key, {}  # Absence is still checked after independent review.
        if kind in {"file", "evidence", "installed_file", "improvement"}:
            if kind == "improvement":
                fields(target, ("path", "plan"))
                return key, {"sha256": sha(raw_file(target["path"]))}
            fields(target, ("path",), ("source",))
            return key, {"sha256": sha(raw_file(target["path"], links=kind == "installed_file"))}
        if kind == "git_source":
            fields(target, ("repository", "remote", "main", "files", "remote_binding"))
            repo = target["repository"]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", text(target["main"])) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", text(target["remote"])):
                raise CompletionError("invalid named Git main or remote")
            local = git(repo, "rev-parse", "refs/heads/" + text(target["main"])).decode().strip()
            remote = remote_ref(repo, target["remote"], "refs/heads/" + target["main"], target["remote_binding"])
            if remote is None:
                raise CompletionError("remote main could not be observed")
            files = {}
            for filename in target["files"]:
                relative = Path(text(filename))
                if relative.is_absolute() or ".." in relative.parts or filename.startswith(":"):
                    raise CompletionError("invalid repository-relative source path")
                files[filename] = (None if (repo, filename) in planned
                                   and not git_source_present(repo, local, filename)
                                   else git_source_sha(repo, local, filename))
            return key, {"local_revision": local, "remote_revision": remote, "files": files}
        raise CompletionError("unsupported candidate observation")

    with ThreadPoolExecutor(max_workers=4) as pool:
        values = dict(pool.map(collect, checks.items()))
    updates = {ref["path"]: (sha(raw_file(ref["path"])) if os.path.lexists(path(ref["path"])) else None)
               for ref in scope["owners"] if "original" in ref}
    updates = {name: value for name, value in updates.items()
               if value != next(ref["sha256"] for ref in scope["owners"] if ref["path"] == name)}
    reference(scope_ref); reference(scope["request"])
    if recovery:
        owner_recovery(scope_ref, scope, state["owner_recovery"])
    if "source_preservation" in state:
        source_preservation(scope_ref, scope, state["source_preservation"])
    if recovery or "source_preservation" in state:
        if raw_file(scope["state_path"]) != state_raw:
            raise CompletionError("recovery binding changed during candidate collection")
    current_owners(scope["owners"], updates, recovery)
    result = {"scope_sha256": scope_ref["sha256"], "checks": values, "exceptions": {}}
    if updates:
        result["owner_updates"] = updates
    return result


def observe(check, expected, *, preserved=None):
    kind, target = check["kind"], check["target"]
    if kind == "improvement":
        fields(target, ("path", "plan")); fields(expected, ("sha256",))
        return improvement_observer().inspect(target["plan"], {"path": target["path"], "sha256": digest(expected["sha256"])})
    if kind in ("file", "evidence", "installed_file"):
        fields(target, ("path",), ("source",))
        fields(expected, ("sha256",))
        actual = sha(raw_file(target["path"], links=kind == "installed_file"))
        if actual != digest(expected["sha256"]):
            raise CompletionError("file differs from reviewed bytes")
        if kind == "installed_file":
            if "source" not in target or sha(raw_file(target["source"], links=True)) != actual:
                raise CompletionError("installed and owner source bytes differ")
        return {"sha256": actual, "observation": "bytes only; invocation/quality remain owner checks"}
    if kind == "absent":
        fields(target, ("path",)); fields(expected, ())
        if os.path.lexists(path(target["path"])):
            raise CompletionError("owned temporary path remains")
        return {"absent": True}
    if kind in ("ref_absent", "remote_ref_absent", "worktree_absent"):
        required = ("repository", "ref") if kind != "worktree_absent" else ("repository", "path")
        fields(target, required, ("remote", "remote_binding") if kind == "remote_ref_absent" else ()); fields(expected, ())
        if kind != "worktree_absent":
            ref = validate_ref_observer(kind, target["ref"])
            if kind == "remote_ref_absent":
                remote = text(target.get("remote"))
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote):
                    raise CompletionError("invalid named remote")
                if remote_ref(target["repository"], remote, ref, target.get("remote_binding")):
                    raise CompletionError("owned remote branch remains")
            elif ref == "refs/stash":
                current = git(target["repository"], "for-each-ref", "--format=%(refname)", ref).decode().splitlines()
                if ref in current:
                    raise CompletionError("owned stash remains")
            elif ref in inventory(target["repository"])["refs"]:
                raise CompletionError("owned branch remains")
        else:
            current = inventory(target["repository"])["worktrees"]
            if any(item.startswith("worktree " + str(path(target["path"])) + "\n") for item in current) or os.path.lexists(target["path"]):
                raise CompletionError("owned worktree remains")
        return {"absent": True}
    if kind == "process_absent":
        fields(target, ("pid", "identity")); fields(expected, ())
        text(target["identity"])
        actual = process_identity(target["pid"])
        if actual is not None:
            pattern = r"^[A-Za-z]{3} [A-Za-z]{3} [ 0-9]{2} [0-9:]{8} [0-9]{4}\b"
            previous, current = re.match(pattern, target["identity"]), re.match(pattern, actual)
            if previous is None or current is None:
                raise CompletionError("process start identity is unverified")
            if previous[0] == current[0]:
                # exec can change argv while the same owned process is alive.
                raise CompletionError("owned process remains or changed executable; PID reuse not established")
        return {"absent": True, "pid_reused": actual is not None}
    if kind == "http_json":
        fields(target, ("url", "revision_field", "health_field"))
        http_expectation(expected)
        url = urlsplit(text(target["url"]))
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.fragment:
            raise CompletionError("expected an owner-bound HTTP endpoint without credentials")
        request = Request(target["url"], headers={"Cache-Control": "no-cache", "Accept": "application/json"}, method="GET")
        handlers = [NoRedirect, ProxyHandler({})]
        if url.scheme == "https":
            ca = ssl.get_default_verify_paths().openssl_cafile
            if not ca or not Path(ca).is_file(): raise CompletionError("fixed HTTPS trust source unavailable")
            handlers.append(HTTPSHandler(context=ssl.create_default_context(cafile=ca)))
        with build_opener(*handlers).open(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise CompletionError("HTTP observation exceeded limit")
        value = decode(raw)
        if not isinstance(value, dict):
            raise CompletionError("HTTP observation must be an object")
        revision_key, health_key = text(target["revision_field"]), text(target["health_field"])
        if revision_key not in value or health_key not in value:
            raise CompletionError("running revision or health field missing")
        actual = {"revision": value[revision_key], "health": value[health_key]}
        if actual != expected:
            raise CompletionError("running revision or health differs")
        return actual
    if kind == "git_source":
        fields(target, ("repository", "remote", "main", "files", "remote_binding"))
        fields(expected, ("local_revision", "remote_revision", "files"))
        main, remote = text(target["main"]), text(target["remote"])
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", main) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote):
            raise CompletionError("invalid named Git main or remote")
        repo = target["repository"]
        if git(repo, "symbolic-ref", "--short", "HEAD").decode().strip() != main:
            raise CompletionError("working path has not returned to main")
        local = git(repo, "rev-parse", "refs/heads/" + main).decode().strip()
        remote_oid = remote_ref(repo, remote, "refs/heads/" + main, target["remote_binding"])
        if remote_oid is None:
            raise CompletionError("remote main could not be observed")
        if (local, remote_oid) != (expected["local_revision"], expected["remote_revision"]):
            raise CompletionError("main revision changed or was not integrated")
        if not target["files"] or set(target["files"]) != set(expected["files"]):
            raise CompletionError("reviewed file coverage differs")
        for filename, wanted in expected["files"].items():
            relative = Path(filename)
            if relative.is_absolute() or ".." in relative.parts or filename.startswith(":"):
                raise CompletionError("invalid repository-relative source path")
            if wanted is None:
                if preserved and filename in preserved:
                    raise CompletionError("cannot delete a file with preserved foreign regions")
                for revision in (local, remote_oid):
                    if git_source_present(repo, revision, filename):
                        raise CompletionError("reviewed source deletion is not preserved in both main revisions")
                if os.path.lexists(path(repo) / filename):
                    raise CompletionError("reviewed deleted source reappeared")
                continue
            wanted = digest(wanted)
            for revision in (local, remote_oid):
                if git_source_sha(repo, revision, filename) != wanted:
                    raise CompletionError("reviewed source bytes are not preserved in both main revisions")
            working = wanted
            if preserved and filename in preserved:
                proof = preserved[filename]
                published = git(repo, "show", "--no-ext-diff", "--no-textconv", local + ":" + filename)
                working = sha(preserved_source_bytes(published, proof["working"]))
                rows = git(repo, "--literal-pathspecs", "ls-files", "--stage", "-z", "--", filename).decode().split("\0")
                mode, indexed = index_source_bytes(repo, [row.split("\t", 1)[0] for row in rows if row])
                if mode != proof["mode"] or sha(indexed) != sha(preserved_source_bytes(published, proof["index"])):
                    raise CompletionError("preserved source index differs from publication and retained staging")
            if working_source_sha(path(repo) / filename) != working:
                raise CompletionError("working source bytes differ from integrated source")
        return {"local_revision": local, "remote_revision": remote_oid}
    raise CompletionError("unsupported observer")


def check_scope(scope_ref, result_path, *, task_id=None, request_digest=None, review_ref=None, final_review_ref=None, workspace=None, stopped=False, report=False):
    started = time.monotonic()
    scope = read_reference(scope_ref)
    state_raw = raw_file(scope["state_path"])
    state = decode(state_raw)
    fields(state, ("task_id", "session_id", "intent", "scope", "review", "final_review", "result_path", "resources", "outcome"), ("status_basis", "owner_recovery", "source_preservation"))
    if (state["task_id"], state["session_id"], state["intent"], state["scope"]) != (scope["task_id"], scope["session_id"], "closure", scope_ref):
        raise CompletionError("current resource pointer differs from frozen task")
    recovery = owner_recovery(scope_ref, scope, state["owner_recovery"]) if "owner_recovery" in state else None
    checks = validate_scope(scope, recovery)
    preserved = source_preservation(scope_ref, scope, state["source_preservation"]) if "source_preservation" in state else {}
    if workspace is not None and scope["environment"]["workspace"] != str(path(workspace)):
        raise CompletionError("observer workspace differs from host")
    if task_id is not None and scope["task_id"] != task_id:
        raise CompletionError("task identity differs from host")
    if request_digest is not None and scope["request_digest"] != request_digest:
        raise CompletionError("original request differs from host")
    result_raw = raw_file(result_path)
    result = decode(result_raw)
    fields(result, ("scope_sha256", "checks", "exceptions"), ("owner_updates",))
    if not isinstance(result["checks"], dict) or result["scope_sha256"] != scope_ref["sha256"] or set(result["checks"]) != set(checks):
        raise CompletionError("final result changed or omitted fixed obligations")
    planned = {(ref["original"]["repository"], ref["original"]["file"])
               for ref in scope["owners"] if "original" in ref}
    for key, definition in checks.items():
        if definition["kind"] == "git_source":
            expected = result["checks"][key]
            fields(expected, ("local_revision", "remote_revision", "files"))
            if not isinstance(expected["files"], dict):
                raise CompletionError("source files must be exact path expectations")
            if any(wanted is None and (definition["target"]["repository"], filename) not in planned
                   for filename, wanted in expected["files"].items()):
                raise CompletionError("source deletion was not declared as a planned owner edit")
    current_owners(scope["owners"], result.get("owner_updates", {}), recovery)
    if stopped:
        if state["outcome"] not in {"blocked", "cancelled"}:
            raise CompletionError("terminal release requires blocked/cancelled, not waiting/completed")
        reference(state.get("status_basis"))
    elif state["outcome"] != "working":
        raise CompletionError("task is waiting or stopped, not completed")
    selected_review = reference(review_ref if review_ref is not None else state["review"])
    reviewed = read_reference(selected_review)
    fields(reviewed, ("scope_sha256", "status"))
    if reviewed != {"scope_sha256": scope_ref["sha256"], "status": "No Findings"}:
        raise CompletionError("scope review does not cover these exact scope bytes")
    selected_final = reference(final_review_ref if final_review_ref is not None else state["final_review"])
    reviewed_final = read_reference(selected_final)
    expected_review = {"scope_sha256": scope_ref["sha256"], "result_sha256": sha(result_raw), "status": "No Findings"}
    if recovery: expected_review["owner_recovery_sha256"] = state["owner_recovery"]["evidence"]["sha256"]
    if preserved: expected_review["source_preservation_sha256"] = state["source_preservation"]["evidence"]["sha256"]
    if stopped: expected_review["disposition"] = "stopped"
    if reviewed_final != expected_review:
        raise CompletionError("final review does not cover these exact expectations and exceptions")
    failures, observed = [], {}

    def inspect(item):
        key, definition, wanted = item
        if stopped and definition["kind"] not in CLEANUP_KINDS:
            return key, {"unfulfilled": True, "reason": "terminal stop; original goal is not claimed complete"}, None
        try:
            return key, (observe(definition, wanted, preserved=preserved[key]) if key in preserved else observe(definition, wanted)), None
        except (CompletionError, OSError, ValueError, URLError, subprocess.TimeoutExpired) as exc:
            return key, None, str(exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        for key, observation, failure in pool.map(inspect, [(key, value, result["checks"][key]) for key, value in checks.items()]):
            if failure:
                failures.append({"id": key, "reason": failure})
            else:
                observed[key] = observation
    if not isinstance(state["resources"], list) or len(state["resources"]) > 128:
        raise CompletionError("expected bounded accumulated resource list")
    if len({resource.get("id") for resource in state["resources"] if isinstance(resource, dict)}) != len(state["resources"]):
        raise CompletionError("duplicate accumulated resource identity")
    for resource in state["resources"]:
        fields(resource, ("id", "kind", "target", "basis"))
        try:
            reference(resource["basis"])
            if resource["kind"] not in CLEANUP_KINDS:
                raise CompletionError("unresolved reservation or unsupported host resource observation")
            observe(resource, {})
        except (CompletionError, OSError, subprocess.TimeoutExpired) as exc:
            failures.append({"id": "resource:" + resource["id"], "reason": str(exc)})
    exceptions = result["exceptions"]
    if not isinstance(exceptions, dict):
        raise CompletionError("exceptions must be an object")
    for exception in exceptions.values():
        fields(exception, ("owner", "reason", "scope", "removal_condition", "basis"))
        for key in ("owner", "reason", "scope", "removal_condition"):
            text(exception[key])
        refs(exception["basis"])
    unclassified = []
    for repo, before in scope["baseline"]["repositories"].items():
        after = inventory(repo)
        mains = {"refs/heads/" + c["target"]["main"] for c in checks.values() if c["kind"] == "git_source" and c["target"]["repository"] == repo}
        mains.update("refs/remotes/" + c["target"]["remote"] + "/" + c["target"]["main"] for c in checks.values() if c["kind"] == "git_source" and c["target"]["repository"] == repo)
        if after["identity"] != before["identity"]:
            raise CompletionError("repository identity changed during task")
        if stopped: mains = set()
        for name in before["refs"].keys() | after["refs"].keys():
            if name not in mains and before["refs"].get(name) != after["refs"].get(name):
                unclassified.append("git:" + repo + ":ref:" + name)
        for name in ("worktrees", "dirty", "stash"):
            values = (lambda v: {line.splitlines()[0] for line in v if line.startswith("worktree ")}) if name == "worktrees" else set
            for item in values(after[name]) ^ values(before[name]):
                if item:
                    unclassified.append("git:" + repo + ":" + name + ":" + item)
        for filename in before["dirty_bytes"].keys() | after["dirty_bytes"].keys():
            if before["dirty_bytes"].get(filename) != after["dirty_bytes"].get(filename):
                unclassified.append("git:" + repo + ":content:" + filename)
    for root, before in scope["baseline"]["artifact_roots"].items():
        after = tree(root)
        for item in before.keys() | after.keys():
            if before.get(item) != after.get(item):
                unclassified.append("path:" + item)
    # Re-observe source after all other checks: main/remote changes seen inside
    # this inspection are never hidden by the expected main inventory exception.
    for key, definition in checks.items():
        if definition["kind"] in {"git_source", "improvement"} and not stopped:
            _, _, failure = inspect((key, definition, result["checks"][key]))
            if failure:
                label = "source" if definition["kind"] == "git_source" else "improvement"
                failures.append({"id": key, "reason": "final " + label + " observation: " + failure})
    for item in unclassified:
        if item not in exceptions:
            failures.append({"id": item, "reason": "new remaining resource has no current owner classification"})
    # Recheck immutable inputs and accumulated handles after observations.
    reference(scope_ref); reference(scope["request"])
    if recovery: owner_recovery(scope_ref, scope, state["owner_recovery"])
    if preserved: source_preservation(scope_ref, scope, state["source_preservation"])
    current_owners(scope["owners"], result.get("owner_updates", {}), recovery); reference(selected_review)
    reference(selected_final)
    if scope["environment"] != environment(scope["environment"]["workspace"]):
        raise CompletionError("observer environment changed during inspection")
    if raw_file(result_path) != result_raw or raw_file(scope["state_path"]) != state_raw:
        raise CompletionError("completion input or accumulated resources changed during inspection")
    if stopped: reference(state["status_basis"])
    answer = {"complete": not failures and not stopped, "stopped": stopped and not failures, "task_id": scope["task_id"], "scope_sha256": scope_ref["sha256"],
            "checked_at": datetime.now(timezone.utc).isoformat(), "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "observed": observed, "failures": failures, "exceptions": exceptions,
            "resource_state": {"path": scope["state_path"], "sha256": sha(state_raw)},
            "owner_paths": [ref["path"] for ref in scope["owners"]],
            "assurance": "mechanical observations; scope, consent, quality and reviewer independence remain owner judgments"}
    if report:
        answer.update(categories=scope["categories"], expectations=result["checks"],
                      result={"path": str(path(result_path)), "sha256": sha(result_raw)},
                      scope_review=selected_review, final_review=selected_final)
    if preserved: answer["source_preservation_sha256"] = state["source_preservation"]["evidence"]["sha256"]
    return answer


def write_new(filename, value):
    filename = path(filename)
    with filename.open("xb") as stream:
        os.chmod(filename, 0o600)
        stream.write(canonical(value) + b"\n")
    return {"path": str(filename), "sha256": sha(raw_file(str(filename)))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    begin = sub.add_parser("prepare", help="capture beginning observations before implementation; independent review freezes the output")
    begin.add_argument("--seed", required=True); begin.add_argument("--output", required=True)
    draft = sub.add_parser("draft", help="collect candidate expectations; independent final review is still required")
    draft.add_argument("--scope", required=True); draft.add_argument("--sha256", required=True)
    draft.add_argument("--output", required=True)
    draft.add_argument("--expectations", help="JSON with exactly the owner-selected HTTP check IDs and revision/health expectations")
    check = sub.add_parser("check", help="observe exact fixed obligations without executing input commands")
    check.add_argument("--scope", required=True); check.add_argument("--sha256", required=True)
    check.add_argument("--result", required=True)
    check.add_argument("--stopped", action="store_true", help="verify terminal resource release; never marks the goal complete")
    check.add_argument("--task-id"); check.add_argument("--request-digest"); check.add_argument("--workspace")
    check.add_argument("--review"); check.add_argument("--review-sha256")
    check.add_argument("--final-review"); check.add_argument("--final-review-sha256")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            answer = write_new(args.output, prepare(decode(raw_file(args.seed))))
        elif args.command == "draft":
            value = draft_result({"path": args.scope, "sha256": args.sha256},
                                 decode(raw_file(args.expectations)) if args.expectations else None)
            answer = {**write_new(args.output, value), "review_required": True}
        else:
            answer = check_scope({"path": args.scope, "sha256": args.sha256}, args.result,
                                 task_id=args.task_id, request_digest=args.request_digest, workspace=args.workspace, stopped=args.stopped,
                                 review_ref={"path": args.review, "sha256": args.review_sha256} if args.review else None,
                                 final_review_ref={"path": args.final_review, "sha256": args.final_review_sha256} if args.final_review else None)
        print(canonical(answer).decode())
        return 0 if answer.get("complete", True) or answer.get("stopped") else 2
    except (CompletionError, OSError, ValueError, URLError, subprocess.TimeoutExpired) as exc:
        print(canonical({"complete": False, "error": str(exc)}).decode())
        return 2


if __name__ == "__main__":
    sys.exit(main())
