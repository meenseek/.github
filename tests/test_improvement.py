"""Synthetic observation fixtures and actual completion boundary checks."""

import copy
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import test_check_completion as completion_fixture


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_improvement.py"
spec = importlib.util.spec_from_file_location("improvement", SCRIPT)
i = importlib.util.module_from_spec(spec)
spec.loader.exec_module(i)
c = i.c


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "candidate.py"
        self.source.write_text(inspect.getsource(self.candidate_transition))
        self.basis = self.root / "owner.md"
        self.basis.write_text("Synthetic criterion: unresolved prevention cannot complete.\n")
        self.candidate = [self.ref(self.source)]
        self.candidate_sha = c.sha(c.canonical(self.candidate))
        self.plan = {"task_id": "synthetic-improvement", "problem": "Promise was accepted as prevention",
                     "criterion": "Known missing checks block completion", "basis": self.ref(self.basis),
                     "baseline_sha256": c.sha(inspect.getsource(self.baseline_transition).encode()), "cases": []}
        self.record = {"plan_sha256": "", "candidate": self.candidate, "cases": [],
                       "application": [{"source": self.candidate[0], "installed_path": str(self.source)}]}
        self.plan_path = self.root / "plan.json"
        self.record_path = self.root / "record.json"
        for domain in ("interview", "application", "video"):
            for condition in ("criterion-missing", "usage-missing", "ready"):
                phase = "reproduction" if condition == "criterion-missing" else "transfer"
                key = domain + "-" + condition
                supplied = {"domain": domain, "stage": "review" if phase == "reproduction" else "apply",
                            "checks": {"criterion": condition != "criterion-missing", "reproduction": True,
                                       "transfer": True, "application": condition != "usage-missing"}}
                input_ref = self.write(key + "-input.json", supplied)
                case = {"id": key, "phase": phase, "input": input_ref,
                        "assertions": [{"path": ["complete"], "equals": condition == "ready"}]}
                # Execute the prior task transition. It incorrectly accepts a promise.
                before = self.baseline_transition(supplied)
                case["baseline"] = self.write(key + "-before.json", self.envelope(case, self.plan["baseline_sha256"], before))
                self.plan["cases"].append(case)
                # Execute the corrected transition with the same input and criterion.
                after = self.candidate_transition(supplied)
                self.record["cases"].append({"id": key, "after": self.write(key + "-after.json", self.envelope(case, self.candidate_sha, after))})
        self.freeze()

    @staticmethod
    def baseline_transition(supplied):
        return {"complete": True, "prevention": "be careful next time"}

    @staticmethod
    def candidate_transition(supplied):
        required = ["criterion", "reproduction", "transfer"]
        if supplied["stage"] == "apply":
            required.append("application")
        return {"complete": all(supplied["checks"].get(key) is True for key in required)}

    def ref(self, path):
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def write(self, name, value):
        path = self.root / name
        path.write_bytes(c.canonical(value) + b"\n")
        return self.ref(path)

    @staticmethod
    def envelope(case, implementation, result):
        return {"case_id": case["id"], "input_sha256": case["input"]["sha256"],
                "implementation_sha256": implementation, "result": result}

    def freeze(self):
        self.plan_ref = self.write("plan.json", self.plan)
        self.record["plan_sha256"] = self.plan_ref["sha256"]
        self.record_ref = self.write("record.json", self.record)

    def inspect(self):
        return i.inspect(self.plan_ref, self.record_ref, self.plan["task_id"])

    def test_synthetic_three_domain_observations_and_transfer(self):
        report = self.inspect()
        self.assertEqual((report["baseline_passes"], report["candidate_passes"], report["transfer_cases"]), (3, 9, 6))

    def test_promise_only_record_is_refused(self):
        self.record = {"promise": "next time I will check"}
        self.freeze()
        with self.assertRaises(ValueError): self.inspect()

    def test_missing_criterion_cannot_be_frozen(self):
        del self.plan["criterion"]
        self.freeze()
        with self.assertRaises(ValueError): self.inspect()

    def test_owner_establishes_criterion_before_freezing(self):
        self.plan["criterion"] = "Owner reviewed new missing-check criterion"
        self.freeze()
        self.assertTrue(self.inspect()["verified"])

    def test_failed_transfer_is_refused(self):
        case = self.plan["cases"][-1]
        self.record["cases"][-1]["after"] = self.write("regression.json", self.envelope(case, self.candidate_sha, {"complete": False}))
        self.freeze()
        with self.assertRaisesRegex(ValueError, "transfer assertion"): self.inspect()

    def test_same_input_relabelled_as_transfer_is_refused(self):
        self.plan["cases"][1]["input"] = self.plan["cases"][0]["input"]
        self.freeze()
        with self.assertRaisesRegex(ValueError, "distinct input"): self.inspect()

    def test_stale_plan_and_input_and_candidate_are_refused(self):
        for ref in (self.plan_ref, self.plan["cases"][0]["input"], self.candidate[0]):
            path = Path(ref["path"]); original = path.read_bytes()
            path.write_bytes(original + b" ")
            with self.assertRaises(ValueError): self.inspect()
            path.write_bytes(original)

    def test_actual_usage_mismatch_is_refused(self):
        installed = self.root / "installed.py"; installed.write_text("old implementation\n")
        self.record["application"][0]["installed_path"] = str(installed)
        self.freeze()
        with self.assertRaisesRegex(ValueError, "actual usage differs"): self.inspect()

    def test_missing_input_and_partial_case_coverage_are_refused(self):
        self.record["cases"].pop(); self.freeze()
        with self.assertRaisesRegex(ValueError, "coverage"): self.inspect()
        Path(self.plan["cases"][0]["input"]["path"]).unlink()
        with self.assertRaises(OSError): self.inspect()

    def test_wrong_implementation_and_task_are_refused(self):
        with self.assertRaisesRegex(ValueError, "another task"):
            i.inspect(self.plan_ref, self.record_ref, "other")
        case = self.plan["cases"][0]
        self.record["cases"][0]["after"] = self.write("wrong.json", self.envelope(case, "f" * 64, {"complete": False}))
        self.freeze()
        with self.assertRaisesRegex(ValueError, "implementation"): self.inspect()

    def preserved_owner(self):
        repo = self.root / "repo"; repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout.decode().strip()
        git("config", "user.name", "Synthetic test")
        git("config", "user.email", "test@example.invalid")
        basis = repo / "owner.md"; basis.write_bytes(self.basis.read_bytes())
        git("add", "owner.md"); git("commit", "-m", "test: preserve criterion")
        self.plan["basis"] = self.ref(basis); self.freeze()
        owner = {**self.ref(basis), "original": {"repository": str(repo), "file": "owner.md",
                 "identity": c.repo_identity(str(repo)), "revision": git("rev-parse", "HEAD")}}
        basis.write_text("Reviewed updated owner; original criterion retained.\n")
        return owner

    def test_planned_basis_uses_verified_original_and_completion_observer(self):
        owner = self.preserved_owner()
        with self.assertRaisesRegex(ValueError, "source bytes changed"):
            self.inspect()
        check = {"kind": "improvement", "target": {"path": self.record_ref["path"], "plan": self.plan_ref}}
        self.assertTrue(c.observe(check, {"sha256": self.record_ref["sha256"]}, owners=[owner])["verified"])
        self.assertEqual(i.validate_plan(self.plan_ref, owners=[owner])[0]["basis"], self.plan["basis"])

    def test_unplanned_or_forged_basis_lineage_is_refused(self):
        owner = self.preserved_owner()
        for change in ({"revision": "0" * 40}, {"file": "other.md"},
                       {"identity": {**owner["original"]["identity"], "inode": 0}}):
            forged = copy.deepcopy(owner); forged["original"].update(change)
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                i.inspect(self.plan_ref, self.record_ref, owners=[forged])
        snapshot = self.root / "snapshot.md"; snapshot.write_text("forged criterion")
        forged = copy.deepcopy(owner); forged["original"].pop("revision")
        forged["original"]["snapshot"] = self.ref(snapshot)
        with self.assertRaisesRegex(ValueError, "snapshot differs"):
            i.validate_plan(self.plan_ref, owners=[forged])
        for owners in ([], [{**owner, "sha256": "0" * 64}]):
            with self.assertRaisesRegex(ValueError, "source bytes changed"):
                i.validate_plan(self.plan_ref, owners=owners)

    def test_preserved_basis_is_rechecked_after_observations(self):
        owner = self.preserved_owner()
        snapshot = self.root / "snapshot.md"; snapshot.write_bytes(self.basis.read_bytes())
        owner["original"].pop("revision"); owner["original"]["snapshot"] = self.ref(snapshot)
        original = i.observation
        def mutate(*args):
            answer = original(*args)
            if args[2] == self.candidate_sha:
                snapshot.write_text("changed after candidate observation")
            return answer
        with mock.patch.object(i, "observation", side_effect=mutate), self.assertRaisesRegex(ValueError, "source bytes changed"):
            i.inspect(self.plan_ref, self.record_ref, owners=[owner])

    def recovered_basis_fixture(self):
        bindings = self.root / "bindings"; bindings.mkdir(mode=0o700)
        state = bindings / "basis-recovery.json"
        request = self.write("request.json", {"request": "Synthetic owner recovery"})
        check = {"id": "prevention", "category": "quality", "kind": "improvement",
                 "target": {"path": str(self.record_path), "plan": self.plan_ref}}
        categories = {name: {"checks": [], "not_applicable": {
            "reason": "Synthetic test observes only the improvement boundary.", "basis": [self.plan["basis"]]}}
            for name in c.CATEGORIES}
        categories["quality"] = {"checks": ["prevention"], "not_applicable": None}
        scope = c.prepare({"task_id": self.plan["task_id"], "session_id": "basis-recovery",
            "request": request, "request_digest": request["sha256"], "owners": [self.plan["basis"]],
            "categories": categories, "checks": [check], "repositories": [], "artifact_roots": [],
            "state_path": str(state), "workspace": str(self.root)})
        scope_ref = self.write("scope.json", scope)
        review = self.write("scope-review.json", {"scope_sha256": scope_ref["sha256"], "status": "No Findings"})
        hook = SCRIPT.with_name("completion_hook.py")
        def call(command, *args):
            argv = [sys.executable, "-I", str(hook), "--bindings", str(bindings), command,
                    "--session", "basis-recovery"]
            if state.exists(): argv += ["--expected-sha256", self.ref(state)["sha256"]]
            if command == "complete": argv += ["--task-id", scope["task_id"], "--request-digest", scope["request_digest"]]
            done = subprocess.run([*argv, *args], capture_output=True, timeout=35)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            return json.loads(done.stdout)
        call("arm", "--task-id", scope["task_id"])
        call("bind", "--scope", scope_ref["path"], "--scope-sha256", scope_ref["sha256"],
             "--review", review["path"], "--review-sha256", review["sha256"])
        original = self.root / "original.md"; original.write_bytes(self.basis.read_bytes())
        current = self.root / "current.md"; current.write_text("Normal independent owner update.\n")
        self.basis.write_bytes(current.read_bytes())
        evidence = self.write("recovery.json", {"scope_sha256": scope_ref["sha256"],
            "task_id": scope["task_id"], "session_id": scope["session_id"], "authorization": request,
            "owners": [{"owner_path": str(self.basis), "snapshots": {
                "original": self.ref(original), "current": self.ref(current)}}]})
        reviewed = self.write("recovery-review.json", {"scope_sha256": scope_ref["sha256"],
            "recovery_sha256": evidence["sha256"], "status": "No Findings"})
        return scope_ref, scope, state, request, original, {"evidence": evidence, "review": reviewed}, call

    def test_recovered_basis_actual_cli_preserves_criteria_and_completes(self):
        scope_ref, scope, state, request, original, binding, call = self.recovered_basis_fixture()
        frozen = Path(scope_ref["path"]).read_bytes(), self.plan_path.read_bytes()
        call("outcome", "--status", "waiting", "--basis", request["path"], "--basis-sha256", request["sha256"])
        call("recover-owner", "--evidence", binding["evidence"]["path"], "--evidence-sha256", binding["evidence"]["sha256"],
             "--review", binding["review"]["path"], "--review-sha256", binding["review"]["sha256"])
        call("outcome", "--status", "working", "--basis", request["path"], "--basis-sha256", request["sha256"])
        result = c.draft_result(scope_ref, state_path=str(state))
        result_ref = self.write("result.json", result)
        final_path = self.root / "final-review.json"
        reviewed = c.final_review_fields(scope_ref, scope, json.loads(state.read_bytes()),
                                        result_ref["sha256"], str(final_path), stopped=False)
        final_ref = self.write(final_path.name, reviewed)
        answer = call("complete", "--work-turn", "synthetic-closing-turn", "--result", result_ref["path"],
                      "--review", final_ref["path"], "--review-sha256", final_ref["sha256"])
        self.assertTrue(answer["binding_released"])
        self.assertFalse(state.exists())
        self.assertEqual(frozen, (Path(scope_ref["path"]).read_bytes(), self.plan_path.read_bytes()))
        self.assertEqual(self.basis.read_bytes(), (self.root / "current.md").read_bytes())

    def test_recovered_basis_rejects_plain_map_wrong_lineage_and_changed_original(self):
        scope_ref, scope, state, request, original, binding, call = self.recovered_basis_fixture()
        recovery = c.owner_recovery(scope_ref, scope, binding)
        self.assertTrue(i.inspect(self.plan_ref, self.record_ref, owners=scope["owners"], recovery=recovery)["verified"])
        original_proof = recovery.originals[str(self.basis)]["snapshot"]
        recovery.originals[str(self.basis)]["snapshot"] = self.ref(self.root / "current.md")
        with self.assertRaises(ValueError):
            i.validate_plan(self.plan_ref, owners=scope["owners"], recovery=recovery)
        recovery.originals[str(self.basis)]["snapshot"] = original_proof
        with self.assertRaisesRegex(ValueError, "source bytes changed"):
            i.validate_plan(self.plan_ref, owners=scope["owners"], recovery=dict(recovery))
        self.plan["basis"] = {**self.plan["basis"], "sha256": "0" * 64}; self.freeze()
        with self.assertRaises(ValueError):
            i.validate_plan(self.plan_ref, owners=scope["owners"], recovery=recovery)
        self.plan["basis"] = scope["owners"][0]; self.freeze()
        observation = i.observation
        def mutate(*args):
            result = observation(*args)
            if args[2] == self.candidate_sha:
                original.write_text("Altered preserved criterion after observing the candidate.\n")
            return result
        with mock.patch.object(i, "observation", side_effect=mutate), self.assertRaisesRegex(ValueError, "source bytes changed"):
            i.inspect(self.plan_ref, self.record_ref, owners=scope["owners"], recovery=recovery)

    def test_recovered_git_basis_rechecks_original_revision(self):
        fixture = completion_fixture.CompletionTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.recovery_fixture()
        scope = json.loads(fixture.scope_path.read_bytes())
        self.plan["basis"] = scope["owners"][0]; self.freeze()
        recovery = c.owner_recovery(fixture.scope_ref, scope, fixture.recovery_binding)
        self.assertTrue(i.inspect(self.plan_ref, self.record_ref, owners=scope["owners"], recovery=recovery)["verified"])
        recovery.originals[str(fixture.owner)]["original"]["revision"] = "0" * 40
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            i.validate_plan(self.plan_ref, owners=scope["owners"], recovery=recovery)

    def test_json_bool_is_not_number(self):
        self.assertFalse(i.matches({"x": 0}, [{"path": ["x"], "equals": False}]))

    def test_cli_reports_only_verified_case_scope(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--plan", self.plan_ref["path"],
                                 "--plan-sha256", self.plan_ref["sha256"], "--task-id", self.plan["task_id"],
                                 "--record", self.record_ref["path"], "--record-sha256", self.record_ref["sha256"]], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["candidate_passes"], 9)

    def test_invalid_phase_type_is_a_structured_cli_refusal(self):
        for phase in ([], {}):
            self.plan["cases"][0]["phase"] = phase
            self.freeze()
            result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--plan", self.plan_ref["path"],
                                     "--plan-sha256", self.plan_ref["sha256"], "--task-id", self.plan["task_id"]], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(json.loads(result.stdout)["verified"])
            self.assertNotIn("Traceback", result.stderr)
            with self.assertRaises(ValueError):
                i.validate_plan(self.plan_ref)

    def test_actual_completion_boundary_refuses_promises_for_three_domains(self):
        # Exercise actual old evidence observation and the new improvement
        # observation; the three domain labels do not claim live integrations.
        for domain in ("interview", "application", "video"):
            promise = self.write(domain + "-promise.json", {"prevention": "be careful next time"})
            evidence = {"kind": "evidence", "target": {"path": promise["path"]}}
            self.assertEqual(c.observe(evidence, {"sha256": promise["sha256"]})["sha256"], promise["sha256"])
            improved = {"kind": "improvement", "target": {"path": promise["path"], "plan": self.plan_ref}}
            with self.assertRaises(ValueError):
                c.observe(improved, {"sha256": promise["sha256"]})
        verified = {"kind": "improvement", "target": {"path": self.record_ref["path"], "plan": self.plan_ref}}
        self.assertTrue(c.observe(verified, {"sha256": self.record_ref["sha256"]})["verified"])


if __name__ == "__main__":
    unittest.main()
