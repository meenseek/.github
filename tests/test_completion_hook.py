import importlib.util
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

    def arm_bind(self):
        self.call("arm", "--task-id", self.f.seed["task_id"])
        self.f.seed["state_path"] = str(self.state)
        self.f.freeze()  # reviewed synthetic scope/result stored outside pointer directory
        self.call("bind", "--scope", str(self.f.scope_path), "--scope-sha256", self.f.scope_ref["sha256"],
                  "--review", str(self.f.review_path), "--review-sha256", self.f.ref(self.f.review_path)["sha256"])
        self.call("finalize", "--result", str(self.f.result_path), "--review", str(self.f.final_review_path),
                  "--review-sha256", self.f.ref(self.f.final_review_path)["sha256"])

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
