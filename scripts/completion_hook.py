"""Thin advisory Codex adapter. No task execution, polling, transcript parsing or deletion."""
import argparse
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile

# Hook observations must not create repository bytecode artifacts.
sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("completion", Path(__file__).with_name("check_completion.py"))
c = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(c)
ROOT = Path(__file__).resolve().parents[1] / ".completion"
GUIDANCE = ("Desktop 정리 요청은 변경 전에 completion_hook.py arm으로 이 session_id를 연결하고 "
            "docs/task-completion.md의 범위 고정·검증·리뷰·실제 적용·자원 정리를 끝까지 수행하세요. "
            "정리에는 commit/main/remote, 실제 최신 사용본, 배포 판단·상태, 산출물, 브랜치/worktree, "
            "임시 파일/프로세스/탭/자식 작업, 중단 복구가 포함됩니다. 완료는 현재 관측으로 확인하세요.")


def pointer(root, session):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", session):
        raise c.CompletionError("invalid session identity")
    return c.path(str(root)) / (session + ".json")


@contextmanager
def locked(root):
    root = c.path(str(root))
    if root.exists() and (root.is_symlink() or root.stat().st_mode & 0o077):
        raise c.CompletionError("binding directory must be private and real")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def replace(filename, value, expected):
    if filename.exists() and c.sha(c.raw_file(str(filename))) != expected:
        raise c.CompletionError("pointer changed; reread before mutation")
    if not filename.exists() and expected is not None:
        raise c.CompletionError("pointer disappeared")
    fd, tmp = tempfile.mkstemp(dir=filename.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(c.canonical(value) + b"\n")
            stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, filename)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    return {"path": str(filename), "sha256": c.sha(c.raw_file(str(filename)))}


def load_state(filename):
    raw = c.raw_file(str(filename))
    value = c.decode(raw)
    c.fields(value, ("task_id", "session_id", "intent", "scope", "review", "final_review", "result_path", "resources", "outcome"), ("status_basis",))
    if value["intent"] != "closure": raise c.CompletionError("invalid closure intent")
    return value, c.sha(raw)


def hook(payload, root=ROOT):
    event = payload.get("hook_event_name")
    if event not in {"SessionStart", "UserPromptSubmit", "Stop"}: return {}
    filename = pointer(root, c.text(payload.get("session_id")))
    cwd = c.path(payload.get("cwd"))
    desktop = Path(__file__).resolve().parents[2]
    # Bound worktree sessions stay covered even when their cwd is outside Desktop.
    if not filename.exists() and not cwd.is_relative_to(desktop): return {}
    if event != "Stop":
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": GUIDANCE + " 현재 session_id=" + payload["session_id"]}}
    if not filename.exists():
        return {"systemMessage": "정리 완료 검증에 연결되지 않은 세션입니다. 정리 의도가 있으면 먼저 arm/bind 하세요. 미연결 상태는 완료 보증이 아닙니다."}
    try:
        state, _ = load_state(filename)
        if state["session_id"] != payload["session_id"]:
            raise c.CompletionError("session binding changed")
        if state["outcome"] in {"waiting", "blocked", "cancelled"}:
            c.reference(state.get("status_basis"))
            return {"systemMessage": "정리 미완료: " + state["outcome"] + ". 남은 범위·이유는 상태 근거에 보존되어 있습니다. 완료로 보고하지 마세요."}
        if state["outcome"] != "working" or state["scope"] is None:
            raise c.CompletionError("armed task has no frozen reviewed scope")
        scope = c.read_reference(state["scope"])
        if scope["state_path"] != str(filename): raise c.CompletionError("resource pointer is not bound to this session")
        answer = c.check_scope(state["scope"], state["result_path"], task_id=state["task_id"], workspace=str(cwd), review_ref=state["review"])
        if answer["complete"]: return {"systemMessage": "정리 검사 통과: 고정한 범위의 현재 관측 완료. 사용자 최종 보고에 적용·배포·잔여 자원·예외를 포함하세요."}
        reason = "정리 미완료: " + json.dumps(answer["failures"], ensure_ascii=False)[:3000]
    except (c.CompletionError, OSError, ValueError, TypeError, KeyError) as exc:
        reason = "정리 미완료: " + str(exc)[:1000]
    if payload.get("stop_hook_active"):
        return {"systemMessage": reason + ". 자동 반복은 멈춥니다. 완료라고 보고하지 말고 해결하거나 상태 근거를 남기세요."}
    return {"decision": "block", "reason": reason + ". 승인된 남은 정리를 계속하고 현재 관측을 다시 확인하세요."}


def configuration(original):
    if not isinstance(original, dict) or not isinstance(original.get("hooks", {}), dict):
        raise c.CompletionError("invalid hooks configuration")
    result = c.decode(c.canonical(original))
    events = result.setdefault("hooks", {})
    command = shlex.quote(str(Path(sys.executable).resolve())) + " -I " + shlex.quote(str(Path(__file__).resolve())) + " hook"
    for event in ("SessionStart", "UserPromptSubmit", "Stop"):
        group = {"hooks": [{"type": "command", "command": command, "timeout": 35}]}
        groups = events.setdefault(event, [])
        if not isinstance(groups, list): raise c.CompletionError("invalid hook groups")
        existing = [g for g in groups if any("completion_hook.py" in h.get("command", "") for h in g.get("hooks", []))]
        if existing and existing != [group]: raise c.CompletionError("changed completion hook requires explicit review")
        if not existing: groups.append(group)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bindings", default=str(ROOT), help="private per-session pointer directory; host installation uses the default")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("hook")
    config = subs.add_parser("configure")
    config.add_argument("--existing", required=True); config.add_argument("--output", required=True)
    for name in ("arm", "bind", "reserve", "attach", "finalize", "complete", "outcome", "inspect", "unbind", "release"):
        item = subs.add_parser(name)
        item.add_argument("--session", required=True)
        if name == "arm": item.add_argument("--task-id", required=True)
        else: item.add_argument("--expected-sha256", required=True, help="current pointer SHA; CAS under a local lock")
        if name == "bind":
            item.add_argument("--scope", required=True); item.add_argument("--scope-sha256", required=True)
            item.add_argument("--review", required=True); item.add_argument("--review-sha256", required=True)
        if name in {"reserve", "attach"}:
            item.add_argument("--id", required=True); item.add_argument("--basis", required=True); item.add_argument("--basis-sha256", required=True)
        if name == "attach": item.add_argument("--resource", required=True, help="JSON with fixed kind,target; actual tool handle and owner record")
        if name in {"finalize", "complete"}:
            item.add_argument("--result", required=True); item.add_argument("--review", required=True); item.add_argument("--review-sha256", required=True)
        if name == "outcome":
            item.add_argument("--status", required=True, choices=("working", "waiting", "blocked", "cancelled"))
            item.add_argument("--basis", required=True); item.add_argument("--basis-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "hook":
            answer = hook(c.decode(sys.stdin.buffer.read(c.MAX_BYTES + 1)), Path(args.bindings))
        elif args.command == "configure":
            answer = c.write_new(args.output, configuration(c.decode(c.raw_file(args.existing))))
        else:
            filename = pointer(Path(args.bindings), args.session)
            with locked(filename.parent):
                if args.command == "arm":
                    if os.path.lexists(filename): raise c.CompletionError("session already armed; reuse its current pointer")
                    state = {"task_id": c.text(args.task_id), "session_id": args.session, "intent": "closure",
                             "scope": None, "review": None, "final_review": None, "result_path": None, "resources": [], "outcome": "working"}
                    answer = replace(filename, state, None)
                else:
                    state, before = load_state(filename)
                    if before != c.digest(args.expected_sha256) or state["session_id"] != args.session:
                        raise c.CompletionError("stale pointer or wrong session")
                    if args.command == "bind":
                        if state["scope"] is not None: raise c.CompletionError("scope already frozen; recovery must not replace original obligations")
                        scope_ref = c.reference({"path": args.scope, "sha256": args.scope_sha256})
                        review_ref = c.reference({"path": args.review, "sha256": args.review_sha256})
                        scope = c.read_reference(scope_ref); c.validate_scope(scope)
                        if (scope["task_id"], scope["session_id"], scope["state_path"]) != (state["task_id"], args.session, str(filename)):
                            raise c.CompletionError("scope task/session/resource pointer differs")
                        if c.read_reference(review_ref) != {"scope_sha256": args.scope_sha256, "status": "No Findings"}:
                            raise c.CompletionError("review is not for this exact scope")
                        state.update(scope=scope_ref, review=review_ref)
                    elif args.command in {"reserve", "attach"}:
                        basis = c.reference({"path": args.basis, "sha256": args.basis_sha256})
                        found = [r for r in state["resources"] if r["id"] == args.id]
                        if args.command == "reserve":
                            if found or len(state["resources"]) >= 128: raise c.CompletionError("duplicate or excessive resource reservation")
                            state["resources"].append({"id": c.text(args.id), "kind": "reserved", "target": {}, "basis": basis})
                        else:
                            if len(found) != 1 or found[0]["kind"] != "reserved": raise c.CompletionError("resource must be reserved before creation")
                            resource = c.decode(c.raw_file(args.resource)); c.fields(resource, ("kind", "target"))
                            if resource["kind"] not in {"absent", "ref_absent", "remote_ref_absent", "worktree_absent", "process_absent"} or not isinstance(resource["target"], dict):
                                raise c.CompletionError("pointer covers temporary resources only; retained outputs/runtime need fixed owner scope checks")
                            if resource["kind"] == "remote_ref_absent":
                                target = resource["target"]
                                target["remote_binding"] = c.remote_identity(target["repository"], target["remote"])
                            found[0].update(resource, basis=basis)
                    elif args.command in {"finalize", "complete"}:
                        state.update(result_path=str(c.path(args.result)), final_review=c.reference({"path": args.review, "sha256": args.review_sha256}))
                        if args.command == "complete":
                            # Preserve reviewed inputs for recovery before observing. A failed
                            # inspection leaves this CAS-updated pointer bound for retry.
                            before = replace(filename, state, before)["sha256"]
                            scope = c.read_reference(state["scope"])
                            answer = c.check_scope(state["scope"], state["result_path"], task_id=state["task_id"],
                                                   workspace=scope["environment"]["workspace"], review_ref=state["review"], report=True)
                            if answer["complete"]:
                                if c.sha(c.raw_file(str(filename))) != before: raise c.CompletionError("pointer changed")
                                filename.unlink()
                            answer["binding_released"] = answer["complete"]
                            print(c.canonical(answer).decode()); return 0 if answer["complete"] else 2
                    elif args.command == "outcome":
                        state.update(outcome=args.status, status_basis=c.reference({"path": args.basis, "sha256": args.basis_sha256}))
                    else:
                        scope = c.read_reference(state["scope"])
                        answer = c.check_scope(state["scope"], state["result_path"], task_id=state["task_id"], workspace=scope["environment"]["workspace"], review_ref=state["review"], stopped=args.command == "release")
                        if args.command in {"unbind", "release"}:
                            if not (answer["stopped"] if args.command == "release" else answer["complete"]): raise c.CompletionError("unfinished task remains bound")
                            if c.sha(c.raw_file(str(filename))) != before: raise c.CompletionError("pointer changed")
                            filename.unlink()
                        print(c.canonical(answer).decode()); return 0 if answer["complete"] or answer["stopped"] else 2
                    answer = replace(filename, state, before)
        print(c.canonical(answer).decode()); return 0
    except (c.CompletionError, OSError, ValueError, TypeError, KeyError) as exc:
        print(c.canonical({"complete": False, "error": str(exc)}).decode()); return 2


if __name__ == "__main__": sys.exit(main())
