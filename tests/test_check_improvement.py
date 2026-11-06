"""Regression coverage for accepted case sets beyond former list budgets."""
import importlib.util
from pathlib import Path
import unittest
spec = importlib.util.spec_from_file_location("improvement", Path(__file__).resolve().parents[1] / "scripts/check_improvement.py")
i = importlib.util.module_from_spec(spec); spec.loader.exec_module(i)

class CaseCapacityTests(unittest.TestCase):
    def test_all_explicit_assertions_past32_and_paths_past16_are_checked(self):
        result = {}; value = result
        path = [f"key-{index}" for index in range(20)]
        for key in path[:-1]: value[key] = {}; value = value[key]
        value[path[-1]] = True
        assertions = [{"path":path, "equals":True} for _ in range(40)]
        self.assertTrue(i.matches(result, i.nonempty_list(assertions)))
        assertions[-1]["equals"] = False
        self.assertFalse(i.matches(result, assertions))
        with self.assertRaises(ValueError): i.nonempty_list([])

    def test_full_improvement_inspection_accepts_more_than32_distinct_cases(self):
        spec = importlib.util.spec_from_file_location("fixture", Path(__file__).with_name("test_improvement.py"))
        fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)
        f = fixture.ImprovementTests(); f.setUp(); self.addCleanup(f.doCleanups)
        for index in range(40):
            supplied = {"domain":f"transfer-{index}", "stage":"apply",
                        "checks":{"criterion":False,"reproduction":True,"transfer":True,"application":True}}
            key = f"distinct-{index}"
            case = {"id":key,"phase":"transfer","input":f.write(key+"-input.json",supplied),
                    "assertions":[{"path":["complete"],"equals":False}]}
            case["baseline"] = f.write(key+"-before.json", f.envelope(case,f.plan["baseline_sha256"],f.baseline_transition(supplied)))
            f.plan["cases"].append(case)
            f.record["cases"].append({"id":key,"after":f.write(key+"-after.json",f.envelope(case,f.candidate_sha,f.candidate_transition(supplied)))})
        import json
        case = f.plan["cases"][0]
        row = f.record["cases"][0]
        prefix = [f"nested-{index}" for index in range(20)]
        case["assertions"] = [{"path":[*prefix,f"result-{index}"],"equals":False} for index in range(33)]
        def nested(value):
            result = {f"result-{index}":value for index in range(33)}
            for key in reversed(prefix): result = {key:result}
            return result
        for reference in [case["baseline"],row["after"]]:
            path = Path(reference["path"]); observed = json.loads(path.read_bytes())
            observed["result"] = nested(observed["result"]["complete"])
            f.write(path.name,observed); reference["sha256"] = f.ref(path)["sha256"]
        f.freeze()
        self.assertEqual(f.inspect()["cases"],49)
        path = Path(row["after"]["path"]); observed = json.loads(path.read_bytes()); value = observed["result"]
        for key in prefix: value = value[key]
        value["result-32"] = True
        f.write(path.name,observed); row["after"] = f.ref(path); f.freeze()
        with self.assertRaisesRegex(ValueError,"failed a frozen"):
            f.inspect()
