"""Actual isolated Git/files/process/HTTP observations, not production approval."""

import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_completion.py"
spec = importlib.util.spec_from_file_location("completion", SCRIPT)
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.remote = self.root / "remote.git"
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Synthetic completion test")
        self.git("config", "user.email", "completion@example.invalid")
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        (self.repo / "tool.py").write_text("print('reviewed tool')\n")
        self.git("add", "tool.py"); self.git("commit", "-m", "test: establish source")
        self.git("remote", "add", "origin", str(self.remote)); self.git("push", "origin", "main")
        self.owner = self.root / "owner.md"
        self.owner.write_text("Synthetic owner: local Python tool, no service deployment.\n")
        self.request = self.root / "request.txt"
        self.request.write_text("Synthetic instruction: finish the reviewed local tool.\n")
        self.installed = self.root / "installed.py"
        self.installed.symlink_to(self.repo / "tool.py")
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.scope_path = self.root / "scope.json"
        self.state_path = self.root / "state.json"
        self.review_path = self.root / "scope-review.json"
        self.final_review_path = self.root / "final-review.json"
        self.result_path = self.root / "result.json"
        self.seed = {"task_id": "synthetic-task", "session_id": "synthetic-session",
                     "request": self.ref(self.request), "request_digest": "0" * 64,
                     "owners": [self.ref(self.owner)],
                     "checks": [
                         {"id": "source", "category": "source", "kind": "git_source",
                          "target": {"repository": str(self.repo), "remote": "origin", "main": "main", "files": ["tool.py"]}},
                         {"id": "installed", "category": "usage", "kind": "installed_file",
                          "target": {"path": str(self.installed), "source": str(self.repo / "tool.py")}}],
                     "categories": {}, "repositories": [str(self.repo)],
                     "workspace": str(Path.cwd()),
                     "artifact_roots": [str(self.artifacts)], "state_path": str(self.state_path)}
        self.reset_categories()
        self.freeze()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True).stdout.decode().strip()

    def ref(self, filename):
        return {"path": str(filename), "sha256": hashlib.sha256(filename.read_bytes()).hexdigest()}

    def write(self, filename, value):
        filename.write_bytes(c.canonical(value) + b"\n")

    def reset_categories(self):
        for name in c.CATEGORIES:
            ids = [x["id"] for x in self.seed["checks"] if x["category"] == name]
            self.seed["categories"][name] = {"checks": ids, "not_applicable": None if ids else
                                           {"reason": "Excluded by the synthetic owner contract", "basis": [self.ref(self.owner)]}}

    def freeze(self):
        self.write(self.scope_path, c.prepare(self.seed))
        self.scope_ref = self.ref(self.scope_path)
        self.write(self.review_path, {"scope_sha256": self.scope_ref["sha256"], "status": "No Findings"})
        self.state = {"task_id": self.seed["task_id"], "session_id": self.seed["session_id"], "intent": "closure",
                      "scope": self.scope_ref, "review": self.ref(self.review_path), "result_path": str(self.result_path),
                      "final_review": None,
                      "resources": [], "outcome": "working"}
        self.write(self.state_path, self.state)
        revision = self.git("rev-parse", "main")
        self.result = {"scope_sha256": self.scope_ref["sha256"], "exceptions": {}, "checks": {
            "source": {"local_revision": revision, "remote_revision": revision,
                       "files": {name: self.ref(self.repo / name)["sha256"] for name in self.seed["checks"][0]["target"]["files"]}},
            "installed": {"sha256": self.ref(self.repo / "tool.py")["sha256"]}}}
        self.write(self.result_path, self.result)
        self.final_review()

    def final_review(self):
        reviewed = {"scope_sha256": self.scope_ref["sha256"], "status": "No Findings",
                    "result_sha256": self.ref(self.result_path)["sha256"]}
        if "source_preservation" in self.state:
            reviewed["source_preservation_sha256"] = self.state["source_preservation"]["evidence"]["sha256"]
        self.write(self.final_review_path, reviewed)
        self.state["final_review"] = self.ref(self.final_review_path)
        self.write(self.state_path, self.state)

    def inspect(self):
        return c.check_scope(self.scope_ref, str(self.result_path), task_id=self.seed["task_id"], request_digest="0" * 64)

    def retained_classification(self):
        return {"owner": "synthetic task", "reason": "Keep reviewed originals and their exact provenance. " * 6,
                "scope": "Explicit resource keys only", "removal_condition": "Reviewed owner retention change",
                "basis": [self.ref(self.owner)]}

    def grouped_result(self, items):
        self.result["exception_groups"] = [{"items": items, "classification": self.retained_classification()}]
        self.write(self.result_path, self.result); self.final_review()

    def test_exception_groups_preserve_large_exact_inventory_with_bounded_input_and_report(self):
        items = []
        for number in range(4000):
            filename = self.artifacts / (f"asset-{number:04d}-" + "retained-original-" * 4)
            filename.write_bytes(b"original")
            items.append("path:" + str(filename))
        inline = {**self.result, "exceptions": {key: self.retained_classification() for key in items}}
        self.assertGreater(len(c.canonical(inline)), c.MAX_BYTES)
        frozen = self.scope_path.read_bytes()
        self.grouped_result(items)
        self.assertLess(self.result_path.stat().st_size, c.MAX_BYTES)
        answer = c.check_scope(self.scope_ref, str(self.result_path), task_id=self.seed["task_id"],
                               request_digest="0" * 64, report=True)
        self.assertTrue(answer["complete"], answer["failures"])
        self.assertLess(len(c.canonical(answer)), c.MAX_BYTES)
        self.assertEqual(answer["exception_groups"], self.result["exception_groups"])
        self.assertEqual(self.scope_path.read_bytes(), frozen)

    def test_exception_groups_do_not_cover_omissions_or_prefixes(self):
        (self.artifacts / "retained.txt").write_text("original")
        missing = self.artifacts / "unclassified.txt"
        missing.write_text("also original")
        self.grouped_result(["path:" + str(self.artifacts / "retained.txt"), "path:" + str(self.artifacts) + "/*"])
        answer = self.inspect()
        self.assertFalse(answer["complete"])
        self.assertIn("path:" + str(missing), [item["id"] for item in answer["failures"]])

    def test_exception_groups_reject_overlap_empty_and_invalid_classification(self):
        key = "path:" + str(self.artifacts / "retained.txt")
        good = {"items": [key], "classification": self.retained_classification()}
        bad_groups = [[{**good, "items": [key, key]}], [good, good],
                      [{**good, "items": []}], [{**good, "classification": {"owner": "incomplete"}}]]
        for groups in bad_groups:
            with self.subTest(groups=groups):
                self.result["exception_groups"] = groups
                self.write(self.result_path, self.result); self.final_review()
                with self.assertRaises(c.CompletionError): self.inspect()
        self.result["exceptions"] = {key: self.retained_classification()}
        self.grouped_result([key])
        with self.assertRaises(c.CompletionError): self.inspect()

    def test_exception_groups_remain_bound_to_the_exact_final_review(self):
        self.grouped_result(["path:" + str(self.artifacts / "retained.txt")])
        self.result["exception_groups"][0]["items"].append("path:" + str(self.artifacts / "added.txt"))
        self.write(self.result_path, self.result)
        with self.assertRaisesRegex(c.CompletionError, "final review"): self.inspect()

    def test_exception_groups_keep_the_exact_input_limit(self):
        self.grouped_result(["path:" + str(self.artifacts / "retained.txt")])
        payload = self.result_path.read_bytes()
        self.result_path.write_bytes(payload + b" " * (c.MAX_BYTES - len(payload)))
        self.final_review()
        self.assertTrue(self.inspect()["complete"])
        self.result_path.write_bytes(self.result_path.read_bytes() + b" ")
        with self.assertRaises(c.CompletionError): self.inspect()

    def test_exception_basis_is_rechecked_after_resource_inventory(self):
        basis = self.root / "classification-basis.md"
        basis.write_text("Reviewed retention decision")
        classification = self.retained_classification()
        classification["basis"] = [self.ref(basis)]
        self.result["exceptions"] = {"path:" + str(self.artifacts / "retained.txt"): classification}
        self.write(self.result_path, self.result); self.final_review()
        original_tree = c.tree
        def changed(root):
            result = original_tree(root)
            basis.write_text("Changed while checking resources")
            return result
        with mock.patch.object(c, "tree", side_effect=changed):
            with self.assertRaises(c.CompletionError): self.inspect()

    def snapshot_recovery_fixture(self):
        frozen = self.scope_path.read_bytes()
        before = self.root / "owner-before.md"
        before.write_bytes(self.owner.read_bytes())
        self.owner.write_text("Reviewed current operating owner, with original retained.\n")
        current = self.root / "owner-current.md"
        current.write_bytes(self.owner.read_bytes())
        self.recovery_path = self.root / "recovery.json"
        self.recovery_review_path = self.root / "recovery-review.json"
        self.recovery = {"scope_sha256": self.scope_ref["sha256"], "task_id": self.seed["task_id"],
                         "session_id": self.seed["session_id"], "authorization": self.ref(self.request),
                         "owners": [{"owner_path": str(self.owner), "snapshots": {
                             "original": self.ref(before), "current": self.ref(current)}}]}
        self.bind_snapshot_recovery()
        return frozen, before, current

    def bind_snapshot_recovery(self):
        self.write(self.recovery_path, self.recovery)
        evidence = self.ref(self.recovery_path)
        self.write(self.recovery_review_path, {"scope_sha256": self.scope_ref["sha256"],
                                             "recovery_sha256": evidence["sha256"], "status": "No Findings"})
        self.state["owner_recovery"] = {"evidence": evidence, "review": self.ref(self.recovery_review_path)}
        self.write(self.final_review_path, {"scope_sha256": self.scope_ref["sha256"],
                                           "result_sha256": self.ref(self.result_path)["sha256"],
                                           "owner_recovery_sha256": evidence["sha256"], "status": "No Findings"})
        self.state["final_review"] = self.ref(self.final_review_path)
        self.write(self.state_path, self.state)

    def test_snapshot_recovery_preserves_unversioned_original_and_frozen_scope(self):
        frozen, before, current = self.snapshot_recovery_fixture()
        self.assertTrue(self.inspect()["complete"])
        self.assertEqual(c.draft_result(self.scope_ref), self.result)
        self.assertEqual(self.scope_path.read_bytes(), frozen)
        self.assertEqual(self.ref(before)["sha256"], self.seed["owners"][0]["sha256"])
        self.assertEqual(current.read_bytes(), self.owner.read_bytes())
        self.installed.unlink(); self.installed.write_text("stale installation")
        self.assertFalse(self.inspect()["complete"])

    def test_snapshot_recovery_rejects_forged_original_new_owner_and_new_drift(self):
        frozen, before, current = self.snapshot_recovery_fixture()
        original = copy.deepcopy(self.recovery)
        before.write_text("Not the frozen original")
        self.recovery["owners"][0]["snapshots"]["original"] = self.ref(before)
        self.bind_snapshot_recovery()
        with self.assertRaises(c.CompletionError): self.inspect()
        self.recovery = copy.deepcopy(original)
        before.write_text("Synthetic owner: local Python tool, no service deployment.\n")
        self.recovery["owners"][0]["owner_path"] = str(self.request)
        self.bind_snapshot_recovery()
        with self.assertRaises(c.CompletionError): self.inspect()
        self.recovery = original; self.bind_snapshot_recovery()
        self.owner.write_text("Unreviewed later change")
        with self.assertRaises(c.CompletionError): self.inspect()

    def test_snapshot_recovery_can_mix_with_existing_git_provenance(self):
        frozen = self.recovery_fixture(two_owners=True)
        entry = self.recovery["owners"][1]
        before = self.root / "second-before.md"
        before.write_bytes(subprocess.check_output(["git", "-C", str(self.repo), "show",
                           entry["original"]["revision"] + ":" + entry["original"]["file"]]))
        current = self.root / "second-current.md"
        current.write_bytes(Path(entry["owner_path"]).read_bytes())
        self.recovery["owners"][1] = {"owner_path": entry["owner_path"], "snapshots": {
            "original": self.ref(before), "current": self.ref(current)}}
        self.bind_snapshot_recovery()
        self.assertTrue(self.inspect()["complete"])
        self.assertEqual(self.scope_path.read_bytes(), frozen)

    def test_snapshot_recovery_rejects_planned_owner_and_snapshot_tampering(self):
        self.owner_edit_fixture()
        self.snapshot_recovery_fixture()
        with self.assertRaises(c.CompletionError): self.inspect()
        self.setUp_snapshot_tampering_case()

    def setUp_snapshot_tampering_case(self):
        # Use a separate ordinary raw-hash owner, without changing the Git-bound owner fixture.
        owner = self.root / "another-owner.md"
        owner.write_text("Frozen ordinary owner")
        self.owner = owner; self.seed["owners"] = [self.ref(owner)]
        self.reset_categories(); self.freeze()
        frozen, before, current = self.snapshot_recovery_fixture()
        current.write_text("Changed retained snapshot")
        with self.assertRaises(c.CompletionError): self.inspect()

    def test_real_observations_are_read_only_and_complete(self):
        before = {p: p.read_bytes() for p in (self.scope_path, self.state_path, self.result_path, self.repo / ".git/index")}
        answer = self.inspect()
        self.assertTrue(answer["complete"], answer)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertIn("owner judgments", answer["assurance"])

    def test_large_binary_source_draft_and_check_preserve_exact_bytes(self):
        png = self.repo / "large.png"
        payload = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8193
        png.write_bytes(payload)
        self.git("add", "large.png"); self.git("commit", "-m", "test: add large binary source")
        self.git("push", "origin", "main")
        self.seed["checks"][0]["target"]["files"].append("large.png")
        self.freeze()
        candidate = c.draft_result(self.scope_ref)
        self.assertEqual(candidate, self.result)
        self.assertEqual(candidate["checks"]["source"]["files"]["large.png"],
                         hashlib.sha256(payload).hexdigest())
        self.assertTrue(self.inspect()["complete"])
        with png.open("r+b") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(c.CompletionError, "working source bytes differ"):
            c.observe(json.loads(self.scope_path.read_bytes())["checks"][0],
                      self.result["checks"]["source"])

    def test_source_hash_has_no_replacement_size_cap(self):
        source = self.root / "large-source.bin"
        with source.open("wb") as stream:
            stream.write(b"large source")
            stream.truncate(129 * c.MAX_BYTES)
        with source.open("rb") as stream:
            expected = hashlib.file_digest(stream, "sha256").hexdigest()
        self.assertEqual(c.working_source_sha(source), expected)
        self.assertEqual(c.working_source_sha(self.installed), self.ref(self.repo / "tool.py")["sha256"])

    def test_large_installed_copy_draft_and_check_preserve_exact_bytes(self):
        source = self.repo / "tool.py"
        payload = bytes(range(256)) * 8193
        source.write_bytes(payload)
        self.git("commit", "-am", "test: add large installed binary")
        self.git("push", "origin", "main")
        self.installed.unlink(); self.installed.write_bytes(payload)
        self.freeze()
        self.assertEqual(c.draft_result(self.scope_ref), self.result)
        self.assertTrue(self.inspect()["complete"])
        check = json.loads(self.scope_path.read_bytes())["checks"][1]
        expected = self.result["checks"]["installed"]
        self.installed.write_bytes(payload[:-1] + b"x")
        with self.assertRaisesRegex(c.CompletionError, "file differs from reviewed bytes"):
            c.observe(check, expected)
        self.installed.write_bytes(payload)
        source.write_bytes(payload[:-1] + b"x")
        with self.assertRaisesRegex(c.CompletionError, "installed and owner source bytes differ"):
            c.observe(check, expected)

    def test_installed_binary_reads_keep_the_existing_binary_limit(self):
        oversized = self.root / "oversized-binary"
        with oversized.open("wb") as stream:
            stream.truncate(129 * c.MAX_BYTES)
        self.installed.unlink(); self.installed.symlink_to(oversized)
        with self.assertRaisesRegex(c.CompletionError, "bounded regular file"):
            c.draft_result(self.scope_ref)
        check = json.loads(self.scope_path.read_bytes())["checks"][1]
        with self.assertRaisesRegex(c.CompletionError, "bounded regular file"):
            c.observe(check, self.result["checks"]["installed"])
        self.installed.unlink(); self.installed.symlink_to(self.repo / "tool.py")
        check["target"]["source"] = str(oversized)
        with self.assertRaisesRegex(c.CompletionError, "bounded regular file"):
            c.observe(check, self.result["checks"]["installed"])

    def test_source_hash_rejects_missing_blob_nonregular_file_and_timeout(self):
        revision = self.git("rev-parse", "main")
        with self.assertRaisesRegex(c.CompletionError, "source observation failed"):
            c.git_source_sha(str(self.repo), revision, "missing.png")
        fifo = self.root / "source-fifo"
        c.os.mkfifo(fifo)
        with self.assertRaisesRegex(c.CompletionError, "regular file"):
            c.working_source_sha(fifo)
        with mock.patch.object(c.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", c.TIMEOUT)):
            with self.assertRaisesRegex(c.CompletionError, "source observation unavailable or timed out"):
                c.git_source_sha(str(self.repo), revision, "tool.py")

    def test_metadata_and_policy_byte_limits_remain_enforced(self):
        policy = self.root / "oversized.json"
        policy.write_bytes(b" " * (c.MAX_BYTES + 1))
        with self.assertRaises(c.CompletionError):
            c.raw_file(str(policy))
        with self.assertRaisesRegex(c.CompletionError, "exceeded output limit"):
            c.run([sys.executable, "-I", "-c",
                   "import sys; sys.stdout.buffer.write(b'x' * (1024 * 1024 + 1))"])

    def test_draft_actual_cli_collects_candidate_without_claiming_completion(self):
        output = self.root / "candidate.json"
        before = self.state_path.read_bytes()
        done = subprocess.run([sys.executable, "-I", str(SCRIPT), "draft", "--scope", str(self.scope_path),
                               "--sha256", self.scope_ref["sha256"], "--output", str(output)], capture_output=True)
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertTrue(json.loads(done.stdout)["review_required"])
        self.assertEqual(json.loads(output.read_bytes()), self.result)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertNotIn("complete", json.loads(done.stdout))
        again = subprocess.run([sys.executable, "-I", str(SCRIPT), "draft", "--scope", str(self.scope_path),
                                "--sha256", self.scope_ref["sha256"], "--output", str(output)], capture_output=True)
        self.assertEqual(again.returncode, 2)

    def test_draft_keeps_committed_source_and_cannot_accept_dirty_or_new_resources(self):
        committed = self.result["checks"]["source"]["files"]["tool.py"]
        (self.repo / "tool.py").write_text("uncommitted local bytes")
        (self.artifacts / "temporary.txt").write_text("unclassified")
        self.result = c.draft_result(self.scope_ref)
        self.assertEqual(self.result["checks"]["source"]["files"]["tool.py"], committed)
        self.assertEqual(self.result["exceptions"], {})
        self.write(self.result_path, self.result); self.final_review()
        answer = self.inspect()
        self.assertFalse(answer["complete"])
        self.assertTrue(any(f["id"].startswith("path:") for f in answer["failures"]))

    def test_draft_http_expectations_are_explicit_and_never_observed_from_endpoint(self):
        self.seed["checks"].append({"id":"live", "category":"deployment", "kind":"http_json",
                                   "target":{"url":"http://127.0.0.1:1/live", "revision_field":"revision", "health_field":"healthy"}})
        self.reset_categories()
        self.write(self.scope_path, c.prepare(self.seed)); self.scope_ref = self.ref(self.scope_path)
        for expected in (None, {"unknown":{}}, {"live":{"revision":"old", "health":False}}):
            with self.assertRaises(c.CompletionError): c.draft_result(self.scope_ref, expected)
        wanted = {"revision":"owner-selected-revision", "health":True}
        with mock.patch.object(c, "build_opener", side_effect=AssertionError("draft must not choose running health")):
            self.assertEqual(c.draft_result(self.scope_ref, {"live":wanted})["checks"]["live"], wanted)

    def test_draft_owner_updates_still_require_exact_independent_review(self):
        self.owner_edit_fixture(); self.update_owner_result()
        self.result = c.draft_result(self.scope_ref)
        self.assertEqual(self.result["owner_updates"], {str(self.owner):self.ref(self.owner)["sha256"]})
        self.write(self.result_path, self.result)
        self.final_review_path.write_text("{}")
        self.state["final_review"] = self.ref(self.final_review_path); self.write(self.state_path, self.state)
        with self.assertRaises(c.CompletionError): self.inspect()
        self.final_review()
        self.assertTrue(self.inspect()["complete"])

    def delete_planned_owner(self, push=True):
        self.git("mv", "README.md", "guide.md")
        self.git("commit", "-m", "test: move reviewed owner")
        if push:
            self.git("push", "origin", "main")
        self.result = c.draft_result(self.scope_ref)
        self.write(self.result_path, self.result)
        self.final_review()

    def test_reviewed_owner_rename_preserves_scope_and_checks_original_path_absence(self):
        self.owner_edit_fixture()
        original = self.scope_path.read_bytes()
        self.delete_planned_owner()
        self.assertIsNone(self.result["checks"]["source"]["files"]["README.md"])
        self.assertEqual(self.result["owner_updates"], {str(self.owner): None})
        self.assertTrue(self.inspect()["complete"])
        self.assertEqual(self.scope_path.read_bytes(), original)
        self.assertEqual((self.repo / "guide.md").read_text(), "Synthetic original local tool contract.\n")
        self.final_review_path.write_text("{}")
        self.state["final_review"] = self.ref(self.final_review_path)
        self.write(self.state_path, self.state)
        with self.assertRaisesRegex(c.CompletionError, "final review"):
            self.inspect()

    def test_deleted_owner_reappearance_as_file_directory_or_dangling_symlink_refuses(self):
        self.owner_edit_fixture()
        self.delete_planned_owner()
        for kind in ("file", "directory", "symlink"):
            with self.subTest(kind=kind):
                if kind == "file": self.owner.write_text("unreviewed reappearance")
                elif kind == "directory": self.owner.mkdir()
                else: self.owner.symlink_to(self.root / "missing")
                try:
                    with self.assertRaisesRegex(c.CompletionError, "deleted owner reappeared"):
                        self.inspect()
                finally:
                    if kind == "directory": self.owner.rmdir()
                    else: self.owner.unlink()

    def test_deleted_owner_still_present_in_remote_refuses(self):
        self.owner_edit_fixture()
        self.delete_planned_owner(push=False)
        answer = self.inspect()
        self.assertFalse(answer["complete"])
        self.assertTrue(any("deletion is not preserved" in x["reason"] for x in answer["failures"]))

    def test_unplanned_source_or_owner_deletion_is_not_an_absence_expectation(self):
        self.result["checks"]["source"]["files"]["tool.py"] = None
        self.write(self.result_path, self.result)
        self.final_review()
        with self.assertRaisesRegex(c.CompletionError, "not declared"):
            self.inspect()
        (self.repo / "tool.py").unlink()
        self.git("add", "-u"); self.git("commit", "-m", "test: undeclared source deletion")
        self.git("push", "origin", "main")
        with self.assertRaises((c.CompletionError, FileNotFoundError)):
            c.draft_result(self.scope_ref)
        with self.assertRaisesRegex(c.CompletionError, "not declared"):
            c.current_owners([self.ref(self.owner)], {str(self.owner): None})

    def test_git_absence_lookup_errors_are_not_deletions(self):
        with self.assertRaises(c.CompletionError):
            c.git_source_present(str(self.repo), "f" * 40, "README.md")
        with mock.patch.object(c.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", c.TIMEOUT)):
            with self.assertRaises(c.CompletionError):
                c.git_source_present(str(self.repo), self.result["checks"]["source"]["local_revision"], "README.md")

    def test_report_binds_all_categories_expectations_and_reviewed_result(self):
        answer = c.check_scope(self.scope_ref, str(self.result_path), report=True)
        self.assertEqual(answer["categories"], json.loads(self.scope_path.read_bytes())["categories"])
        self.assertEqual(answer["expectations"], self.result["checks"])
        self.assertEqual(answer["result"], self.ref(self.result_path))
        self.assertEqual(answer["final_review"], self.ref(self.final_review_path))
        self.assertNotIn("expectations", self.inspect())  # Existing queue/host output stays compact.

    def test_new_branch_and_dirty_file_and_stash_prevent_completion(self):
        self.git("branch", "feature-unclassified")
        (self.repo / "temporary.txt").write_text("still here")
        (self.repo / "tool.py").write_text("local change")
        self.git("stash", "push", "-m", "unique task state")
        answer = self.inspect()
        self.assertFalse(answer["complete"])
        ids = [f["id"] for f in answer["failures"]]
        for kind in (":ref:", ":dirty:", ":stash:"):
            self.assertTrue(any(kind in key for key in ids), ids)
        self.assertTrue((self.repo / "temporary.txt").exists())
        self.assertTrue(self.git("stash", "list"))

    def test_preexisting_other_resources_remain_without_being_deleted(self):
        self.git("branch", "feature-other-owner")
        (self.repo / "other.txt").write_text("preexisting")
        self.freeze()
        self.assertTrue(self.inspect()["complete"])
        self.assertTrue((self.repo / "other.txt").exists())
        self.assertIn("feature-other-owner", self.git("branch", "--list"))

    def test_squash_uses_content_not_feature_ancestry(self):
        self.git("switch", "-c", "feature-task")
        (self.repo / "tool.py").write_text("print('new reviewed tool')\n")
        self.git("commit", "-am", "test: update task")
        feature = self.git("rev-parse", "HEAD")
        self.git("switch", "main"); self.git("merge", "--squash", "feature-task")
        self.git("commit", "-m", "test: integrate reviewed bytes")
        self.git("branch", "-D", "feature-task"); self.git("push", "origin", "main")
        self.assertNotEqual(self.git("rev-parse", "HEAD"), feature)
        revision = self.git("rev-parse", "main")
        wanted = self.ref(self.repo / "tool.py")["sha256"]
        self.result["checks"]["source"] = {"local_revision": revision, "remote_revision": revision, "files": {"tool.py": wanted}}
        self.result["checks"]["installed"] = {"sha256": wanted}
        self.write(self.result_path, self.result)
        self.final_review()
        self.assertTrue(self.inspect()["complete"])

    def test_stale_installed_copy_and_remote_main_drift_fail(self):
        self.installed.unlink(); self.installed.write_text("old installed tool")
        (self.repo / "tool.py").write_text("new reviewed tool")
        self.git("commit", "-am", "test: new revision"); self.git("push", "origin", "main")
        answer = self.inspect()
        self.assertFalse(answer["complete"])
        self.assertEqual({x["id"] for x in answer["failures"]}, {"source", "installed"})

    def test_all_categories_and_bound_na_basis_are_required(self):
        seed = copy.deepcopy(self.seed)
        del seed["categories"]["deployment"]
        with self.assertRaises(c.CompletionError): c.prepare(seed)
        seed = copy.deepcopy(self.seed)
        seed["categories"]["deployment"]["not_applicable"]["basis"] = [self.ref(self.request)]
        with self.assertRaisesRegex(c.CompletionError, "owner contract"): c.prepare(seed)

    def test_omitted_obligation_and_self_declared_success_are_rejected(self):
        self.result["checks"].pop("installed"); self.write(self.result_path, self.result)
        with self.assertRaises(c.CompletionError): self.inspect()
        self.write(self.result_path, {"complete": True})
        result = subprocess.run([sys.executable, str(SCRIPT), "check", "--scope", str(self.scope_path),
                                 "--sha256", self.scope_ref["sha256"], "--result", str(self.result_path)], capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)["complete"])

    def test_wrong_task_request_scope_and_review_bytes_fail(self):
        for args in ({"task_id": "another-task"}, {"request_digest": "1" * 64}):
            with self.assertRaises(c.CompletionError): c.check_scope(self.scope_ref, str(self.result_path), **args)
        with self.assertRaises(c.CompletionError): c.check_scope({**self.scope_ref, "sha256": "1" * 64}, str(self.result_path))
        self.write(self.review_path, {"scope_sha256": "1" * 64, "status": "No Findings"})
        self.state["review"] = self.ref(self.review_path); self.write(self.state_path, self.state)
        with self.assertRaises(c.CompletionError): self.inspect()

    def test_scope_and_owner_drift_are_not_reused(self):
        self.owner.write_text("changed owner policy")
        with self.assertRaises(c.CompletionError): self.inspect()

    def test_future_artifact_and_pending_resource_reservation_fail(self):
        new = self.artifacts / "new.bin"; new.write_bytes(b"newly generated")
        self.state["resources"].append({"id": "helper", "kind": "reserved", "target": {}, "basis": self.ref(self.owner)})
        self.write(self.state_path, self.state)
        answer = self.inspect()
        self.assertFalse(answer["complete"])
        self.assertEqual(len(answer["failures"]), 2)
        self.assertTrue(new.exists())

    def test_live_process_and_pid_reuse_are_observed_without_killing(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"], stdout=subprocess.PIPE)
        def close_child():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
            child.stdout.close()
        self.addCleanup(close_child)
        self.assertEqual(child.stdout.readline(), b"ready\n")
        fake = self.root / "ps"; fake.write_text("#!/bin/sh\nexit 1\n"); fake.chmod(0o700)
        with mock.patch.dict(c.os.environ, {"PATH": str(self.root)}):
            identity = c.process_identity(child.pid)
        self.assertIsNotNone(identity)
        definition = {"kind": "process_absent", "target": {"pid": child.pid, "identity": identity}}
        with self.assertRaises(c.CompletionError): c.observe(definition, {})
        self.assertIsNone(child.poll())
        child.terminate(); child.wait(timeout=3)
        self.assertTrue(c.observe(definition, {})["absent"])
        with mock.patch.object(c, "process_identity", return_value=identity[:20] + "1901" + identity[24:]):
            self.assertTrue(c.observe(definition, {})["pid_reused"])
        with mock.patch.object(c, "process_identity", return_value=identity[:24] + " another executable"):
            with self.assertRaises(c.CompletionError): c.observe(definition, {})

    def test_input_changes_during_observation_are_rejected(self):
        original = c.observe
        def drift(check, expected):
            answer = original(check, expected)
            if check["id"] == "installed":
                self.state["resources"].append({"id": "late", "kind": "reserved", "target": {}, "basis": self.ref(self.owner)})
                self.write(self.state_path, self.state)
            return answer
        with mock.patch.object(c, "observe", side_effect=drift):
            with self.assertRaisesRegex(c.CompletionError, "changed during"): self.inspect()

    def test_http_checks_actual_revision_health_and_refuses_redirect(self):
        class Handler(BaseHTTPRequestHandler):
            value = {"revision": "reviewed", "healthy": True}
            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302); self.send_header("Location", "/live"); self.end_headers(); return
                self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(self.value).encode())
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        target = {"url": f"http://127.0.0.1:{server.server_port}/live", "revision_field": "revision", "health_field": "healthy"}
        definition = {"kind": "http_json", "target": target}
        expected = {"revision": "reviewed", "health": True}
        with mock.patch.dict(c.os.environ, {"HTTP_PROXY":"http://127.0.0.1:1", "NO_PROXY":""}):
            self.assertEqual(c.observe(definition, expected), expected)
        Handler.value = {}
        with self.assertRaises(c.CompletionError): c.observe(definition, {"revision": None, "health": None})
        with self.assertRaises(c.CompletionError): c.observe(definition, expected)
        Handler.value = {"revision": "old", "healthy": True}
        with self.assertRaises(c.CompletionError): c.observe(definition, expected)
        target["url"] = target["url"].replace("/live", "/redirect")
        with self.assertRaises(c.CompletionError): c.observe(definition, expected)

    def test_local_stash_absence_observes_live_and_packed_refs_read_only(self):
        definition = {"kind": "ref_absent", "target": {"repository": str(self.repo), "ref": "refs/stash"}}
        self.assertTrue(c.observe(definition, {})["absent"])
        (self.repo / "scratch.txt").write_text("unique local work")
        self.git("stash", "push", "-u", "-m", "synthetic local work")
        for packed in (False, True):
            with self.subTest(packed=packed):
                if packed: self.git("pack-refs", "--all")
                before = self.git("rev-parse", "refs/stash")
                with self.assertRaisesRegex(c.CompletionError, "owned stash remains"):
                    c.observe(definition, {})
                self.assertEqual(self.git("rev-parse", "refs/stash"), before)
        self.git("stash", "pop")
        self.assertEqual((self.repo / "scratch.txt").read_text(), "unique local work")
        self.assertTrue(c.observe(definition, {})["absent"])

    def test_stash_ref_without_reflog_is_not_mistaken_for_absence(self):
        definition = {"kind": "ref_absent", "target": {"repository": str(self.repo), "ref": "refs/stash"}}
        self.git("update-ref", "refs/stash", self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("stash", "list"), "")
        with self.assertRaisesRegex(c.CompletionError, "owned stash remains"):
            c.observe(definition, {})

    def test_unsupported_ref_observers_are_rejected_during_prepare(self):
        for kind, ref in (("ref_absent", "refs/tags/example"),
                          ("ref_absent", "refs/stash/example"),
                          ("remote_ref_absent", "refs/stash")):
            with self.subTest(kind=kind, ref=ref):
                seed = copy.deepcopy(self.seed)
                seed["checks"].append({"id": "unsupported", "category": "temporary_resources", "kind": kind,
                                       "target": {"repository": str(self.repo), "remote": "origin", "ref": ref}})
                seed["categories"]["temporary_resources"] = {"checks": ["unsupported"], "not_applicable": None}
                with self.assertRaisesRegex(c.CompletionError, "only exact branch refs"):
                    c.prepare(seed)

    def test_remote_branch_absence_is_current_and_does_not_delete(self):
        self.git("push", "origin", "main:refs/heads/feature-task")
        definition = {"kind": "remote_ref_absent", "target": {"repository": str(self.repo), "remote": "origin", "ref": "refs/heads/feature-task", "remote_binding": c.remote_identity(str(self.repo), "origin")}}
        with self.assertRaises(c.CompletionError): c.observe(definition, {})
        self.assertIn("feature-task", self.git("ls-remote", "--heads", "origin"))
        self.git("push", "origin", "--delete", "feature-task")
        self.assertTrue(c.observe(definition, {})["absent"])

    def test_unrelated_resource_removal_and_bytes_loss_are_detected(self):
        self.git("branch", "other")
        (self.repo / "other.txt").write_text("unique existing content")
        (self.artifacts / "other.txt").write_text("unique artifact")
        self.freeze()
        self.git("branch", "-D", "other")
        (self.repo / "other.txt").write_text("lost content")
        (self.artifacts / "other.txt").unlink()
        answer = self.inspect()
        self.assertFalse(answer["complete"])
        for part in (":ref:refs/heads/other", ":content:other.txt", "path:"):
            self.assertTrue(any(part in failure["id"] for failure in answer["failures"]), answer)

    def test_staged_only_and_mixed_worktree_index_loss_are_detected(self):
        for mixed in (False, True):
            with self.subTest(mixed=mixed):
                other = self.repo / "other.txt"
                other.write_text(f"committed original {mixed}")
                self.git("add", "other.txt"); self.git("commit", "-m", "test: other owner's source")
                self.git("push", "origin", "main")
                other.write_text("unique staged change"); self.git("add", "other.txt")
                if mixed: other.write_text("unique unstaged change")
                self.freeze()
                self.assertTrue(self.inspect()["complete"])
                status = self.git("status", "--porcelain")
                working = other.read_bytes()
                other.write_text("lost staged content"); self.git("add", "other.txt")
                if mixed: other.write_bytes(working)
                self.assertEqual(self.git("status", "--porcelain"), status)
                answer = self.inspect()
                self.assertFalse(answer["complete"])
                self.assertTrue(any(f["id"].endswith(":content:other.txt") for f in answer["failures"]), answer)
                self.git("reset", "--hard", "HEAD")  # Isolated synthetic repository only.

    def owner_edit_fixture(self):
        self.owner = self.repo / "README.md"
        self.owner.write_text("Synthetic original local tool contract.\n")
        self.git("add", "README.md"); self.git("commit", "-m", "test: establish owner contract")
        self.git("push", "origin", "main")
        self.seed["owners"] = [self.ref(self.owner)]
        self.seed["checks"][0]["target"]["files"].append("README.md")
        self.reset_categories(); self.freeze()

    def dirty_owner_fixture(self, freeze=True):
        self.owner_edit_fixture()
        self.owner.write_text("Synthetic inherited staged contract.\n")
        self.git("add", "README.md")
        self.owner.write_text("Synthetic inherited working contract.\n")
        self.snapshot = self.root / "preserved-owner-original.md"
        self.snapshot.write_bytes(self.owner.read_bytes())
        self.seed["owners"] = [self.ref(self.owner)]
        self.seed["owner_snapshots"] = {str(self.owner): self.ref(self.snapshot)}
        self.reset_categories()
        if freeze: self.freeze()

    def snapshot_result(self):
        self.result = c.draft_result(self.scope_ref)
        # Publication changes only the explicitly owned README's dirty entries.
        before = json.loads(self.scope_path.read_bytes())["baseline"]["repositories"][str(self.repo)]
        after = c.inventory(str(self.repo))
        keys = ["git:" + str(self.repo) + ":dirty:" + item
                for item in set(before["dirty"]) ^ set(after["dirty"]) if item.endswith(" README.md")]
        keys.append("git:" + str(self.repo) + ":content:README.md")
        self.result["exceptions"] = {key: {"owner": "synthetic task", "reason": "publish the reviewed owner edit",
                                          "scope": "README.md only", "removal_condition": "publication observed",
                                          "basis": [self.ref(self.request)]} for key in keys}
        self.write(self.result_path, self.result)
        self.final_review()

    def test_dirty_owner_snapshot_actual_cli_prepares_binds_and_completes(self):
        self.dirty_owner_fixture(freeze=False)
        self.state_path = self.root / (self.seed["session_id"] + ".json")
        self.seed["state_path"] = str(self.state_path)
        self.scope_path = self.root / "dirty-scope.json"
        seed_path = self.root / "dirty-seed.json"
        self.write(seed_path, self.seed)
        hook = [sys.executable, "-I", str(SCRIPT.with_name("completion_hook.py")), "--bindings", str(self.root)]

        def cli(argv):
            done = subprocess.run(argv, capture_output=True, timeout=35)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            return json.loads(done.stdout)

        original = self.snapshot.read_bytes()
        index = (self.repo / ".git/index").read_bytes()
        cli([*hook, "arm", "--session", self.seed["session_id"], "--task-id", self.seed["task_id"]])
        self.scope_ref = cli([sys.executable, "-I", str(SCRIPT), "prepare", "--seed", str(seed_path),
                              "--output", str(self.scope_path)])
        scope = json.loads(self.scope_path.read_bytes())
        self.assertEqual(scope["owners"][0]["original"]["snapshot"], self.ref(self.snapshot))
        self.assertNotIn("revision", scope["owners"][0]["original"])
        self.assertNotIn("owner_snapshots", scope)
        self.write(self.review_path, {"scope_sha256": self.scope_ref["sha256"], "status": "No Findings"})
        cli([*hook, "bind", "--session", self.seed["session_id"], "--expected-sha256", self.ref(self.state_path)["sha256"],
             "--scope", str(self.scope_path), "--scope-sha256", self.scope_ref["sha256"],
             "--review", str(self.review_path), "--review-sha256", self.ref(self.review_path)["sha256"]])
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(self.owner.read_bytes(), original)
        self.state = json.loads(self.state_path.read_bytes())
        self.update_owner_result(); self.snapshot_result()
        answer = cli([*hook, "complete", "--session", self.seed["session_id"],
                      "--expected-sha256", self.ref(self.state_path)["sha256"],
                      "--task-id", self.seed["task_id"], "--request-digest", self.seed["request_digest"],
                      "--result", str(self.result_path), "--review", str(self.final_review_path),
                      "--review-sha256", self.ref(self.final_review_path)["sha256"]])
        self.assertTrue(answer["complete"], answer)
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.snapshot.read_bytes(), original)
        self.assertEqual(self.ref(self.scope_path), self.scope_ref)

    def test_dirty_owner_snapshot_requires_exact_original_and_source_obligation(self):
        self.dirty_owner_fixture(freeze=False)
        cases = []
        seed = copy.deepcopy(self.seed); seed.pop("owner_snapshots")
        cases.append((seed, "requires committed original bytes"))
        seed = copy.deepcopy(self.seed); seed["owner_snapshots"][str(self.owner)] = self.ref(self.request)
        cases.append((seed, "differs from the frozen original"))
        seed = copy.deepcopy(self.seed); seed["owner_snapshots"][str(self.owner)] = self.ref(self.owner)
        cases.append((seed, "outside its source repository"))
        seed = copy.deepcopy(self.seed); seed["owner_snapshots"][str(self.request)] = self.ref(self.request)
        cases.append((seed, "exact bound owners"))
        seed = copy.deepcopy(self.seed); seed["checks"][0]["target"]["files"].remove("README.md")
        cases.append((seed, "outside the frozen source obligations"))
        for seed, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(c.CompletionError, reason): c.prepare(seed)
        for both in (False, True):
            owners = c.prepare(self.seed)["owners"]
            if both: owners[0]["original"]["revision"] = self.git("rev-parse", "HEAD")
            else: owners[0]["original"].pop("snapshot")
            with self.subTest(both=both), self.assertRaisesRegex(c.CompletionError, "exactly one"):
                c.owner_refs(owners)

    def test_dirty_owner_snapshot_drift_refuses_cli_without_output_or_index_changes(self):
        self.dirty_owner_fixture(freeze=False)
        seed_path, output = self.root / "dirty-seed.json", self.root / "refused-scope.json"
        self.write(seed_path, self.seed)
        self.snapshot.write_text("Unreviewed replacement original.\n")
        index = (self.repo / ".git/index").read_bytes()
        done = subprocess.run([sys.executable, "-I", str(SCRIPT), "prepare", "--seed", str(seed_path),
                               "--output", str(output)], capture_output=True, timeout=35)
        self.assertEqual(done.returncode, 2, done.stdout)
        self.assertIn("source bytes changed", json.loads(done.stdout)["error"])
        self.assertFalse(output.exists())
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_dirty_owner_snapshot_is_rechecked_after_observations(self):
        self.dirty_owner_fixture(); self.update_owner_result(); self.snapshot_result()
        self.assertTrue(self.inspect()["complete"])
        original = c.observe
        def drift(check, expected):
            answer = original(check, expected)
            if check["kind"] == "git_source": self.snapshot.write_text("Changed during inspection.\n")
            return answer
        with mock.patch.object(c, "observe", side_effect=drift), self.assertRaisesRegex(c.CompletionError, "source bytes changed"):
            self.inspect()

    def preserved_source_fixture(self, attach=True, staged=False, index_only=False):
        self.owner = self.repo / "README.md"
        base = b"# Tool\nOwned: before\n"
        fragments = [(f"## Report {name}\nBaseline {name}\nEnd {name}\n".encode(),
                      f"## Report {name}\nOther task's pending {name}\nEnd {name}\n".encode()) for name in "ABC"]
        base += b"".join(old for old, _ in fragments)
        self.owner.write_bytes(base)
        self.git("add", "README.md"); self.git("commit", "-m", "test: establish shared owner")
        self.git("push", "origin", "main")
        original = base
        for old, new in fragments: original = original.replace(old, new)
        if staged or index_only:
            indexed = base.replace(*fragments[0]) if staged else base
            if index_only: indexed = indexed.replace(b"Owned: before", b"Unregistered index-only draft")
            self.owner.write_bytes(indexed); self.git("add", "README.md")
        self.owner.write_bytes(original)
        self.snapshot = self.root / "preserved-original.md"
        self.snapshot.write_bytes(original)
        self.seed["owners"] = [self.ref(self.owner)]
        self.seed["owner_snapshots"] = {str(self.owner): self.ref(self.snapshot)}
        self.seed["checks"][0]["target"]["files"].append("README.md")
        self.state_path = self.root / (self.seed["session_id"] + ".json")
        self.seed["state_path"] = str(self.state_path)
        self.reset_categories(); self.freeze()
        # Publish only this task's change, leaving the three other regions intact.
        self.owner.write_bytes(base.replace(b"Owned: before", b"Owned: published"))
        self.git("add", "README.md")
        self.owner.write_bytes(original.replace(b"Owned: before", b"Owned: published"))
        self.git("commit", "-m", "test: publish only the owned region"); self.git("push", "origin", "main")
        if staged:
            working = self.owner.read_bytes()
            self.owner.write_bytes(base.replace(b"Owned: before", b"Owned: published").replace(*fragments[0]))
            self.git("add", "README.md"); self.owner.write_bytes(working)
        self.state.update(outcome="waiting", status_basis=self.ref(self.request))
        self.write(self.state_path, self.state)
        self.snapshot_result()
        ranges = [{"base": [base.index(old), base.index(old) + len(old)],
                   "snapshot": [original.index(new), original.index(new) + len(new)]} for old, new in fragments]
        self.preservation_path = self.root / "source-preservation.json"
        self.preservation_review = self.root / "source-preservation-review.json"
        self.preservation = {"scope_sha256": self.scope_ref["sha256"], "task_id": self.seed["task_id"],
                             "session_id": self.seed["session_id"], "previous_state_sha256": self.ref(self.state_path)["sha256"],
                             "authorization": self.ref(self.request), "reason": "Preserve three independently owned report regions",
                             "files": [{"check_id": "source", "file": "README.md", "owner": "synthetic other task",
                                        "basis": [self.ref(self.request)], "ranges": ranges}]}
        self.write_preservation()
        if attach: self.attach_preservation()

    def write_preservation(self):
        self.write(self.preservation_path, self.preservation)
        self.write(self.preservation_review, {"scope_sha256": self.scope_ref["sha256"], "status": "No Findings",
                                             "source_preservation_sha256": self.ref(self.preservation_path)["sha256"]})

    def preservation_call(self, ok=True):
        done = subprocess.run([sys.executable, "-I", str(SCRIPT.with_name("completion_hook.py")),
                               "--bindings", str(self.root), "preserve-source", "--session", self.seed["session_id"],
                               "--expected-sha256", self.ref(self.state_path)["sha256"],
                               "--evidence", str(self.preservation_path), "--evidence-sha256", self.ref(self.preservation_path)["sha256"],
                               "--review", str(self.preservation_review), "--review-sha256", self.ref(self.preservation_review)["sha256"]],
                              capture_output=True, timeout=35)
        self.assertEqual(done.returncode, 0 if ok else 2, done.stdout + done.stderr)
        return json.loads(done.stdout)

    def attach_preservation(self):
        self.preservation_call()
        self.state = json.loads(self.state_path.read_bytes())
        self.state["outcome"] = "working"
        self.write(self.state_path, self.state)
        self.snapshot_result()

    def test_preserved_regions_actual_cli_corrects_without_rewriting_scope_and_completes(self):
        self.preserved_source_fixture(attach=False)
        scope = self.scope_path.read_bytes()
        before = json.loads(self.state_path.read_bytes())
        index = (self.repo / ".git/index").read_bytes()
        with self.assertRaisesRegex(c.CompletionError, "working source bytes differ"):
            c.observe(json.loads(scope)["checks"][0], self.result["checks"]["source"])
        self.preservation_call()
        after = json.loads(self.state_path.read_bytes())
        self.assertEqual({k: v for k, v in after.items() if k != "source_preservation"}, before)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(self.scope_path.read_bytes(), scope)
        self.state = after
        self.state["outcome"] = "working"; self.write(self.state_path, self.state)
        self.snapshot_result()
        self.assertTrue(self.inspect()["complete"])
        done = subprocess.run([sys.executable, "-I", str(SCRIPT.with_name("completion_hook.py")), "--bindings", str(self.root),
                               "complete", "--session", self.seed["session_id"], "--expected-sha256", self.ref(self.state_path)["sha256"],
                               "--task-id", self.seed["task_id"], "--request-digest", self.seed["request_digest"],
                               "--result", str(self.result_path), "--review", str(self.final_review_path),
                               "--review-sha256", self.ref(self.final_review_path)["sha256"]], capture_output=True, timeout=35)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(json.loads(done.stdout)["complete"])
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.scope_path.read_bytes(), scope)

    def test_preserved_regions_reject_loss_unregistered_changes_and_unpublished_owned_bytes(self):
        self.preserved_source_fixture()
        original = self.owner.read_bytes()
        variants = [original.replace(b"pending A", b"tampered A"),
                    original.replace(b"## Report B\nOther task's pending B\nEnd B\n", b""),
                    original.replace(b"Owned: published", b"Owned: not published"), original + b"Unregistered difference\n"]
        for payload in variants:
            with self.subTest(payload=payload):
                self.owner.write_bytes(payload)
                self.result["owner_updates"][str(self.owner)] = self.ref(self.owner)["sha256"]
                self.write(self.result_path, self.result); self.final_review()
                answer = self.inspect()
                self.assertFalse(answer["complete"])
                self.assertTrue(any("working source bytes differ" in f["reason"] for f in answer["failures"]), answer)
        self.owner.unlink()
        self.result["owner_updates"][str(self.owner)] = None
        self.result["checks"]["source"]["files"]["README.md"] = None
        self.write(self.result_path, self.result); self.final_review()
        self.assertTrue(any("cannot delete" in f["reason"] for f in self.inspect()["failures"]))

    def test_preserved_regions_cannot_omit_a_region_or_waive_baseline_index_checks(self):
        self.preserved_source_fixture(attach=False)
        self.preservation["files"][0]["ranges"].pop()
        self.write_preservation(); self.attach_preservation()
        self.assertFalse(self.inspect()["complete"])
        # Only the already reviewed README classification exists; an unrelated
        # index-only change still fails even with preservation bound.
        working = (self.repo / "tool.py").read_bytes()
        (self.repo / "tool.py").write_text("unreviewed staged bytes")
        self.git("add", "tool.py"); (self.repo / "tool.py").write_bytes(working)
        answer = self.inspect()
        self.assertTrue(any(f["id"].endswith(":content:tool.py") for f in answer["failures"]), answer)

    def test_preserved_regions_keep_same_file_staging_despite_a_content_exception(self):
        self.preserved_source_fixture(staged=True)
        self.assertTrue(self.inspect()["complete"])
        working = self.owner.read_bytes()
        published = c.git(str(self.repo), "show", "HEAD:README.md")
        self.owner.write_bytes(published); self.git("add", "README.md"); self.owner.write_bytes(working)
        answer = self.inspect()
        self.assertFalse(answer["complete"])
        self.assertTrue(any("retained staging" in f["reason"] for f in answer["failures"]), answer)
        # Newly staging the two originally unstaged foreign regions also fails.
        self.git("add", "README.md")
        self.assertTrue(any("retained staging" in f["reason"] for f in self.inspect()["failures"]))

    def test_preserved_regions_refuse_unexplained_starting_index_only_bytes(self):
        self.preserved_source_fixture(attach=False, index_only=True)
        before = self.state_path.read_bytes()
        answer = self.preservation_call(ok=False)
        self.assertIn("starting index", answer["error"])
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_source_preservation_cli_refuses_wrong_previous_scope_review_and_replacement(self):
        self.preserved_source_fixture(attach=False)
        before = self.state_path.read_bytes()
        original = copy.deepcopy(self.preservation)
        for key in ("scope_sha256", "previous_state_sha256"):
            self.preservation = copy.deepcopy(original); self.preservation[key] = "f" * 64
            self.write_preservation(); self.preservation_call(ok=False)
            self.assertEqual(self.state_path.read_bytes(), before)
        self.preservation = original; self.write_preservation()
        self.preservation_review.write_text("{}")
        self.preservation_call(ok=False); self.assertEqual(self.state_path.read_bytes(), before)
        self.write_preservation(); self.preservation_call()
        attached = self.state_path.read_bytes()
        self.preservation_call(ok=False); self.assertEqual(self.state_path.read_bytes(), attached)

    def test_preserved_regions_require_unique_ordered_original_context_and_final_review(self):
        self.preserved_source_fixture(attach=False)
        scope = json.loads(self.scope_path.read_bytes())
        original = copy.deepcopy(self.preservation)
        for mutate in (lambda r: r[0].update(base=[0, 1]), lambda r: r.append(copy.deepcopy(r[0]))):
            self.preservation = copy.deepcopy(original); mutate(self.preservation["files"][0]["ranges"])
            self.write_preservation()
            binding = {"evidence": self.ref(self.preservation_path), "review": self.ref(self.preservation_review)}
            with self.assertRaises(c.CompletionError): c.source_preservation(self.scope_ref, scope, binding)
        self.preservation = original; self.write_preservation(); self.attach_preservation()
        reviewed = json.loads(self.final_review_path.read_bytes()); reviewed.pop("source_preservation_sha256")
        self.write(self.final_review_path, reviewed); self.state["final_review"] = self.ref(self.final_review_path)
        self.write(self.state_path, self.state)
        with self.assertRaisesRegex(c.CompletionError, "final review"):
            self.inspect()

    def test_preserved_regions_recheck_evidence_and_snapshot_after_observations(self):
        self.preserved_source_fixture()
        observe = c.observe
        for target in (self.preservation_path, self.snapshot):
            raw = target.read_bytes()
            def mutate(check, expected, **kwargs):
                answer = observe(check, expected, **kwargs)
                if check["kind"] == "git_source": target.write_text("changed during observation")
                return answer
            try:
                with mock.patch.object(c, "observe", side_effect=mutate), self.assertRaises(c.CompletionError): self.inspect()
            finally: target.write_bytes(raw)

    def update_owner_result(self):
        self.owner.write_text("Synthetic reviewed updated local tool contract.\n")
        self.git("add", "README.md"); self.git("commit", "-m", "test: update owner contract")
        self.git("push", "origin", "main")
        revision = self.git("rev-parse", "HEAD")
        wanted = self.ref(self.owner)["sha256"]
        self.result["checks"]["source"].update(local_revision=revision, remote_revision=revision)
        self.result["checks"]["source"]["files"]["README.md"] = wanted
        self.result["owner_updates"] = {str(self.owner): wanted}
        self.write(self.result_path, self.result); self.final_review()

    def recovery_fixture(self, two_owners=False):
        self.owner = self.repo / "README.md"
        self.owner.write_text("Synthetic frozen owner before external drift.\n")
        other = self.repo / "other-owner.md"
        if two_owners:
            other.write_text("Second frozen owner.\n")
            self.git("add", "other-owner.md")
        self.git("add", "README.md"); self.git("commit", "-m", "test: frozen owner")
        self.git("push", "origin", "main")
        original = {"revision": self.git("rev-parse", "HEAD"), "file": "README.md", "sha256": self.ref(self.owner)["sha256"]}
        self.seed["owners"] = [self.ref(self.owner)] + ([self.ref(other)] if two_owners else [])
        other_original = {"revision": original["revision"], "file": "other-owner.md", "sha256": self.ref(other)["sha256"]} if two_owners else None
        self.reset_categories(); self.freeze()
        frozen_bytes = self.scope_path.read_bytes()
        self.owner.write_text("Synthetic externally changed and reviewed current owner.\n")
        if two_owners:
            other.write_text("Second externally changed and reviewed owner.\n")
            self.git("add", "other-owner.md")
        self.git("add", "README.md"); self.git("commit", "-m", "test: external owner drift")
        self.git("push", "origin", "main")
        revision = self.git("rev-parse", "HEAD")
        self.result["checks"]["source"].update(local_revision=revision, remote_revision=revision)
        self.write(self.result_path, self.result)
        source = copy.deepcopy(json.loads(frozen_bytes)["checks"][0]); source["target"]["files"].append("README.md")
        wanted = copy.deepcopy(self.result["checks"]["source"]); wanted["files"]["README.md"] = self.ref(self.owner)["sha256"]
        self.recovery_path = self.root / "recovery.json"; self.recovery_review_path = self.root / "recovery-review.json"
        self.recovery = {"scope_sha256": self.scope_ref["sha256"], "task_id": self.seed["task_id"], "session_id": self.seed["session_id"],
                         "authorization": self.ref(self.request), "owners": [{"owner_path": str(self.owner), "repository": str(self.repo), "identity": c.repo_identity(str(self.repo)),
                         "original": original, "current": {"revision": revision, "file": "README.md", "sha256": self.ref(self.owner)["sha256"]},
                         "source": {"definition": source, "expected": wanted}}]}
        if two_owners:
            other_source = copy.deepcopy(source); other_source["target"]["files"].append("other-owner.md")
            other_wanted = copy.deepcopy(wanted); other_wanted["files"]["other-owner.md"] = self.ref(other)["sha256"]
            self.recovery["owners"].append({"owner_path": str(other), "repository": str(self.repo), "identity": c.repo_identity(str(self.repo)),
                "original": other_original, "current": {"revision": revision, "file": "other-owner.md", "sha256": self.ref(other)["sha256"]},
                "source": {"definition": other_source, "expected": other_wanted}})
        self.write(self.recovery_path, self.recovery)
        self.write(self.recovery_review_path, {"scope_sha256": self.scope_ref["sha256"], "recovery_sha256": self.ref(self.recovery_path)["sha256"], "status": "No Findings"})
        self.recovery_binding = {"evidence": self.ref(self.recovery_path), "review": self.ref(self.recovery_review_path)}
        self.state["owner_recovery"] = self.recovery_binding
        self.write(self.state_path, self.state)
        self.write(self.final_review_path, {"scope_sha256": self.scope_ref["sha256"], "result_sha256": self.ref(self.result_path)["sha256"],
                                           "owner_recovery_sha256": self.ref(self.recovery_path)["sha256"], "status": "No Findings"})
        self.state["final_review"] = self.ref(self.final_review_path)
        self.write(self.state_path, self.state)
        return frozen_bytes

    def test_recovery_preserves_scope_and_does_not_relax_original_obligations(self):
        original = self.recovery_fixture()
        self.assertEqual(c.draft_result(self.scope_ref), self.result)
        self.assertTrue(self.inspect()["complete"]); self.assertEqual(self.scope_path.read_bytes(), original)
        self.installed.unlink(); self.installed.write_text("stale installed copy")
        self.assertFalse(self.inspect()["complete"])
        self.state["resources"] = [{"id": "pending", "kind": "reserved", "target": {}, "basis": self.ref(self.request)}]
        self.write(self.state_path, self.state)
        self.assertIn("resource:pending", [f["id"] for f in self.inspect()["failures"]])

    def test_chained_owner_recovery_retains_old_bytes_without_old_remote_freshness(self):
        frozen = self.recovery_fixture(two_owners=True)
        previous = copy.deepcopy(self.recovery_binding)
        old_bytes = self.recovery_path.read_bytes()
        self.owner.write_text("A second independently reviewed external owner change.\n")
        self.git("add", "README.md"); self.git("commit", "-m", "test: second owner drift")
        self.git("push", "origin", "main")
        revision = self.git("rev-parse", "HEAD")
        successor = copy.deepcopy(self.recovery)
        successor["previous"] = previous
        for owner in successor["owners"]:
            owner["current"].update(revision=revision, sha256=self.ref(Path(owner["owner_path"]))["sha256"])
            expected = owner["source"]["expected"]
            expected.update(local_revision=revision, remote_revision=revision)
            expected["files"]["README.md"] = self.ref(self.owner)["sha256"]
        next_path = self.root / "next-recovery.json"
        next_review = self.root / "next-recovery-review.json"
        self.write(next_path, successor)
        self.write(next_review, {"scope_sha256": self.scope_ref["sha256"], "recovery_sha256": self.ref(next_path)["sha256"], "status": "No Findings"})
        binding = {"evidence": self.ref(next_path), "review": self.ref(next_review)}
        recovered = c.owner_recovery(self.scope_ref, json.loads(frozen), binding)
        self.assertEqual(recovered[str(self.owner)], self.ref(self.owner)["sha256"])
        self.assertEqual(self.recovery_path.read_bytes(), old_bytes)
        successor["owners"] = successor["owners"][:1]
        self.write(next_path, successor)
        self.write(next_review, {"scope_sha256": self.scope_ref["sha256"], "recovery_sha256": self.ref(next_path)["sha256"], "status": "No Findings"})
        with self.assertRaisesRegex(c.CompletionError, "omitted earlier"):
            c.owner_recovery(self.scope_ref, json.loads(frozen), {"evidence": self.ref(next_path), "review": self.ref(next_review)})

    def test_recovery_covers_multiple_exact_owners_and_rejects_duplicate_or_omitted_drift(self):
        frozen = self.recovery_fixture(two_owners=True)
        self.assertTrue(self.inspect()["complete"])
        self.assertEqual(c.draft_result(self.scope_ref), self.result)
        self.assertEqual(self.scope_path.read_bytes(), frozen)
        for owners in ([self.recovery["owners"][0]] * 2, self.recovery["owners"][:1],
                       self.recovery["owners"] + [self.recovery["owners"][0]]):
            value = {**self.recovery, "owners": owners}; self.write(self.recovery_path, value)
            evidence_ref = self.ref(self.recovery_path)
            self.write(self.recovery_review_path, {"scope_sha256": self.scope_ref["sha256"], "recovery_sha256": evidence_ref["sha256"], "status": "No Findings"})
            binding = {"evidence": evidence_ref, "review": self.ref(self.recovery_review_path)}
            with self.assertRaises(c.CompletionError):
                recovery = c.owner_recovery(self.scope_ref, json.loads(frozen), binding)
                c.validate_scope(json.loads(frozen), recovery)
        self.write(self.recovery_path, self.recovery)
        self.write(self.recovery_review_path, {"scope_sha256": self.scope_ref["sha256"], "recovery_sha256": self.ref(self.recovery_path)["sha256"], "status": "No Findings"})
        (self.repo / "other-owner.md").write_text("new unreviewed drift")
        with self.assertRaises(c.CompletionError): self.inspect()

    def test_recovery_requires_exact_task_owner_identity_and_independent_review(self):
        self.recovery_fixture()
        for key, bad in (("scope_sha256", "1" * 64), ("owner_path", str(self.request)), ("task_id", "another"), ("identity", {})):
            with self.subTest(key=key):
                value = copy.deepcopy(self.recovery)
                (value["owners"][0] if key in ("owner_path", "identity") else value)[key] = bad
                self.write(self.recovery_path, value)
                binding = {**self.recovery_binding, "evidence": self.ref(self.recovery_path)}
                with self.assertRaises(c.CompletionError): c.owner_recovery(self.scope_ref, json.loads(self.scope_path.read_bytes()), binding)
        self.write(self.recovery_path, self.recovery)
        self.write(self.recovery_review_path, {"status": "No Findings"})
        with self.assertRaises(c.CompletionError): self.inspect()

    def test_recovery_cannot_be_retrofitted_as_planned_edit_or_reused_after_new_drift(self):
        self.recovery_fixture()
        self.result["owner_updates"] = {str(self.owner): self.ref(self.owner)["sha256"]}
        self.write(self.result_path, self.result)
        with self.assertRaisesRegex(c.CompletionError, "not declared"): self.inspect()
        del self.result["owner_updates"]; self.write(self.result_path, self.result)
        self.owner.write_text("second unreviewed drift")
        with self.assertRaises(c.CompletionError): self.inspect()

    def test_recovery_is_rechecked_after_observations(self):
        self.recovery_fixture(); original = c.observe
        def changed(check, wanted):
            result = original(check, wanted)
            if check["id"] == "installed": self.owner.write_text("concurrent change during inspect")
            return result
        with mock.patch.object(c, "observe", side_effect=changed):
            with self.assertRaises(c.CompletionError): self.inspect()

    def test_reviewed_planned_owner_edit_preserves_original_and_completes(self):
        self.owner_edit_fixture()
        original_scope = self.scope_path.read_bytes()
        self.update_owner_result()
        self.assertTrue(self.inspect()["complete"])
        self.assertEqual(self.scope_path.read_bytes(), original_scope)
        del self.result["owner_updates"]
        self.write(self.result_path, self.result); self.final_review()
        with self.assertRaisesRegex(c.CompletionError, "source bytes changed"):
            self.inspect()

    def test_unplanned_owner_update_and_unreviewed_expected_bytes_refuse(self):
        self.owner_edit_fixture(); self.update_owner_result()
        self.result["owner_updates"][str(self.request)] = self.ref(self.request)["sha256"]
        self.write(self.result_path, self.result); self.final_review()
        with self.assertRaisesRegex(c.CompletionError, "not declared"):
            self.inspect()
        del self.result["owner_updates"][str(self.request)]
        self.write(self.result_path, self.result)  # Deliberately omit a review for these result bytes.
        with self.assertRaisesRegex(c.CompletionError, "final review"):
            self.inspect()

    def test_owner_update_drift_during_inspection_is_rejected(self):
        self.owner_edit_fixture(); self.update_owner_result()
        original = c.observe
        def drift(check, expected):
            answer = original(check, expected)
            if check["id"] == "installed": self.owner.write_text("unreviewed concurrent policy")
            return answer
        with mock.patch.object(c, "observe", side_effect=drift):
            with self.assertRaisesRegex(c.CompletionError, "source bytes changed"):
                self.inspect()

    def test_planned_owner_edit_actual_cli_unbind_and_stopped_release(self):
        for command in ("unbind", "release"):
            with self.subTest(command=command):
                f = CompletionTests(); f.setUp(); self.addCleanup(f.doCleanups)
                f.state_path = f.root / (f.seed["session_id"] + ".json")
                f.seed["state_path"] = str(f.state_path)
                f.owner_edit_fixture(); f.update_owner_result()
                if command == "release":
                    f.state.update(outcome="blocked", status_basis=f.ref(f.owner))
                    before = json.loads(f.scope_path.read_bytes())["baseline"]["repositories"][str(f.repo)]["refs"]
                    after = c.inventory(str(f.repo))["refs"]
                    for ref in before.keys() | after.keys():
                        if before.get(ref) != after.get(ref):
                            key = "git:" + str(f.repo) + ":ref:" + ref
                            f.result["exceptions"][key] = {"owner":"synthetic owner", "reason":"Reviewed stopped-state owner edit", "scope":key,
                                                           "removal_condition":"Next authorized work", "basis":[f.ref(f.owner)]}
                    f.write(f.result_path, f.result)
                    reviewed = {"scope_sha256": f.scope_ref["sha256"], "result_sha256": f.ref(f.result_path)["sha256"],
                                "status":"No Findings", "disposition":"stopped"}
                    f.write(f.final_review_path, reviewed)
                    f.state["final_review"] = f.ref(f.final_review_path)
                    f.write(f.state_path, f.state)
                goal = ["--task-id", f.seed["task_id"], "--request-digest", f.seed["request_digest"]] if command == "unbind" else []
                done = subprocess.run([sys.executable, "-I", str(SCRIPT.with_name("completion_hook.py")), "--bindings", str(f.root), command,
                                       "--session", f.seed["session_id"], "--expected-sha256", f.ref(f.state_path)["sha256"], *goal],
                                      capture_output=True, timeout=35)
                self.assertEqual(done.returncode, 0, done.stdout)
                answer = json.loads(done.stdout)
                self.assertEqual(answer["complete"], command == "unbind")
                self.assertEqual(answer["stopped"], command == "release")
                self.assertFalse(f.state_path.exists())

    def test_git_transport_cannot_execute_and_remote_identity_is_frozen(self):
        marker = self.root / "executed"
        self.git("config", "protocol.ext.allow", "always")
        self.git("remote", "set-url", "origin", "ext::touch " + str(marker))
        self.assertFalse(self.inspect()["complete"])
        self.assertFalse(marker.exists())
        self.git("remote", "set-url", "origin", str(self.remote))
        clone = self.root / "clone.git"
        subprocess.run(["git", "clone", "--bare", str(self.remote), str(clone)], check=True, capture_output=True)
        self.git("remote", "set-url", "origin", str(clone))
        self.assertFalse(self.inspect()["complete"])
        with mock.patch.dict(c.os.environ, {"GIT_CONFIG_COUNT":"1", "GIT_CONFIG_KEY_0":"core.fsmonitor", "GIT_CONFIG_VALUE_0":"touch " + str(marker)}):
            c.inventory(str(self.repo))
        self.assertFalse(marker.exists())

    def test_final_source_observation_rejects_drift(self):
        original = c.observe
        changed = False
        def drift(check, expected):
            nonlocal changed
            answer = original(check, expected)
            if check.get("id") == "source" and not changed:
                changed = True
                (self.repo / "tool.py").write_text("new source during inspection")
                self.git("commit", "-am", "test: concurrent change")
                self.git("push", "origin", "main")
            return answer
        with mock.patch.object(c, "observe", side_effect=drift):
            answer = self.inspect()
        self.assertFalse(answer["complete"])
        self.assertTrue(any("final source" in f["reason"] for f in answer["failures"]), answer)

    def test_fifo_input_refused_without_waiting(self):
        fifo = self.root / "fifo"
        c.os.mkfifo(fifo)
        with self.assertRaises(c.CompletionError): c.raw_file(str(fifo))
        artifact_fifo = self.artifacts / "fifo"; c.os.mkfifo(artifact_fifo)
        with self.assertRaisesRegex(c.CompletionError, "unsupported filesystem"): c.tree(str(self.artifacts))

    def test_empty_directory_and_single_buffer_reference_are_checked(self):
        empty = self.artifacts / "empty"; empty.mkdir()
        self.assertFalse(self.inspect()["complete"])
        self.freeze(); empty.rmdir()
        self.assertFalse(self.inspect()["complete"])
        original = c.raw_file
        calls = []
        def read(value, *args, **kwargs):
            calls.append(value); return original(value, *args, **kwargs)
        with mock.patch.object(c, "raw_file", side_effect=read):
            c.read_reference(self.scope_ref)
        self.assertEqual(calls, [str(self.scope_path)])

    def test_duplicate_keys_and_unrecognized_observer_never_execute_input(self):
        with self.assertRaises(c.CompletionError): c.decode(b'{"scope":1,"scope":2}')
        seed = copy.deepcopy(self.seed)
        seed["checks"][0]["kind"] = "shell"
        seed["checks"][0]["target"] = {"command": "touch arbitrary-file"}
        with self.assertRaises(c.CompletionError): c.prepare(seed)

    def test_frozen_improvement_cannot_be_omitted_or_replaced_by_a_promise(self):
        fixture_spec = importlib.util.spec_from_file_location("improvement_fixture", Path(__file__).with_name("test_improvement.py"))
        fixture_module = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(fixture_module)
        fixture = fixture_module.ImprovementTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.plan["task_id"] = self.seed["task_id"]; fixture.freeze()
        self.seed["checks"].append({"id": "prevention", "category": "quality", "kind": "improvement",
                                    "target": {"path": fixture.record_ref["path"], "plan": fixture.plan_ref}})
        self.reset_categories()
        for invalid in ([], {}):
            fixture.plan["cases"][0]["phase"] = invalid; fixture.freeze()
            self.seed["checks"][-1]["target"]["plan"] = fixture.plan_ref
            seed_path = self.root / "invalid-phase-seed.json"; self.write(seed_path, self.seed)
            output = self.root / "invalid-phase-scope.json"
            result = subprocess.run([sys.executable, "-B", str(SCRIPT), "prepare", "--seed", str(seed_path),
                                     "--output", str(output)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(json.loads(result.stdout)["complete"])
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(output.exists())
        fixture.plan["cases"][0]["phase"] = "reproduction"; fixture.freeze()
        self.seed["checks"][-1]["target"]["plan"] = fixture.plan_ref
        self.freeze()
        with self.assertRaisesRegex(ValueError, "missing|coverage|obligation"):
            self.inspect()
        self.result["checks"]["prevention"] = {"sha256": fixture.record_ref["sha256"]}
        self.write(self.result_path, self.result); self.final_review()
        self.assertTrue(self.inspect()["complete"])
        fixture.record = {"promise": "be careful next time"}; fixture.freeze()
        self.result["checks"]["prevention"] = {"sha256": fixture.record_ref["sha256"]}
        self.write(self.result_path, self.result); self.final_review()
        self.assertFalse(self.inspect()["complete"])


if __name__ == "__main__":
    unittest.main()
