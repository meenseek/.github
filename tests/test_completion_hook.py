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

    def test_complete_actual_cli_finalizes_inspects_and_releases_once(self):
        self.arm_bind(finalize=False)
        answer = self.call("complete", *self.completion_args())
        self.assertTrue(answer["complete"]); self.assertTrue(answer["binding_released"])
        self.assertFalse(self.state.exists())
        self.assertTrue(self.f.scope_path.exists()); self.assertTrue(self.f.result_path.exists())
        self.assertEqual(answer["final_review"], self.f.ref(self.f.final_review_path))

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

    def test_complete_refuses_changed_review_and_stale_pointer(self):
        self.arm_bind(finalize=False)
        argv = ["--bindings", str(self.root), "complete", "--session", self.f.seed["session_id"],
                "--expected-sha256", "1"*64, *self.completion_args()]
        before = self.state.read_bytes()
        with redirect_stdout(io.StringIO()): self.assertEqual(h.main(argv), 2)
        self.assertEqual(self.state.read_bytes(), before)
        self.f.final_review_path.write_text("{}")
        self.call("complete", *self.completion_args(), ok=False)
        self.assertTrue(self.state.exists())

    def test_complete_reuses_one_inspection_and_retains_mid_inspection_mutation(self):
        self.arm_bind(finalize=False)
        argv = ["--bindings", str(self.root), "complete", "--session", self.f.seed["session_id"],
                "--expected-sha256", h.c.sha(self.state.read_bytes()), *self.completion_args()]
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
        frozen = self.f.recovery_fixture()
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

    def test_stale_cas_and_changed_scope_refuse_completion(self):
        self.arm_bind()
        done = subprocess.run([sys.executable, "-I", str(SCRIPT), "--bindings", str(self.root), "reserve", "--session", self.f.seed["session_id"],
                               "--expected-sha256", "1"*64, "--id", "bad", *self.basis()], capture_output=True)
        self.assertEqual(done.returncode, 2)
        self.f.scope_path.write_text("{}"); self.assertEqual(h.hook(self.payload, self.root)["decision"], "block")


if __name__ == "__main__": unittest.main()
