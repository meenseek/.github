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
from urllib.parse import unquote, urlsplit, urlunsplit


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

    def objects(self, org, requests):
        """Fetch exact commit/path pairs in batches, including explicit nulls for absence."""
        results = {}
        for offset in range(0, len(requests), 40):
            batch = requests[offset:offset + 40]
            fields = []
            for i, (repo, commit, path) in enumerate(batch):
                fields.append(f"r{i}:repository(owner:{json.dumps(org)},name:{json.dumps(repo)})"
                              " { object(expression:" + json.dumps(commit + ":" + path)
                              + ") { __typename ... on Blob { oid byteSize isBinary text } } }")
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


def check(org, github, selected=None):
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
    inventory = {r["name"]: r for r in repositories}
    if selected and selected not in inventory:
        report["selection_error"] = "requested repository is not visible"
    targets = [r for r in repositories if not selected or r["name"] == selected]
    requests = [(r["name"], r["defaultBranchRef"]["target"]["oid"], path)
                for r in targets if r.get("defaultBranchRef") for path in ["README.md", "AGENTS.md"]]
    documents = github.objects(org, requests)
    references = {}
    for repo in targets:
        branch = repo.get("defaultBranchRef")
        row = {"id": repo["databaseId"], "name": repo["nameWithOwner"],
               "private": repo["isPrivate"], "archived": repo["isArchived"],
               "empty": repo["isEmpty"], "commit": branch["target"]["oid"] if branch else None,
               "entrypoints": {}, "contract": None, "references": [],
               "structure": {"status": "unknown", "reason": "connection contract needs owner assessment"},
               "connections": {"status": "unknown", "reason": "owner contract meaning and target environment verification required"}}
        report["repositories"].append(row)
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
    resolved = dict(documents)
    resolved.update(github.objects(org, [r for r in references if r not in resolved]))
    for request, items in references.items():
        obj, error = resolved[request]
        for item in items:
            item["status"] = "unknown" if error else "confirmed" if obj else "failed"
            item["reason"] = error or ("source exists at pinned commit; anchors and meaning need native review" if obj
                                      else "declared source absent at pinned commit")
    for row in report["repositories"]:
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
             "| Repository | Structure | Connections |", "|---|---|---|"]
    for row in report["repositories"]:
        lines.append(f"| {row['name']} | {row['structure']['status']} | {row['connections']['status']} |")
    for row in report["repositories"]:
        lines.extend(["", f"## {row['name']}", row["structure"]["reason"]])
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
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", args.org):
        parser.error("invalid organization")
    if args.repo and not re.fullmatch(r"[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo takes a repository name, without an owner")
    report = check(args.org, GitHub(), args.repo)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else markdown(report), end="\n")
    # Exit success concerns structural checks only; connections are deliberately unassessed.
    statuses = {r["structure"]["status"] for r in report["repositories"]}
    if report["inventory"]["status"] != "confirmed" or report.get("selection_error") or "unknown" in statuses:
        return 2
    return 1 if "failed" in statuses else 0


if __name__ == "__main__":
    sys.exit(main())
