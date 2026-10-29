import importlib.util
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import test_check_completion as fixture_module

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/completion_hook.py"
spec = importlib.util.spec_from_file_location("hook", SCRIPT)
h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)


class HookTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.CompletionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture
        self.root = self.f.root / "bindings"
        self.state = h.pointer(self.root, self.f.seed["session_id"])
        self.payload = {"session_id": self.f.seed["session_id"], "cwd": str(Path.cwd()), "hook_event_name": "Stop", "stop_hook_active": False}

    def call(self, command, *args, ok=True):
        argv = [sys.executable, "-I", str(SCRIPT), "--bindings", str(self.root), command, "--session", self.f.seed["session_id"]]
        if command != "arm": argv += ["--expected-sha256", h.c.sha(h.c.raw_file(str(self.state)))]
        if command in {"complete", "unbind"}: argv += self.goal_args()
        done = subprocess.run([*argv, *args], capture_output=True, timeout=35)
        self.assertEqual(done.returncode, 0 if ok else 2, done.stdout)
        return json.loads(done.stdout)

    def arm_bind(self, finalize=True):
        self.call("arm", "--task-id", self.f.seed["task_id"])
        self.f.seed["state_path"] = str(self.state)
        self.f.freeze()  # reviewed synthetic scope/result stored outside pointer directory
        self.call("bind", "--scope", str(self.f.scope_path), "--scope-sha256", self.f.scope_ref["sha256"],
                  "--review", str(self.f.review_path), "--review-sha256", self.f.ref(self.f.review_path)["sha256"])
        if finalize:
            self.call("finalize", "--result", str(self.f.result_path), "--review", str(self.f.final_review_path),
                      "--review-sha256", self.f.ref(self.f.final_review_path)["sha256"])

    def completion_args(self):
        return ["--result", str(self.f.result_path), "--review", str(self.f.final_review_path),
                "--review-sha256", self.f.ref(self.f.final_review_path)["sha256"]]

    def goal_args(self):
        return ["--task-id", self.f.seed["task_id"], "--request-digest", self.f.seed["request_digest"]]

    def record_fixture(self, nested=False):
        self.records = self.f.root / "task-records"
        self.records.mkdir()
        if nested:
            (self.records / "nested").mkdir()
            (self.records / "nested" / "scratch.txt").write_text("task-only scratch")
        self.f.request = self.records / "request.txt"
        self.f.request.write_text("Synthetic request to finish and remove task-only records.")
        self.f.seed.update(request=self.f.ref(self.f.request), record_root=str(self.records))
        for attribute, name in (("scope_path", "scope.json"), ("review_path", "scope-review.json"),
                                ("result_path", "result.json"), ("final_review_path", "final-review.json")):
            setattr(self.f, attribute, self.records / name)
        original = self.f.final_review
        def final_review():
            original()
            value = json.loads(self.f.final_review_path.read_bytes())
            value["record_inventory_sha256"] = h.c.reviewed_record_inventory(
                h.c.record_directory(str(self.records)), str(self.f.final_review_path))
            self.f.write(self.f.final_review_path, value)
            self.f.state["final_review"] = self.f.ref(self.f.final_review_path)
            self.f.write(self.f.state_path, self.f.state)
        self.f.final_review = final_review

    def test_complete_actual_cli_removes_reviewed_task_records_and_binding(self):
        self.record_fixture()
        self.arm_bind(finalize=False)
        answer = self.call("complete", *self.completion_args())
        self.assertTrue(answer["complete"])
        self.assertEqual(answer["records_removed"], str(self.records))
        self.assertFalse(self.records.exists()); self.assertFalse(self.state.exists())
        self.assertTrue(self.f.owner.exists()); self.assertTrue(self.f.repo.exists())

    def test_unbind_actual_cli_removes_nested_task_records(self):
        self.record_fixture(nested=True)
        self.arm_bind()
        answer = self.call("unbind")
        self.assertTrue(answer["complete"])
        self.assertFalse(self.records.exists()); self.assertFalse(self.state.exists())

    def test_failed_completion_preserves_task_records_for_retry(self):
        self.record_fixture()
        self.arm_bind(finalize=False)
        self.call("reserve", "--id", "unresolved", *self.basis())
        before = h.c.record_inventory(h.c.record_directory(str(self.records)))
        answer = self.call("complete", *self.completion_args(), ok=False)
        self.assertFalse(answer["complete"])
        self.assertEqual(h.c.record_inventory(h.c.record_directory(str(self.records))), before)
        self.assertTrue(self.state.exists())

    def test_final_review_rejects_new_record_before_any_deletion(self):
        self.record_fixture()
        self.arm_bind(finalize=False)
        (self.records / "unreviewed.txt").write_text("do not delete")
        self.call("complete", *self.completion_args(), ok=False)
        self.assertTrue(self.f.scope_path.exists())
        self.assertEqual((self.records / "unreviewed.txt").read_text(), "do not delete")
        self.assertNotIn("cleanup", json.loads(self.state.read_bytes()))

    def test_file_added_after_checked_snapshot_is_not_adopted_for_deletion(self):
        self.record_fixture()
        self.arm_bind(finalize=False)
        args = self.completion_args()
        argv = ["--bindings", str(self.root), "complete", "--session", self.f.seed["session_id"],
                "--expected-sha256", h.c.sha(self.state.read_bytes()), *self.goal_args(), *args]
        checked = False
        original_check, original_inventory = h.c.check_scope, h.c.record_inventory
        extra = self.records / "late-unreviewed.txt"
        def inspect(*args, **kwargs):
            nonlocal checked
            answer = original_check(*args, **kwargs); checked = True
            return answer
        def snapshot(record):
            inventory = original_inventory(record)
            if checked and not extra.exists(): extra.write_text("preserve late addition")
            return inventory
        with mock.patch.object(h.c, "check_scope", side_effect=inspect), mock.patch.object(h.c, "record_inventory", side_effect=snapshot), redirect_stdout(io.StringIO()):
            self.assertEqual(h.main(argv), 2)
        self.assertTrue(extra.exists()); self.assertTrue(self.f.scope_path.exists())
        extra.unlink()
        self.assertTrue(self.call("complete", *args)["binding_released"])
        self.assertFalse(self.records.exists())

    def test_partial_cleanup_retry_checks_goal_and_keeps_stop_read_only(self):
        self.record_fixture(nested=True)
        self.arm_bind(finalize=False)
        args = self.completion_args()
        argv = ["--bindings", str(self.root), "complete", "--session", self.f.seed["session_id"],
                "--expected-sha256", h.c.sha(self.state.read_bytes()), *self.goal_args(), *args]
        original = h.os.unlink
        def interrupted(name, *positional, **keywords):
            if name == "request.txt":
                raise PermissionError("synthetic interrupted cleanup")
            return original(name, *positional, **keywords)
        with mock.patch.object(h.os, "unlink", side_effect=interrupted), redirect_stdout(io.StringIO()):
            self.assertEqual(h.main(argv), 2)
        self.assertFalse(self.f.scope_path.exists())  # ordinary scope-based retry is no longer possible
        self.assertTrue(self.state.exists()); self.assertTrue(self.records.exists())
        before = self.state.read_bytes()
        self.call("complete", *args, "--task-id", "wrong-current-goal", ok=False)
        self.assertEqual(self.state.read_bytes(), before)
        with mock.patch.object(h.c, "check_scope", side_effect=AssertionError("pending cleanup must not inspect deleted inputs")):
            self.assertEqual(h.hook(self.payload, self.root)["decision"], "block")
        self.assertEqual(self.state.read_bytes(), before)
        extra = self.records / "unreviewed.txt"; extra.write_text("preserve new bytes")
        self.call("complete", *args, ok=False)
        self.assertTrue(extra.exists()); self.assertEqual(self.state.read_bytes(), before)
        extra.unlink()
        self.assertTrue(self.call("complete", *args)["binding_released"])
        self.assertFalse(self.records.exists()); self.assertFalse(self.state.exists())

    def test_record_cleanup_refuses_links_and_replaced_root(self):
        import os
        self.record_fixture()
        self.arm_bind(finalize=False)
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                link = self.records / "unsafe"
                if kind == "symlink": link.symlink_to(self.f.owner)
                else: os.link(self.f.owner, link)
                self.call("complete", *self.completion_args(), ok=False)
                self.assertTrue(self.f.owner.exists()); self.assertTrue(self.f.scope_path.exists())
                link.unlink()
        moved = self.records.with_name("moved-records")
        args = self.completion_args()
        self.records.rename(moved); self.records.mkdir()
        self.call("complete", *args, ok=False)
        self.assertTrue((moved / "scope.json").exists()); self.assertTrue(self.records.exists())

    def test_record_root_cannot_overlap_product_source_or_retained_artifacts(self):
        for root in (self.f.repo, self.f.artifacts, self.f.root):
            with self.subTest(root=root):
                seed = {**self.f.seed, "record_root": str(root)}
                with self.assertRaises(h.c.CompletionError): h.c.prepare(seed)

    def test_record_root_cannot_hold_a_symlinked_installed_output(self):
        self.record_fixture()
        output = self.records / "installed-output.py"; output.write_text("keep installed output")
        self.f.installed.unlink(); self.f.installed.symlink_to(output)
        with self.assertRaises(h.c.CompletionError): h.c.prepare(self.f.seed)
        self.assertEqual(output.read_text(), "keep installed output")

    def test_record_root_protects_source_omitted_from_repository_inventory(self):
        request = self.f.repo / "cleanup-request.txt"; request.write_text("keep this source checkout")
        self.f.seed.update(request=self.f.ref(request), record_root=str(self.f.repo), repositories=[])
        self.f.seed["checks"] = [self.f.seed["checks"][0]]  # no installed-file alias to mask this source path
        self.f.reset_categories()
        with self.assertRaisesRegex(h.c.CompletionError, "overlaps"):
            h.c.prepare(self.f.seed)
        self.assertTrue((self.f.repo / "tool.py").exists())

    def test_complete_actual_cli_finalizes_inspects_and_releases_once(self):
        self.arm_bind(finalize=False)
        answer = self.call("complete", *self.completion_args())
        self.assertTrue(answer["complete"]); self.assertTrue(answer["binding_released"])
        self.assertFalse(self.state.exists())
        self.assertTrue(self.f.scope_path.exists()); self.assertTrue(self.f.result_path.exists())
        self.assertEqual(answer["final_review"], self.f.ref(self.f.final_review_path))

    def test_complete_requires_current_goal_before_mutating_old_binding(self):
        # Reproduction: a valid old task must not close a later request merely
        # because the caller reused this session and its old result/review.
        self.arm_bind(finalize=False)
        before = self.state.read_bytes()
        done = subprocess.run([sys.executable, "-I", str(SCRIPT), "--bindings", str(self.root),
                               "complete", "--session", self.f.seed["session_id"],
                               "--expected-sha256", h.c.sha(before), *self.completion_args()],
                              capture_output=True, timeout=10)
        self.assertEqual(done.returncode, 2, done.stdout)
        self.assertEqual(self.state.read_bytes(), before)

    def test_new_input_carries_frozen_goal_and_scoped_wait_without_mutation(self):
        self.arm_bind()
        unrelated = self.f.root / "unrelated-approval.txt"
        unrelated.write_text("An older, unrelated question is waiting for approval.")
        self.call("outcome", "--status", "waiting", "--basis", str(unrelated),
                  "--basis-sha256", self.f.ref(unrelated)["sha256"])
        before = self.state.read_bytes()
        with mock.patch.object(h.c, "check_scope", side_effect=AssertionError("input must not run completion observations")):
            answer = h.hook({**self.payload, "hook_event_name": "UserPromptSubmit"}, self.root)
        context = answer["hookSpecificOutput"]["additionalContext"]
        self.assertIn(self.f.seed["task_id"], context)
        self.assertIn(self.f.scope_ref["sha256"], context)
        self.assertIn(self.f.seed["request"]["path"], context)
        self.assertIn("waiting", context)
        self.assertNotIn("decision", answer)
        self.assertEqual(self.state.read_bytes(), before)

    def test_wrong_deliverable_and_missing_deliverable_cannot_close_goal(self):
        artifact = self.f.artifacts / "deliverable.txt"
        expected = b"Reviewed UI deliverable."
        artifact.write_bytes(expected)
        self.f.seed["checks"].append({"id": "deliverable", "category": "artifacts", "kind": "file",
                                      "target": {"path": str(artifact)}})
        self.f.reset_categories()
        self.arm_bind(finalize=False)
        self.f.result["checks"]["deliverable"] = self.f.ref(artifact)
        self.f.result["checks"]["deliverable"].pop("path")
        self.f.write(self.f.result_path, self.f.result)
        self.f.final_review()
        self.call("finalize", *self.completion_args())
        artifact.write_text("Answer to an unrelated old policy question.")
        self.assertFalse(self.call("complete", *self.completion_args(), ok=False)["complete"])
        artifact.unlink()
        self.assertFalse(self.call("complete", *self.completion_args(), ok=False)["complete"])
        self.assertTrue(self.state.exists())
        artifact.write_bytes(expected)
        self.assertTrue(self.call("complete", *self.completion_args())["complete"])

    def test_current_goal_mismatch_preserves_reviewed_result_and_binding(self):
        self.arm_bind()
        before = self.state.read_bytes()
        # Evaluation cases differ from the missing-goal reproduction above.
        for command in ("complete", "unbind"):
            for task, digest in (("different-current-task", self.f.seed["request_digest"]),
                                 (self.f.seed["task_id"], "1" * 64)):
                with self.subTest(command=command, task=task, digest=digest):
                    argv = ["--bindings", str(self.root), command, "--session", self.f.seed["session_id"],
                            "--expected-sha256", h.c.sha(before), "--task-id", task, "--request-digest", digest]
                    if command == "complete": argv += self.completion_args()
                    with redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(h.main(argv), 2)
                    self.assertIn("current task/request differs", output.getvalue())
                    self.assertEqual(self.state.read_bytes(), before)

    def test_new_input_preserves_real_cancellation_without_global_block(self):
        self.arm_bind()
        self.call("outcome", "--status", "cancelled", *self.basis())
        before = self.state.read_bytes()
        answer = h.hook({**self.payload, "hook_event_name": "UserPromptSubmit"}, self.root)
        self.assertIn("cancelled", answer["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("decision", answer)
        self.assertEqual(self.state.read_bytes(), before)
        self.call("complete", *self.completion_args(), ok=False)
        self.assertTrue(self.state.exists())

    def test_resumed_work_does_not_revalidate_an_old_wait_basis(self):
        self.arm_bind()
        old_basis = self.f.root / "old-wait.txt"
        old_basis.write_text("An unrelated approval was previously pending.")
        basis = ["--basis", str(old_basis), "--basis-sha256", self.f.ref(old_basis)["sha256"]]
        self.call("outcome", "--status", "waiting", *basis)
        self.call("outcome", "--status", "working", *basis)
        old_basis.unlink()
        answer = h.hook({**self.payload, "hook_event_name": "UserPromptSubmit"}, self.root)
        self.assertIn(self.f.scope_ref["sha256"], answer["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("기존 목표 연결 미확인", answer["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(self.call("complete", *self.completion_args())["complete"])

    def test_complete_actual_cli_releases_reviewed_planned_owner_deletion(self):
        self.f.owner_edit_fixture()
        self.arm_bind(finalize=False)
        original = self.f.scope_path.read_bytes()
        self.f.delete_planned_owner()
        answer = self.call("complete", *self.completion_args())
        self.assertTrue(answer["complete"])
        self.assertTrue(answer["binding_released"])
        self.assertFalse(self.state.exists())
        self.assertEqual(self.f.scope_path.read_bytes(), original)
        self.assertIsNone(self.f.result["owner_updates"][str(self.f.owner)])

    def test_complete_failure_preserves_reviewed_inputs_for_recovery_and_retry(self):
        self.arm_bind(finalize=False)
        self.call("reserve", "--id", "unresolved", *self.basis())
        answer = self.call("complete", *self.completion_args(), ok=False)
        self.assertFalse(answer["binding_released"])
        state = json.loads(self.state.read_bytes())
        self.assertEqual(state["result_path"], str(self.f.result_path))
        self.assertEqual(state["final_review"], self.f.ref(self.f.final_review_path))
        resource = self.f.root / "resource.json"
        resource.write_text(json.dumps({"kind":"absent", "target":{"path":str(self.f.root / "never-created")}}))
        self.call("attach", "--id", "unresolved", "--resource", str(resource), *self.basis())
        self.assertTrue(self.call("complete", *self.completion_args())["binding_released"])

    def test_stash_resource_waiting_resume_and_complete_actual_cli(self):
        self.f.seed["checks"].append({"id": "stash", "category": "temporary_resources", "kind": "ref_absent",
                                      "target": {"repository": str(self.f.repo), "ref": "refs/stash"}})
        self.f.reset_categories()
        self.arm_bind(finalize=False)
        self.f.result["checks"]["stash"] = {}
        self.f.write(self.f.result_path, self.f.result)
        self.f.final_review()
        self.call("reserve", "--id", "stash", *self.basis())
        scratch = self.f.repo / "scratch.txt"; scratch.write_text("preserved local work")
        self.f.git("stash", "push", "-u", "-m", "synthetic temporary stash")
        resource = self.f.root / "stash-resource.json"
        resource.write_text(json.dumps({"kind": "ref_absent", "target": {"repository": str(self.f.repo), "ref": "refs/stash"}}))
        self.call("attach", "--id", "stash", "--resource", str(resource), *self.basis())
        answer = self.call("complete", *self.completion_args(), ok=False)
        self.assertTrue(any(failure["reason"] == "owned stash remains" for failure in answer["failures"]))
        self.call("outcome", "--status", "waiting", *self.basis())
        self.f.git("stash", "pop")
        self.assertEqual(scratch.read_text(), "preserved local work"); scratch.unlink()
        self.call("outcome", "--status", "working", *self.basis())
        self.assertTrue(self.call("complete", *self.completion_args())["binding_released"])
        self.assertFalse(self.state.exists())

    def test_attach_rejects_unsupported_ref_before_pointer_mutation(self):
        self.arm_bind()
        self.call("reserve", "--id", "invalid", *self.basis())
        before = self.state.read_bytes()
        resource = self.f.root / "invalid-resource.json"
        resource.write_text(json.dumps({"kind": "ref_absent", "target": {"repository": str(self.f.repo), "ref": "refs/tags/example"}}))
        self.call("attach", "--id", "invalid", "--resource", str(resource), *self.basis(), ok=False)
        self.assertEqual(self.state.read_bytes(), before)

    def test_complete_refuses_changed_review_and_stale_pointer(self):
        self.arm_bind(finalize=False)
        argv = ["--bindings", str(self.root), "complete", "--session", self.f.seed["session_id"],
                "--expected-sha256", "1"*64, *self.goal_args(), *self.completion_args()]
        before = self.state.read_bytes()
        with redirect_stdout(io.StringIO()): self.assertEqual(h.main(argv), 2)
        self.assertEqual(self.state.read_bytes(), before)
        self.f.final_review_path.write_text("{}")
        self.call("complete", *self.completion_args(), ok=False)
        self.assertTrue(self.state.exists())

    def test_complete_reuses_one_inspection_and_retains_mid_inspection_mutation(self):
        self.arm_bind(finalize=False)
        argv = ["--bindings", str(self.root), "complete", "--session", self.f.seed["session_id"],
                "--expected-sha256", h.c.sha(self.state.read_bytes()), *self.goal_args(), *self.completion_args()]
        original = h.c.observe
        def drift(check, expected):
            answer = original(check, expected)
            if check["id"] == "installed":
                state = json.loads(self.state.read_bytes())
                state["resources"].append({"id":"late", "kind":"reserved", "target":{}, "basis":self.f.ref(self.f.owner)})
                self.state.write_bytes(h.c.canonical(state) + b"\n")
            return answer
        with mock.patch.object(h.c, "check_scope", wraps=h.c.check_scope) as inspect:
            with mock.patch.object(h.c, "observe", side_effect=drift), redirect_stdout(io.StringIO()):
                self.assertEqual(h.main(argv), 2)
            self.assertEqual(inspect.call_count, 1)
        self.assertTrue(self.state.exists())
        self.assertEqual(json.loads(self.state.read_bytes())["resources"][0]["id"], "late")

    def basis(self):
        return ["--basis", str(self.f.owner), "--basis-sha256", self.f.ref(self.f.owner)["sha256"]]

    def test_installed_cli_does_not_create_local_bytecode(self):
        runtime = self.f.root / "runtime"; runtime.mkdir()
        import shutil
        for name in ("completion_hook.py", "check_completion.py"):
            shutil.copyfile(SCRIPT.with_name(name), runtime / name)
        done = subprocess.run([sys.executable, "-I", str(runtime / "completion_hook.py"), "--bindings", str(self.root), "hook"],
                              input=json.dumps(self.payload).encode(), capture_output=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertFalse((runtime / "__pycache__").exists())

    def test_unbound_guidance_and_context_have_no_inspection(self):
        with mock.patch.object(h.c, "check_scope", side_effect=AssertionError("unbound must not inspect")):
            self.assertNotIn("decision", h.hook(self.payload, self.root))
            for event in ("SessionStart", "UserPromptSubmit"):
                answer = h.hook({**self.payload, "hook_event_name": event}, self.root)
                self.assertIn(self.payload["session_id"], answer["hookSpecificOutput"]["additionalContext"])

    def test_fresh_cli_covers_external_worktree_of_desktop_repository(self):
        import shutil
        runtime = self.f.root / ".github" / "scripts"
        runtime.mkdir(parents=True)
        for name in ("completion_hook.py", "check_completion.py"):
            shutil.copyfile(SCRIPT.with_name(name), runtime / name)
        with tempfile.TemporaryDirectory() as temporary:
            linked = Path(temporary).resolve() / "linked"
            self.f.git("worktree", "add", "--detach", str(linked), "HEAD")
            before = (self.f.git("status", "--porcelain"), self.f.git("worktree", "list", "--porcelain"))
            try:
                for event in ("SessionStart", "UserPromptSubmit", "Stop"):
                    with self.subTest(event=event):
                        payload = {**self.payload, "cwd": str(linked), "hook_event_name": event}
                        done = subprocess.run([sys.executable, "-I", str(runtime / "completion_hook.py"),
                                               "--bindings", str(self.root), "hook"],
                                              input=json.dumps(payload).encode(), capture_output=True, timeout=10)
                        self.assertEqual(done.returncode, 0, done.stdout)
                        answer = json.loads(done.stdout)
                        if event == "Stop":
                            self.assertIn("systemMessage", answer)
                        else:
                            self.assertIn(self.payload["session_id"], answer["hookSpecificOutput"]["additionalContext"])
                        self.assertNotIn("decision", answer)
                self.assertEqual((self.f.git("status", "--porcelain"), self.f.git("worktree", "list", "--porcelain")), before)
                self.assertFalse(self.root.exists())
                self.assertFalse((runtime / "__pycache__").exists())
            finally:
                self.f.git("worktree", "remove", str(linked))

    def test_unbound_foreign_repository_and_unavailable_git_are_not_adopted(self):
        payload = {**self.payload, "cwd": str(self.f.repo)}
        for event in ("SessionStart", "UserPromptSubmit", "Stop"):
            self.assertEqual(h.hook({**payload, "hook_event_name": event}, self.root), {})
        with mock.patch.object(h.c, "git", side_effect=h.c.CompletionError("synthetic unavailable Git")):
            self.assertEqual(h.hook(payload, self.root), {})
        self.assertFalse(self.root.exists())

    def test_armed_missing_scope_continues_once_then_reports_unfinished(self):
        self.call("arm", "--task-id", self.f.seed["task_id"])
        self.assertEqual(h.hook(self.payload, self.root)["decision"], "block")
        self.assertNotIn("decision", h.hook({**self.payload, "stop_hook_active":True}, self.root))
        self.call("unbind", ok=False)

    def test_actual_cli_reserves_and_attaches_owned_path_until_absent(self):
        self.arm_bind()
        self.call("reserve", "--id", "temporary", *self.basis())
        self.assertEqual(h.hook(self.payload, self.root)["decision"], "block")
        temporary = self.f.root / "owned-temporary.txt"; temporary.write_text("helper")
        resource = self.f.root / "resource.json"
        resource.write_text(json.dumps({"kind":"absent", "target":{"path":str(temporary)}}))
        self.call("attach", "--id", "temporary", "--resource", str(resource), *self.basis())
        self.call("inspect", ok=False)
        temporary.unlink()
        answer = self.call("inspect")
        self.assertTrue(answer["complete"])
        self.assertNotIn("decision", h.hook(self.payload, self.root))
        self.call("unbind")
        self.assertFalse(self.state.exists())

    def test_waiting_never_means_completed_and_scope_cannot_be_replaced(self):
        self.arm_bind()
        self.call("outcome", "--status", "waiting", *self.basis())
        answer = h.hook(self.payload, self.root)
        self.assertIn("미완료", answer["systemMessage"])
        self.call("inspect", ok=False)
        self.call("bind", "--scope", str(self.f.scope_path), "--scope-sha256", self.f.scope_ref["sha256"],
                  "--review", str(self.f.review_path), "--review-sha256", self.f.ref(self.f.review_path)["sha256"], ok=False)
        self.call("outcome", "--status", "working", *self.basis())
        self.assertTrue(self.call("inspect")["complete"])

    def test_terminal_release_preserves_unfinished_goal_and_allows_session_reuse(self):
        self.arm_bind()
        self.call("outcome", "--status", "cancelled", *self.basis())
        self.call("release", ok=False)  # completed review cannot authorize stopped release
        reviewed = json.loads(self.f.final_review_path.read_text()); reviewed["disposition"] = "stopped"
        self.f.final_review_path.write_text(json.dumps(reviewed))
        self.call("finalize", "--result", str(self.f.result_path), "--review", str(self.f.final_review_path),
                  "--review-sha256", self.f.ref(self.f.final_review_path)["sha256"])
        answer = self.call("release")
        self.assertFalse(answer["complete"]); self.assertTrue(answer["stopped"])
        self.assertTrue(self.f.scope_path.exists()); self.assertTrue(self.f.result_path.exists())
        self.call("arm", "--task-id", "new-synthetic-task")

    def test_exact_owner_recovery_actual_cli_preserves_binding_and_unbinds(self):
        self.f.seed["state_path"] = str(self.state)
        frozen = self.f.recovery_fixture(two_owners=True)
        self.root.mkdir(mode=0o700)
        waiting = dict(self.f.state); waiting.pop("owner_recovery")
        waiting.update(outcome="waiting", status_basis=self.f.ref(self.f.request))
        self.f.write(self.state, waiting)
        args = ["--evidence", str(self.f.recovery_path), "--evidence-sha256", self.f.ref(self.f.recovery_path)["sha256"],
                "--review", str(self.f.recovery_review_path), "--review-sha256", self.f.ref(self.f.recovery_review_path)["sha256"]]
        before = self.state.read_bytes()
        bad = subprocess.run([sys.executable, "-I", str(SCRIPT), "--bindings", str(self.root), "recover-owner",
                              "--session", self.f.seed["session_id"], "--expected-sha256", "0" * 64, *args], capture_output=True)
        self.assertEqual(bad.returncode, 2); self.assertEqual(self.state.read_bytes(), before)
        self.call("recover-owner", *args)
        recovered = json.loads(self.state.read_bytes())
        self.assertEqual(recovered["outcome"], "waiting"); self.assertEqual(recovered["scope"], waiting["scope"])
        self.assertEqual(recovered["resources"], waiting["resources"]); self.assertEqual(recovered["status_basis"], waiting["status_basis"])
        self.call("recover-owner", *args, ok=False)
        self.call("outcome", "--status", "working", "--basis", str(self.f.request), "--basis-sha256", self.f.ref(self.f.request)["sha256"])
        self.assertTrue(self.call("inspect")["complete"])
        self.assertTrue(self.call("unbind")["complete"]); self.assertFalse(self.state.exists())
        self.assertEqual(self.f.scope_path.read_bytes(), frozen)

    def test_configuration_preserves_all_handlers_and_is_idempotent(self):
        original = {"description":"Existing owner", "hooks":{"Stop":[{"hooks":[{"type":"command", "command":"owner-command", "timeout":5}]}],
                                                        "Interrupt":[{"hooks":[{"type":"command", "command":"interrupt-owner"}]}]}}
        answer = h.configuration(original)
        self.assertEqual(answer["hooks"]["Stop"][0], original["hooks"]["Stop"][0])
        self.assertEqual(answer["hooks"]["Interrupt"], original["hooks"]["Interrupt"])
        self.assertEqual(h.configuration(answer), answer)
        self.assertEqual(len(original["hooks"]["Stop"]), 1)

    def filesystem_owner_fixture(self, external=False):
        self.f.seed["state_path"] = str(self.state)
        self.f.recovery_fixture()
        scope = json.loads(self.f.scope_path.read_bytes())
        if external:
            scope["repositories"] = []
            scope["baseline"]["repositories"] = {}
            scope["checks"] = [check for check in scope["checks"] if check["id"] != "source"]
            scope["categories"]["source"] = {"checks": [], "not_applicable": {
                "reason": "Synthetic app operation owns no Git changes; the contract owner is external.",
                "basis": [{"path": o["path"], "sha256": o["sha256"]} for o in scope["owners"]]}}
            self.f.result["checks"].pop("source")
        rows = {}
        slots = [x["identity"] for x in scope["baseline"]["repositories"].values()]
        for check in scope["checks"]:
            remote = check["target"].get("remote_binding", {})
            slots.extend(remote[k] for k in ("local", "destination") if k in remote)
        for slot in slots:
            location = slot["common_dir"]
            after = {k: slot[k] for k in ("device", "inode")}
            slot["device"] += 100
            rows[location] = {"path": location, "before": {k: slot[k] for k in ("device", "inode")}, "after": after}
        for slot in h.c.owner_identity_slots(self.f.recovery):
            location = slot["common_dir"]
            after = {k: slot[k] for k in ("device", "inode")}
            slot["device"] += 100
            rows[location] = {"path": location, "before": {k: slot[k] for k in ("device", "inode")}, "after": after}
        self.f.write(self.f.scope_path, scope); self.f.scope_ref = self.f.ref(self.f.scope_path)
        self.f.recovery["scope_sha256"] = self.f.scope_ref["sha256"]
        self.f.write(self.f.recovery_path, self.f.recovery)
        frozen_owner = self.f.recovery_path.read_bytes()
        self.f.write(self.f.recovery_review_path, {"scope_sha256": self.f.scope_ref["sha256"],
                    "recovery_sha256": self.f.ref(self.f.recovery_path)["sha256"], "status": "No Findings"})
        binding = {"evidence": self.f.ref(self.f.recovery_path), "review": self.f.ref(self.f.recovery_review_path)}
        self.f.write(self.f.review_path, {"scope_sha256": self.f.scope_ref["sha256"], "status": "No Findings"})
        waiting = {**self.f.state, "scope": self.f.scope_ref, "review": self.f.ref(self.f.review_path),
                   "owner_recovery": binding, "outcome": "waiting", "status_basis": self.f.ref(self.f.request)}
        self.root.mkdir(mode=0o700); self.f.write(self.state, waiting)
        evidence = self.f.root / "filesystem-evidence.json"; review = self.f.root / "filesystem-review.json"
        self.f.write(evidence, {"scope_sha256": self.f.scope_ref["sha256"], "task_id": scope["task_id"], "session_id": scope["session_id"],
             "previous_state_sha256": self.f.ref(self.state)["sha256"], "authorization": self.f.ref(self.f.request), "identities": list(rows.values())})
        self.f.write(review, {"scope_sha256": self.f.scope_ref["sha256"], "filesystem_recovery_sha256": self.f.ref(evidence)["sha256"], "status": "No Findings"})
        self.call("recover-filesystem", "--evidence", str(evidence), "--evidence-sha256", self.f.ref(evidence)["sha256"],
                  "--review", str(review), "--review-sha256", self.f.ref(review)["sha256"])
        final = json.loads(self.state.read_bytes())
        self.assertEqual(final["owner_recovery"], binding)
        self.assertIn("filesystem_recovery", final)
        self.assertEqual(self.f.recovery_path.read_bytes(), frozen_owner)
        self.f.result["scope_sha256"] = self.f.scope_ref["sha256"]
        self.f.write(self.f.result_path, self.f.result)
        self.f.write(self.f.final_review_path, {"scope_sha256": self.f.scope_ref["sha256"],
            "result_sha256": self.f.ref(self.f.result_path)["sha256"], "status": "No Findings",
            "owner_recovery_sha256": binding["evidence"]["sha256"],
            "filesystem_recovery_sha256": final["filesystem_recovery"]["evidence"]["sha256"]})
        self.call("outcome", "--status", "working", "--basis", str(self.f.request), "--basis-sha256", self.f.ref(self.f.request)["sha256"])
        self.call("finalize", "--result", str(self.f.result_path), "--review", str(self.f.final_review_path),
                  "--review-sha256", self.f.ref(self.f.final_review_path)["sha256"])
        self.assertTrue(self.call("inspect")["complete"])
        return binding, frozen_owner

    def test_filesystem_recovery_preserves_existing_owner_recovery(self):
        self.filesystem_owner_fixture()

    def test_device_then_owner_change_preserves_external_owner_identity_history(self):
        previous, frozen_owner = self.filesystem_owner_fixture(external=True)
        self.call("outcome", "--status", "waiting", "--basis", str(self.f.request), "--basis-sha256", self.f.ref(self.f.request)["sha256"])
        self.f.owner.write_text("Synthetic additional owner change after device review.\n")
        self.f.git("add", "README.md"); self.f.git("commit", "-m", "test: change external owner after device review")
        self.f.git("push", "origin", "main")
        revision = self.f.git("rev-parse", "HEAD")
        latest = json.loads(json.dumps(self.f.recovery))
        latest["previous"] = previous
        for owner in latest["owners"]:
            owner["identity"] = h.c.repo_identity(owner["repository"])
            owner["current"].update(revision=revision, sha256=self.f.ref(self.f.owner)["sha256"])
            target = owner["source"]["definition"]["target"]
            target["remote_binding"] = h.c.remote_identity(target["repository"], target["remote"])
            owner["source"]["expected"].update(local_revision=revision, remote_revision=revision)
            owner["source"]["expected"]["files"]["README.md"] = self.f.ref(self.f.owner)["sha256"]
        evidence = self.f.root / "next-owner.json"; review = self.f.root / "next-owner-review.json"
        self.f.write(evidence, latest)
        self.f.write(review, {"scope_sha256": self.f.scope_ref["sha256"], "recovery_sha256": self.f.ref(evidence)["sha256"], "status": "No Findings"})
        self.call("recover-owner", "--evidence", str(evidence), "--evidence-sha256", self.f.ref(evidence)["sha256"],
                  "--review", str(review), "--review-sha256", self.f.ref(review)["sha256"])
        state = json.loads(self.state.read_bytes())
        self.f.write(self.f.final_review_path, {"scope_sha256": self.f.scope_ref["sha256"],
            "result_sha256": self.f.ref(self.f.result_path)["sha256"], "status": "No Findings",
            "owner_recovery_sha256": self.f.ref(evidence)["sha256"],
            "filesystem_recovery_sha256": state["filesystem_recovery"]["evidence"]["sha256"]})
        self.call("outcome", "--status", "working", "--basis", str(self.f.request), "--basis-sha256", self.f.ref(self.f.request)["sha256"])
        self.call("finalize", "--result", str(self.f.result_path), "--review", str(self.f.final_review_path),
                  "--review-sha256", self.f.ref(self.f.final_review_path)["sha256"])
        self.assertTrue(self.call("inspect")["complete"])
        self.assertEqual(self.f.recovery_path.read_bytes(), frozen_owner)

    def test_stale_cas_and_changed_scope_refuse_completion(self):
        self.arm_bind()
        done = subprocess.run([sys.executable, "-I", str(SCRIPT), "--bindings", str(self.root), "reserve", "--session", self.f.seed["session_id"],
                               "--expected-sha256", "1"*64, "--id", "bad", *self.basis()], capture_output=True)
        self.assertEqual(done.returncode, 2)
        self.f.scope_path.write_text("{}"); self.assertEqual(h.hook(self.payload, self.root)["decision"], "block")


if __name__ == "__main__": unittest.main()
