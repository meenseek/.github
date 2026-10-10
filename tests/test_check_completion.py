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
        self.write(self.final_review_path, {"scope_sha256": self.scope_ref["sha256"], "status": "No Findings",
                                           "result_sha256": self.ref(self.result_path)["sha256"]})
        self.state["final_review"] = self.ref(self.final_review_path)
        self.write(self.state_path, self.state)

    def inspect(self):
        return c.check_scope(self.scope_ref, str(self.result_path), task_id=self.seed["task_id"], request_digest="0" * 64)

    def test_real_observations_are_read_only_and_complete(self):
        before = {p: p.read_bytes() for p in (self.scope_path, self.state_path, self.result_path, self.repo / ".git/index")}
        answer = self.inspect()
        self.assertTrue(answer["complete"], answer)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertIn("owner judgments", answer["assurance"])

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
                done = subprocess.run([sys.executable, "-I", str(SCRIPT.with_name("completion_hook.py")), "--bindings", str(f.root), command,
                                       "--session", f.seed["session_id"], "--expected-sha256", f.ref(f.state_path)["sha256"]],
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


if __name__ == "__main__":
    unittest.main()
