"""Check frozen before/after observations, never execute input-selected commands.

This checks supplied bytes and predicates. The owner and independent reviewer
verify provenance, execution, meaningful criteria and the adequacy of the cases.
It does not establish causality, future performance or arbitrary chat coverage.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import sys


spec = importlib.util.spec_from_file_location("improvement_completion", Path(__file__).with_name("check_completion.py"))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def bounded_list(value, maximum=32):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise c.CompletionError("expected a nonempty bounded list")
    return value


def matches(result, assertions):
    """Compare JSON values without Python's True == 1 coercion."""
    for assertion in assertions:
        current = result
        for key in assertion["path"]:
            if not isinstance(current, dict) or key not in current:
                return False
            current = current[key]
        if c.canonical(current) != c.canonical(assertion["equals"]):
            return False
    return True


def observation(ref, case, implementation):
    value = c.read_reference(ref)
    c.fields(value, ("case_id", "input_sha256", "implementation_sha256", "result"))
    if (value["case_id"], value["input_sha256"], value["implementation_sha256"]) != (
        case["id"], case["input"]["sha256"], implementation
    ):
        raise c.CompletionError("observation belongs to another case, input or implementation")
    return matches(value["result"], case["assertions"])


def validate_plan(plan_ref, task_id=None, *, owners=()):
    plan = c.read_reference(plan_ref)
    c.fields(plan, ("task_id", "problem", "criterion", "basis", "baseline_sha256", "cases"))
    for key in ("task_id", "problem", "criterion"):
        c.text(plan[key])
    if task_id is not None and plan["task_id"] != task_id:
        raise c.CompletionError("improvement plan belongs to another task")
    # A declared owner edit preserves its original criterion in Git/a snapshot.
    # Match both frozen path and bytes; all other bases remain live references.
    if not isinstance(owners, (list, tuple)) or len(owners) > 32:
        raise c.CompletionError("expected bounded owner references")
    c.fields(plan["basis"], ("path", "sha256"))
    c.path(plan["basis"]["path"]); c.digest(plan["basis"]["sha256"])
    preserved = [owner for owner in owners if isinstance(owner, dict)
                 and owner.get("path") == plan["basis"]["path"]
                 and owner.get("sha256") == plan["basis"]["sha256"] and "original" in owner]
    if len(preserved) > 1:
        raise c.CompletionError("duplicate preserved basis")
    if preserved:
        c.owner_refs(preserved)
    else:
        c.reference(plan["basis"])
    c.digest(plan["baseline_sha256"])
    ids, inputs, paths, phases = set(), set(), set(), set()
    baseline_passes = 0
    reproduced = 0
    for case in bounded_list(plan["cases"]):
        c.fields(case, ("id", "phase", "input", "baseline", "assertions"))
        key = c.text(case["id"])
        if key in ids or not isinstance(case["phase"], str) or case["phase"] not in {"reproduction", "transfer"}:
            raise c.CompletionError("duplicate case or unsupported phase")
        ids.add(key)
        phases.add(case["phase"])
        c.reference(case["input"])
        identity = str(c.path(case["input"]["path"]))
        if case["input"]["sha256"] in inputs or identity in paths:
            raise c.CompletionError("reproduction and transfer cases need distinct input bytes and paths")
        inputs.add(case["input"]["sha256"])
        paths.add(identity)
        assertion_paths = set()
        for assertion in bounded_list(case["assertions"]):
            c.fields(assertion, ("path", "equals"))
            steps = tuple(c.text(part) for part in bounded_list(assertion["path"], 16))
            if steps in assertion_paths:
                raise c.CompletionError("duplicate assertion path")
            assertion_paths.add(steps)
            c.canonical(assertion["equals"])
        passed = observation(case["baseline"], case, plan["baseline_sha256"])
        baseline_passes += passed
        reproduced += case["phase"] == "reproduction" and not passed
    if phases != {"reproduction", "transfer"} or not reproduced:
        raise c.CompletionError("require a reproduced baseline failure and a separate transfer case")
    return plan, baseline_passes


def inspect(plan_ref, record_ref, task_id=None, *, owners=()):
    plan, baseline_passes = validate_plan(plan_ref, task_id, owners=owners)
    record = c.read_reference(record_ref)
    c.fields(record, ("plan_sha256", "candidate", "cases", "application"))
    if record["plan_sha256"] != plan_ref["sha256"]:
        raise c.CompletionError("record differs from the frozen improvement plan")
    candidates = c.refs(record["candidate"])
    source_paths = [ref["path"] for ref in candidates]
    if len(set(source_paths)) != len(source_paths):
        raise c.CompletionError("duplicate candidate source")
    candidate_sha = c.sha(c.canonical(candidates))
    results = bounded_list(record["cases"])
    if len(results) != len(plan["cases"]):
        raise c.CompletionError("improvement case coverage differs from the frozen plan")
    after_refs = []
    for case, row in zip(plan["cases"], results):
        c.fields(row, ("id", "after"))
        if row["id"] != case["id"]:
            raise c.CompletionError("case order/identity differs from the frozen plan")
        if row["after"] in after_refs or row["after"] == case["baseline"]:
            raise c.CompletionError("after observations must be distinct from each other and baseline")
        after_refs.append(row["after"])
        if not observation(row["after"], case, candidate_sha):
            raise c.CompletionError("candidate failed a frozen reproduction or transfer assertion")
    applied = bounded_list(record["application"])
    if len(applied) != len(candidates):
        raise c.CompletionError("actual usage must cover every candidate source")
    installed = set()
    for source, row in zip(candidates, applied):
        c.fields(row, ("source", "installed_path"))
        location = str(c.path(row["installed_path"]))
        if row["source"] != source or location in installed:
            raise c.CompletionError("installed source coverage differs or is duplicated")
        installed.add(location)
        if c.sha(c.raw_file(location, links=True)) != source["sha256"]:
            raise c.CompletionError("actual usage differs from the verified candidate")
    # Repeat every binding after reading the final observation and installed bytes.
    validate_plan(plan_ref, task_id, owners=owners)
    c.reference(record_ref)
    c.refs(candidates)
    for ref in after_refs:
        c.reference(ref)
    for source, row in zip(candidates, applied):
        if c.sha(c.raw_file(row["installed_path"], links=True)) != source["sha256"]:
            raise c.CompletionError("actual usage changed during inspection")
    return {"verified": True, "cases": len(results), "baseline_passes": baseline_passes,
            "candidate_passes": len(results), "candidate_minus_baseline_passes": len(results) - baseline_passes,
            "transfer_cases": sum(case["phase"] == "transfer" for case in plan["cases"]),
            "claim": "supplied frozen cases only; execution provenance and semantic quality require owner review; real outcome effect unverified"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--record")
    parser.add_argument("--record-sha256")
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args(argv)
    try:
        plan = {"path": args.plan, "sha256": args.plan_sha256}
        if bool(args.record) != bool(args.record_sha256):
            raise c.CompletionError("record requires its exact SHA-256")
        if args.record:
            result = inspect(plan, {"path": args.record, "sha256": args.record_sha256}, args.task_id)
        else:
            value, passes = validate_plan(plan, args.task_id)
            result = {"plan_valid": True, "cases": len(value["cases"]), "baseline_passes": passes,
                      "verified": False, "reason": "candidate observations and actual usage are still required"}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(json.dumps({"verified": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
