import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_repositories.py"
spec = importlib.util.spec_from_file_location("repository_check", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def repository(name="product", identity=1, archived=False, empty=False):
    return {"databaseId": identity, "name": name, "nameWithOwner": "meenseek/" + name,
            "isPrivate": True, "isArchived": archived, "isEmpty": empty, "isFork": False,
            "defaultBranchRef": None if empty else {"name": "main", "target": {"oid": "a" * 40}}}


def blob(text):
    return {"__typename": "Blob", "oid": "b" * 40, "byteSize": len(text.encode()),
            "isBinary": False, "text": text}


class MemoryGitHub:
    def __init__(self, repos, files=None, complete=True):
        self.repos, self.files, self.complete = repos, files or {}, complete
        self.calls = 0
        self.requests = []

    def coverage(self, org):
        return {"status": "confirmed" if self.complete else "unknown", "reason": "synthetic coverage"}

    def inventory(self, org):
        return self.repos

    def objects(self, org, requests):
        self.requests.extend(requests)
        return {request: self.files.get(request, (None, None)) for request in requests}


class RepositoryChecks(unittest.TestCase):
    def test_structure_is_not_runtime_readiness_and_no_commands_execute(self):
        repo = repository()
        key = ("product", "a" * 40, "README.md")
        github = MemoryGitHub([repo], {key: (blob("# Product\n## 연결과 검증\n[Contract](README.md)\n"
                                                "`touch /tmp/should-not-exist`\n"), None)})
        with patch.object(module.subprocess, "run", side_effect=AssertionError("no document execution")):
            result = module.check("meenseek", github)
        row = result["repositories"][0]
        self.assertEqual(row["structure"]["status"], "confirmed")
        self.assertEqual(row["connections"]["status"], "unknown")
        self.assertEqual(github.requests.count(key), 1)

    def test_missing_declared_source_fails_but_missing_declaration_is_unknown(self):
        repo = repository()
        key = ("product", "a" * 40, "README.md")
        github = MemoryGitHub([repo], {key: (blob("## 연결과 검증\n[Contract](missing.md)"), None)})
        row = module.check("meenseek", github)["repositories"][0]
        self.assertEqual(row["structure"]["status"], "failed")
        row = module.check("meenseek", MemoryGitHub([repo]))["repositories"][0]
        self.assertEqual(row["structure"]["status"], "unknown")

    def test_partial_permissions_never_prove_full_inventory(self):
        result = module.check("meenseek", MemoryGitHub([repository()], complete=False))
        self.assertTrue(result["inventory"]["pagination_complete"])
        self.assertEqual(result["inventory"]["status"], "unknown")

    def test_coverage_failure_preserves_independent_discovery_and_page_failure_preserves_seen_repos(self):
        github = MemoryGitHub([repository()])
        with patch.object(github, "coverage", side_effect=module.ReadError("membership denied")):
            report = module.check("meenseek", github)
        self.assertEqual(len(report["repositories"]), 1)
        self.assertEqual(report["inventory"]["status"], "unknown")
        with patch.object(github, "inventory", side_effect=module.ReadError("page unavailable", [repository()])):
            report = module.check("meenseek", github)
        self.assertFalse(report["inventory"]["pagination_complete"])
        self.assertEqual(len(report["repositories"]), 1)

    def test_archived_and_empty_repositories_are_not_automatically_exempt(self):
        repos = [repository(archived=True), repository("reports", 2, empty=True)]
        rows = module.check("meenseek", MemoryGitHub(repos))["repositories"]
        self.assertEqual([r["connections"]["status"] for r in rows], ["unknown", "unknown"])

    def test_read_failure_is_unknown_not_deleted(self):
        key = ("product", "a" * 40, "README.md")
        github = MemoryGitHub([repository()], {key: (None, "access denied")})
        row = module.check("meenseek", github)["repositories"][0]
        self.assertEqual(row["entrypoints"]["README.md"]["status"], "unknown")
        self.assertEqual(row["structure"]["status"], "unknown")

    def test_unassessed_entrypoint_cannot_be_overridden_by_a_valid_other_source(self):
        oversized = blob("No recognized contract")
        oversized["byteSize"] = 1024 * 1024 + 1
        for unread in [(None, "access denied"), (oversized, None),
                       ({"__typename": "Blob", "isBinary": True}, None)]:
            files = {("product", "a" * 40, "README.md"): unread,
                     ("product", "a" * 40, "AGENTS.md"):
                     (blob("## 연결과 검증\n[Self](AGENTS.md)"), None)}
            row = module.check("meenseek", MemoryGitHub([repository()], files))["repositories"][0]
            self.assertEqual(row["structure"]["status"], "unknown")

    def test_indented_code_examples_are_not_declared_dependencies(self):
        body = "[Self](README.md)\n\n    [Example](missing.md)\n\n\t[Example2](missing2.md)\n"
        self.assertEqual(module.declared_links(body), ["README.md"])
        key = ("product", "a" * 40, "README.md")
        github = MemoryGitHub([repository()], {key: (blob("## 연결과 검증\n" + body), None)})
        row = module.check("meenseek", github)["repositories"][0]
        self.assertEqual(row["structure"]["status"], "confirmed")
        self.assertEqual(len(github.requests), 2)  # Root entrypoints only; examples never read.

    def test_ambiguous_indented_list_content_is_unknown_instead_of_an_example_failure(self):
        key = ("product", "a" * 40, "README.md")
        text = "## 연결과 검증\n- [Self](README.md)\n\n    [Nested](missing.md)\n"
        row = module.check("meenseek", MemoryGitHub([repository()], {key: (blob(text), None)}))["repositories"][0]
        self.assertEqual(row["structure"]["status"], "unknown")

    def test_cross_repo_reference_uses_current_owner_commit_not_local_folder_guess(self):
        repos = [repository(), repository(".github", 2)]
        key = ("product", "a" * 40, "README.md")
        ref = (".github", "a" * 40, "docs/repository-rules.md")
        github = MemoryGitHub(repos, {key: (blob("## 연결과 검증\n[Rules](../.github/docs/repository-rules.md)"), None),
                                     ref: (blob("Current rules"), None)})
        row = module.check("meenseek", github)["repositories"][0]
        self.assertEqual(row["references"][0]["source"]["repository"], ".github")
        self.assertEqual(row["structure"]["status"], "confirmed")

    def test_external_native_and_host_references_need_own_verification(self):
        repo = repository()
        inventory = {repo["name"]: repo}
        for target in ["/Users/someone/context.md", "vault:profile/rules.md", "https://example.org/x",
                       "../../other/private.md", "../AGENTS.md"]:
            self.assertIsNone(module.reference(target, repo, "README.md", inventory, "meenseek"))

    def test_policy_and_commit_are_bound_to_current_observation(self):
        first = module.check("meenseek", MemoryGitHub([repository()]))
        renamed = repository("renamed")
        renamed["defaultBranchRef"]["target"]["oid"] = "c" * 40
        with patch.object(module, "POLICY", Path(__file__)):
            second = module.check("meenseek", MemoryGitHub([renamed]))
        self.assertEqual(first["repositories"][0]["id"], second["repositories"][0]["id"])
        self.assertNotEqual(first["repositories"][0]["commit"], second["repositories"][0]["commit"])
        self.assertNotEqual(first["policy"]["sha256"], second["policy"]["sha256"])

    def test_fenced_examples_and_duplicate_sources_are_not_active_contracts(self):
        self.assertIsNone(module.section("```md\n## 연결과 검증\n[Fake](fake.md)\n```"))
        with self.assertRaises(module.ReadError):
            module.section("## 연결과 검증\nA\n## 연결과 검증\nB")
        self.assertEqual(module.section("## 연결과 검증\nA\n### Details\nB\n## Other\nC"),
                         "\nA\n### Details\nB\n")
        key = ("product", "a" * 40, "README.md")
        files = {key: (blob("## 연결과 검증\n[Rules](README.md)"), None),
                 ("product", "a" * 40, "AGENTS.md"): (blob("## 연결과 검증\n[Rules](README.md)"), None)}
        row = module.check("meenseek", MemoryGitHub([repository()], files))["repositories"][0]
        self.assertEqual(row["structure"]["status"], "unknown")

    def test_ambiguous_readme_cannot_be_overridden_by_an_agents_section(self):
        files = {("product", "a" * 40, "README.md"):
                 (blob("## 연결과 검증\nA\n## 연결과 검증\nB"), None),
                 ("product", "a" * 40, "AGENTS.md"):
                 (blob("## 연결과 검증\n[Contract](AGENTS.md)"), None)}
        row = module.check("meenseek", MemoryGitHub([repository()], files))["repositories"][0]
        self.assertEqual(row["structure"]["status"], "unknown")

    def test_inline_examples_escaped_links_and_comments_are_not_dependencies(self):
        self.assertEqual(module.declared_links('`[Example](missing.md)` \\[Escaped](missing.md) [Actual](README.md)'),
                         ['README.md'])
        self.assertIsNone(module.section('<!--\n## 연결과 검증\n[Fake](x.md)\n-->'))

    def test_parenthesized_escaped_and_angle_targets_are_not_truncated(self):
        self.assertEqual(module.declared_links('[A](docs/a(b).md) [B](docs/a\\(b\\).md) [C](<docs/a(b).md> "title")'),
                         ['docs/a(b).md'])

    def test_malformed_and_sensitive_links_preserve_structured_output(self):
        key = ("product", "a" * 40, "README.md")
        text = '## 연결과 검증\n[Invalid](https://[invalid)\n[Secret](https://user:secret@example.com/x?token=sensitive)'
        report = module.check("meenseek", MemoryGitHub([repository()], {key: (blob(text), None)}))
        output = json.dumps(report)
        self.assertEqual(report["repositories"][0]["structure"]["status"], "unknown")
        for secret in ['sensitive', 'user:secret']:
            self.assertNotIn(secret, output)

    def test_selected_repository_still_uses_whole_inventory(self):
        result = module.check("meenseek", MemoryGitHub([repository(), repository("second", 2)]), "second")
        self.assertEqual(result["inventory"]["visible_count"], 2)
        self.assertEqual([r["name"] for r in result["repositories"]], ["meenseek/second"])
        result = module.check("meenseek", MemoryGitHub([repository()]), "inaccessible")
        self.assertIn("selection_error", result)


class GitHubBoundaryTests(unittest.TestCase):
    def test_inventory_paginates_past_100_and_requires_final_cursor(self):
        pages = [dict(nodes=[repository(f"p{i}", i + 1) for i in range(100)],
                      pageInfo={"hasNextPage": True, "endCursor": "next"}),
                 dict(nodes=[repository(f"p{i}", i + 1) for i in range(100, 130)],
                      pageInfo={"hasNextPage": False, "endCursor": "end"})]
        github = module.GitHub()
        with patch.object(github, "graphql", side_effect=[{"organization": {"repositories": p}} for p in pages]) as call:
            self.assertEqual(len(github.inventory("meenseek")), 130)
            self.assertIn('after:"next"', call.call_args[0][0])
        pages[0]["pageInfo"]["endCursor"] = None
        with patch.object(github, "graphql", return_value={"organization": {"repositories": pages[0]}}):
            with self.assertRaises(module.ReadError):
                github.inventory("meenseek")

    def test_object_reads_are_batched_and_partial_graphql_is_not_absence(self):
        github = module.GitHub()
        requests = [("p", "a" * 40, f"docs/{i}.md") for i in range(130)]
        with patch.object(github, "graphql", return_value={}) as call:
            result = github.objects("meenseek", requests)
            self.assertEqual(call.call_count, 4)
            self.assertTrue(all(error for _, error in result.values()))
        response = subprocess.CompletedProcess([], 0, json.dumps({"data": {}, "errors": [{"message": "denied"}]}), "")
        with patch.object(module.subprocess, "run", return_value=response):
            with self.assertRaises(module.ReadError):
                github.graphql("query { viewer { login } }")

    def test_full_coverage_requires_classic_repo_scope_and_org_admin(self):
        github = module.GitHub()
        with patch.object(github, "command", return_value="X-Oauth-Scopes: repo, read:org\n"), \
                patch.object(github, "api", return_value={"state": "active", "role": "admin"}):
            self.assertEqual(github.coverage("meenseek")["status"], "confirmed")
        with patch.object(github, "command", return_value="HTTP/2 200\n"), \
                patch.object(github, "api", return_value={"state": "active", "role": "admin"}):
            self.assertEqual(github.coverage("meenseek")["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
