#!/usr/bin/env python3
"""Read-only repository discovery and contract-reference checks; never run documents."""

import argparse
import hashlib
import json
import posixpath
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit


POLICY = Path(__file__).resolve().parents[1] / "docs/repository-rules.md"
SECTION = re.compile(r"^## (?:연결과 검증|Connections and verification)\s*$", re.M)
LINK = re.compile(r"\[[^\]\n]*\]\(")


class ReadError(Exception):
    def __init__(self, message, partial=None):
        super().__init__(message)
        self.partial = partial or []


class GitHub:
    def __init__(self):
        self.calls = 0

    def command(self, *args):
        self.calls += 1
        try:
            result = subprocess.run(["gh", "api", *args], capture_output=True,
                                    text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReadError(type(exc).__name__) from exc
        if result.returncode:
            # Never copy credentials, document bodies, or raw tool output into reports.
            raise ReadError(f"GitHub read failed (exit {result.returncode})")
        return result.stdout

    def api(self, endpoint):
        try:
            return json.loads(self.command(endpoint))
        except json.JSONDecodeError as exc:
            raise ReadError("invalid GitHub JSON") from exc

    def graphql(self, query):
        try:
            value = json.loads(self.command("graphql", "-f", "query=" + query))
        except json.JSONDecodeError as exc:
            raise ReadError("invalid GraphQL JSON") from exc
        if value.get("errors") or not isinstance(value.get("data"), dict):
            raise ReadError("GraphQL read incomplete")
        return value["data"]

    def coverage(self, org):
        """A selected-repository token must not masquerade as full organization access."""
        raw = self.command("--include", "user")
        scope_header = re.search(r"^x-oauth-scopes:\s*(.*)$", raw, re.I | re.M)
        scopes = set(scope_header.group(1).strip().split(", ")) if scope_header else set()
        membership = self.api(f"user/memberships/orgs/{org}")
        complete = ("repo" in scopes and bool(scopes & {"read:org", "admin:org"})
                    and membership.get("state") == "active"
                    and membership.get("role") == "admin")
        return {"status": "confirmed" if complete else "unknown",
                "reason": "organization admin with classic full-repo read scopes" if complete
                else "full private-repository visibility is not established"}

    def inventory(self, org):
        repositories, cursor, seen = [], None, set()
        while True:
            after = ",after:" + json.dumps(cursor) if cursor else ""
            query = ("query { organization(login:" + json.dumps(org) + ") { "
                     "repositories(first:100" + after + ",orderBy:{field:NAME,direction:ASC}) { "
                     "nodes { databaseId name nameWithOwner isPrivate isArchived isEmpty isFork "
                     "defaultBranchRef { name target { ... on Commit { oid } } } } "
                     "pageInfo { hasNextPage endCursor } } } }")
            try:
                data = self.graphql(query)
            except ReadError as exc:
                raise ReadError(str(exc), repositories) from exc
            organization = data.get("organization")
            if not isinstance(organization, dict):
                raise ReadError("organization is inaccessible", repositories)
            page = organization["repositories"]
            nodes = page["nodes"]
            if any(not isinstance(r, dict) or not r.get("databaseId") for r in nodes):
                raise ReadError("repository inventory contains inaccessible entries", repositories)
            repositories.extend(nodes)
            info = page["pageInfo"]
            if not info["hasNextPage"]:
                if len({r["databaseId"] for r in repositories}) != len(repositories):
                    raise ReadError("inventory changed during pagination; reconcile again", repositories)
                return repositories
            cursor = info["endCursor"]
            if not cursor or cursor in seen:
                raise ReadError("inventory pagination is incomplete", repositories)
            seen.add(cursor)

    def actions_budget(self, org):
        """Read the organization-wide Actions hard stop; never change billing."""
        try:
            data = self.api(f"orgs/{org}/settings/billing/budgets")
            if not isinstance(data, dict) or data.get("has_next_page") is not False:
                raise ReadError("Actions budget coverage is incomplete")
            budgets = data.get("budgets")
            if not isinstance(budgets, list) or data.get("total_count") != len(budgets):
                raise ReadError("Actions budget coverage is incomplete")
            matches = [b for b in budgets if isinstance(b, dict)
                       and b.get("budget_type") == "ProductPricing"
                       and b.get("budget_product_sku") == "actions"
                       and b.get("budget_scope") == "organization"
                       and isinstance(b.get("budget_entity_name"), str)
                       and b["budget_entity_name"].lower() == org.lower()]
            if any(b.get("budget_amount") == 0 and b.get("prevent_further_usage") is True
                   for b in matches):
                return {"status": "confirmed", "amount": 0, "stop_usage": True,
                        "reason": "organization-wide Actions zero-dollar stop is enabled"}
            return {"status": "failed", "reason": "organization-wide Actions zero-dollar stop not established"}
        except ReadError as exc:
            return {"status": "unknown", "reason": str(exc)}

    def runs(self, org, repo, commit):
        """One bounded, paginated read per declared repository, shared by its workflows."""
        runs, seen = [], set()
        for page in range(1, 11):
            query = urlencode({"head_sha": commit, "event": "push", "per_page": 100, "page": page})
            data = self.api(f"repos/{org}/{repo}/actions/runs?{query}")
            if not isinstance(data, dict) or not isinstance(data.get("workflow_runs"), list):
                raise ReadError("invalid Actions runs response")
            batch = data["workflow_runs"]
            total = data.get("total_count")
            if not isinstance(total, int) or not 0 <= total <= 1000:
                raise ReadError("Actions runs exceed the bounded API search coverage")
            for run in batch:
                if not isinstance(run, dict) or not isinstance(run.get("id"), int) or run["id"] in seen:
                    raise ReadError("Actions runs changed during pagination")
                seen.add(run["id"])
            runs.extend(batch)
            if len(runs) == total:
                return runs
            if len(batch) < 100:
                raise ReadError("Actions runs pagination is incomplete")
        raise ReadError("Actions runs pagination is incomplete")

    def objects(self, org, requests):
        """Fetch exact commit/path pairs in batches, including explicit nulls for absence."""
        results = {}
        for offset in range(0, len(requests), 40):
            batch = requests[offset:offset + 40]
            fields = []
            for i, (repo, commit, path) in enumerate(batch):
                fields.append(f"r{i}:repository(owner:{json.dumps(org)},name:{json.dumps(repo)})"
                              " { object(expression:" + json.dumps(commit + ":" + path)
                              + ") { __typename ... on Blob { oid byteSize isBinary text } "
                              "... on Tree { entries { name type } } } }")
            try:
                data = self.graphql("query { " + " ".join(fields) + " }")
                for i, request in enumerate(batch):
                    record = data.get(f"r{i}")
                    results[request] = ((record["object"], None) if isinstance(record, dict)
                                        and "object" in record else (None, "repository inaccessible"))
            except ReadError as exc:
                for request in batch:
                    results[request] = (None, str(exc))
        return results


def section(text):
    lines, fence = [], None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
        else:
            lines.append(line)
    text = re.sub(r"<!--.*?-->", "", "".join(lines), flags=re.S)
    matches = list(SECTION.finditer(text))
    if len(matches) > 1:
        raise ReadError("duplicate connection sections in one entrypoint")
    if not matches:
        return None
    tail = text[matches[0].end():]
    end = re.search(r"^#{1,2} ", tail, re.M)
    return tail[:end.start()] if end else tail


def declared_links(body):
    lines, indented, previous_blank, list_seen = [], False, True, False
    for line in body.splitlines(keepends=True):
        is_indented = line.startswith("    ") or line.startswith("\t")
        if is_indented and (indented or previous_blank):
            # List continuations and nested code need a full Markdown parser.
            # Preserve uncertainty instead of testing an example as a dependency.
            if list_seen and LINK.search(line):
                raise ReadError("indented list content needs owner assessment")
            indented = True
        else:
            if line.strip():
                indented = False
            lines.append(line)
            if re.match(r"^ {0,3}(?:[-+*]|\d+[.)]) ", line):
                list_seen = True
        previous_blank = not line.strip()
    body = "".join(lines)
    body = re.sub(r"(`+)(?!`).*?(?<!`)\1(?!`)", "", body, flags=re.S)
    targets = []
    for match in LINK.finditer(body):
        prefix = body[:match.start()]
        if (len(prefix) - len(prefix.rstrip("\\"))) % 2:
            continue
        start = match.end()
        if body[start:start + 1] == "<":
            end = body.find(">", start + 1)
            target = body[start + 1:end] if end >= 0 else "unparsed:markdown-link"
        else:
            end, depth = start, 1
            while end < len(body):
                char = body[end]
                if char == "\\":
                    end += 2
                    continue
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        break
                elif char.isspace() and depth == 1:
                    break
                end += 1
            target = body[start:end] if end < len(body) and depth == 1 else "unparsed:markdown-link"
            # A balanced outer close has depth zero; nested parentheses belong to the path.
            if end < len(body) and depth == 0:
                target = body[start:end]
            target = re.sub(r"\\([\\()<> ])", r"\1", target)
        if target not in targets:
            targets.append(target)
    return targets


def declared_ci_links(body):
    return declared_links("\n".join(line for line in body.splitlines() if line.startswith("CI:")))


def display_target(target):
    try:
        parsed = urlsplit(target.strip("<>"))
    except ValueError:
        return "[invalid reference; inspect the source entrypoint]"
    if parsed.username or parsed.password or parsed.query:
        return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", parsed.fragment))
    return target


def reference(target, repo, source, inventory, org):
    try:
        parsed = urlsplit(target.strip("<>"))
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        parts = unquote(parsed.path).strip("/").split("/")
        if (parsed.scheme == "https" and parsed.netloc == "github.com" and len(parts) >= 5
                and parts[0] == org and parts[2] in {"blob", "tree"}):
            owner = inventory.get(parts[1])
            if owner and owner.get("defaultBranchRef"):
                branch = owner["defaultBranchRef"]
                rest = "/".join(parts[3:])
                for ref in [branch["target"]["oid"], branch["name"]]:
                    if rest.startswith(ref + "/"):
                        return (owner["name"], branch["target"]["oid"], rest[len(ref) + 1:])
        return None
    path = unquote(parsed.path)
    if not path:
        return (repo["name"], repo["defaultBranchRef"]["target"]["oid"], source)
    if path.startswith("/") or "\x00" in path:
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
    owner = repo
    if path.startswith("../"):
        parts = path.split("/", 2)
        if len(parts) != 3 or parts[1] not in inventory:
            return None
        owner, path = inventory[parts[1]], parts[2]
    branch = owner.get("defaultBranchRef")
    if not branch or path.startswith("../"):
        return None
    return (owner["name"], branch["target"]["oid"], path)


def local_source(repo, root):
    """Export common source identity only; never execute a repository's commands."""
    row = {"status": "unknown", "checked_at": datetime.now(timezone.utc).isoformat(),
           "reason": "local checkout is not available", "entrypoints": {}}
    path = Path(root).resolve() / repo["name"].split("/", 1)[1]
    try:
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != Path(root).resolve():
            return row
        def git(*args):
            result = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                                    text=True, timeout=10, check=False)
            if result.returncode:
                raise ReadError("local Git identity could not be verified")
            return result.stdout.strip()
        if Path(git("rev-parse", "--show-toplevel")).resolve() != path.resolve():
            raise ReadError("directory is not the root of this checkout")
        remote = git("remote", "get-url", "origin")
        expected = re.escape(repo["name"])
        if not re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                            + expected + r"(?:\.git)?/?", remote, re.I):
            raise ReadError("origin does not match the discovered repository")
        row.update(status="confirmed", path=str(path.resolve()),
                   commit=git("rev-parse", "HEAD"), branch=git("branch", "--show-current"),
                   dirty=bool(git("status", "--porcelain")),
                   reason="local Git and entrypoint bytes only; not runtime or business verification")
        for name in ("README.md", "AGENTS.md"):
            source = path / name
            if not source.exists():
                row["entrypoints"][name] = {"status": "absent"}
                continue
            if source.is_symlink() or not source.is_file() or source.stat().st_size > 1024 * 1024:
                row["entrypoints"][name] = {"status": "unknown", "reason": "not a bounded regular source"}
                continue
            before = source.stat()
            data = source.read_bytes()
            after = source.stat()
            identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                      value.st_mtime_ns, value.st_ctime_ns)
            if source.is_symlink() or identity(before) != identity(after) or len(data) != after.st_size:
                row["entrypoints"][name] = {"status": "unknown", "reason": "source changed while reading"}
                continue
            row["entrypoints"][name] = {"status": "confirmed", "path": str(source),
                                        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    except (OSError, subprocess.TimeoutExpired, ReadError) as exc:
        row.update(status="unknown", reason=str(exc) if isinstance(exc, ReadError) else type(exc).__name__)
    return row


def ci_observation(repo, listing, references, github, org):
    """Keep discovery, owner selection and exact-commit hosted success distinct."""
    result = {"status": "unknown", "workflow_files": [], "workflows": [],
              "reason": "CI applicability and workflow selection need the owner contract"}
    obj, error = listing
    if error:
        result["reason"] = error
        return result
    if obj is not None and (obj.get("__typename") != "Tree" or not isinstance(obj.get("entries"), list)):
        result["reason"] = "workflow directory could not be assessed"
        return result
    result["workflow_files"] = sorted(".github/workflows/" + e["name"]
        for e in (obj or {}).get("entries", [])
        if e.get("type") == "blob" and re.fullmatch(r"[^/]+\.ya?ml", e.get("name", "")))
    declared = {}
    for item in references:
        source = item.get("source", {})
        if source.get("repository") != repo["name"] or not re.fullmatch(
                r"\.github/workflows/[^/]+\.ya?ml", source.get("path", "")):
            result["reason"] = "CI declaration does not identify a workflow in this repository; owner assessment required"
            return result
        declared[source["path"]] = item
    if not declared:
        if not result["workflow_files"]:
            result.update(status="absent", reason="no GitHub workflow at observed commit; owner must assess applicability")
        return result
    if any(p not in result["workflow_files"] or r["status"] != "confirmed" for p, r in declared.items()):
        result.update(status="failed", reason="declared CI workflow is absent or inaccessible at observed commit")
        if any(r["status"] == "unknown" for r in declared.values()):
            result.update(status="unknown", reason="declared CI workflow access is unverified")
        return result
    branch = repo["defaultBranchRef"]
    try:
        runs = github.runs(org, repo["name"], branch["target"]["oid"])
    except ReadError as exc:
        result["reason"] = str(exc)
        return result
    for filename in sorted(declared):
        candidates = [r for r in runs if r.get("path") == filename and r.get("event") == "push"
                      and r.get("head_sha") == branch["target"]["oid"] and r.get("head_branch") == branch["name"]]
        item = {"path": filename, "status": "unknown", "reason": "no main push run for the observed commit"}
        if candidates:
            run = max(candidates, key=lambda r: (r["id"], r.get("run_attempt", 1)))
            success = run.get("status") == "completed" and run.get("conclusion") == "success"
            failure = run.get("status") == "completed" and run.get("conclusion") in {"failure", "timed_out", "action_required", "startup_failure"}
            item.update(status="confirmed" if success else "failed" if failure else "unknown",
                        reason="latest exact-commit main push workflow result",
                        run={k: run.get(k) for k in ("id", "run_attempt", "head_sha", "status", "conclusion")},
                        url=f"https://github.com/{org}/{repo['name']}/actions/runs/{run['id']}")
        result["workflows"].append(item)
    statuses = {w["status"] for w in result["workflows"]}
    result.update(status="failed" if "failed" in statuses else "unknown" if "unknown" in statuses else "confirmed",
                  reason="declared workflow results only; command meaning and runtime readiness need owner review")
    return result


def check(org, github, selected=None, local_root=None):
    report = {"org": org, "checked_at": datetime.now(timezone.utc).isoformat(),
              "environment": "GitHub committed sources; local runtime checks are separate",
              "policy": {"path": str(POLICY), "sha256": hashlib.sha256(POLICY.read_bytes()).hexdigest()},
              "inventory": {"status": "unknown"}, "repositories": []}
    try:
        coverage = github.coverage(org)
    except ReadError as exc:
        coverage = {"status": "unknown", "reason": str(exc)}
    try:
        repositories = github.inventory(org)
        pagination_complete = True
    except ReadError as exc:
        repositories = list({r["databaseId"]: r for r in exc.partial}.values())
        coverage = {"status": "unknown", "reason": str(exc)}
        pagination_complete = False
    report["inventory"] = {**coverage, "visible_count": len(repositories), "pagination_complete": pagination_complete}
    report["actions_budget"] = github.actions_budget(org)
    inventory = {r["name"]: r for r in repositories}
    if selected and selected not in inventory:
        report["selection_error"] = "requested repository is not visible"
    targets = [r for r in repositories if not selected or r["name"] == selected]
    requests = [(r["name"], r["defaultBranchRef"]["target"]["oid"], path)
                for r in targets if r.get("defaultBranchRef") for path in ["README.md", "AGENTS.md", ".github/workflows"]]
    documents = github.objects(org, requests)
    references = {}
    ci_references = {}
    for repo in targets:
        branch = repo.get("defaultBranchRef")
        row = {"id": repo["databaseId"], "name": repo["nameWithOwner"],
               "private": repo["isPrivate"], "archived": repo["isArchived"],
               "empty": repo["isEmpty"], "commit": branch["target"]["oid"] if branch else None,
               "entrypoints": {}, "contract": None, "references": [],
               "structure": {"status": "unknown", "reason": "connection contract needs owner assessment"},
               "connections": {"status": "unknown", "reason": "owner contract meaning and target environment verification required"}}
        row["ci"] = {"status": "unknown", "workflow_files": [], "workflows": [],
                     "reason": "no committed source; owner must assess applicability"}
        report["repositories"].append(row)
        if local_root is not None:
            row["local"] = local_source(row, local_root)
        if not branch:
            row["structure"]["reason"] = "no committed source; assess repository purpose before excluding"
            continue
        contracts, declaration_error = [], False
        for path in ["README.md", "AGENTS.md"]:
            obj, error = documents[(repo["name"], row["commit"], path)]
            row["entrypoints"][path] = {"status": "unknown" if error else "confirmed" if obj else "absent"}
            if error:
                row["entrypoints"][path]["reason"] = error
                declaration_error = True
            parsed_document = False
            if obj and obj.get("__typename") == "Blob" and not obj.get("isBinary"):
                text = obj.get("text")
                if isinstance(text, str) and obj.get("byteSize", 0) <= 1024 * 1024:
                    parsed_document = True
                    try:
                        body = section(text)
                    except ReadError as exc:
                        row["entrypoints"][path]["contract_error"] = str(exc)
                        declaration_error = True
                        continue
                    if body and body.strip():
                        contracts.append((path, obj["oid"], body))
            if obj and not parsed_document:
                row["entrypoints"][path] = {"status": "unknown", "reason": "entrypoint content cannot be assessed"}
                declaration_error = True
        if declaration_error or len(contracts) != 1:
            row["structure"]["reason"] = "declare connections in one README/AGENTS section or resolve duplicated ownership"
            continue
        source, oid, body = contracts[0]
        row["contract"] = {"path": source, "blob": oid, "section": "연결과 검증"}
        # Fenced examples are not declarations of actual dependencies.
        try:
            links = declared_links(body)
            ci_links = declared_ci_links(body)
        except ReadError as exc:
            row["structure"]["reason"] = str(exc)
            continue
        if not links:
            row["structure"]["reason"] = "contract has no source references; owner assessment required"
            continue
        for link in links:
            request = reference(link, repo, source, inventory, org)
            item = {"target": display_target(link), "status": "unknown"}
            if request:
                item["source"] = {"repository": request[0], "commit": request[1], "path": request[2]}
                references.setdefault(request, []).append(item)
            else:
                item["reason"] = "native, external, historical or host-specific reference; verify in its own environment"
            row["references"].append(item)
            if link in ci_links:
                ci_references.setdefault(row["id"], []).append(item)
    resolved = dict(documents)
    resolved.update(github.objects(org, [r for r in references if r not in resolved]))
    for request, items in references.items():
        obj, error = resolved[request]
        for item in items:
            item["status"] = "unknown" if error else "confirmed" if obj else "failed"
            item["reason"] = error or ("source exists at pinned commit; anchors and meaning need native review" if obj
                                      else "declared source absent at pinned commit")
    for row in report["repositories"]:
        if row["commit"]:
            repo = inventory[row["name"].split("/", 1)[1]]
            row["ci"] = ci_observation(repo, documents[(repo["name"], row["commit"], ".github/workflows")],
                                       ci_references.get(row["id"], []), github, org)
        if not row["references"]:
            continue
        statuses = {i["status"] for i in row["references"]}
        status = "failed" if "failed" in statuses else "unknown" if "unknown" in statuses else "confirmed"
        row["structure"] = {"status": status, "reason": "reference accessibility only; not repository readiness"}
    report["github_calls"] = github.calls
    return report


def markdown(report):
    lines = [f"# {report['org']} repository connection check", "",
             f"Checked: {report['checked_at']}",
             f"Inventory: {report['inventory']['status']} (full visibility and pagination assessed separately)",
             "", "Structure checks do not prove runtime readiness.", "",
             f"Actions budget: {report['actions_budget']['status']} — {report['actions_budget']['reason']}", "",
             "| Repository | Structure | CI | Connections |", "|---|---|---|---|"]
    for row in report["repositories"]:
        lines.append(f"| {row['name']} | {row['structure']['status']} | {row['ci']['status']} | {row['connections']['status']} |")
    for row in report["repositories"]:
        lines.extend(["", f"## {row['name']}", row["structure"]["reason"]])
        lines.extend(["CI: " + row["ci"]["reason"], "Workflow files: " + (", ".join(row["ci"]["workflow_files"]) or "none")])
        for workflow in row["ci"]["workflows"]:
            lines.append(f"- CI {workflow['path']}: {workflow['status']} — {workflow['reason']}")
        for ref in row["references"]:
            if ref["status"] != "confirmed":
                lines.append(f"- {ref['status']}: `{ref['target']}` — {ref['reason']}")
    if report.get("selection_error"):
        lines.append(report["selection_error"])
    if report["inventory"].get("reason"):
        lines.append(report["inventory"]["reason"])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default="meenseek")
    parser.add_argument("--repo", help="one repository name; inventory still spans the organization")
    parser.add_argument("--require-ci", action="store_true",
                        help="Creation gate: require one selected repo's declared exact-commit CI and zero-dollar Actions stop")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--local-root", type=Path,
                        help="Optionally export verified local Git/entrypoint identity; no runtime checks")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", args.org):
        parser.error("invalid organization")
    if args.repo and not re.fullmatch(r"[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo takes a repository name, without an owner")
    if args.require_ci and not args.repo:
        parser.error("--require-ci needs --repo; applicability exclusions require owner assessment")
    report = check(args.org, GitHub(), args.repo, args.local_root)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else markdown(report), end="\n")
    # Exit success concerns structural checks only; connections are deliberately unassessed.
    statuses = {r["structure"]["status"] for r in report["repositories"]}
    if args.require_ci:
        statuses.update("unknown" if r["ci"]["status"] == "absent" else r["ci"]["status"] for r in report["repositories"])
        statuses.add(report["actions_budget"]["status"])
    if report["inventory"]["status"] != "confirmed" or report.get("selection_error") or "unknown" in statuses:
        return 2
    return 1 if "failed" in statuses else 0


if __name__ == "__main__":
    sys.exit(main())
