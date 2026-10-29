"""Advisory Codex adapter; explicit completion can remove reviewed task-only records."""
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
GUIDANCE = ("Desktop 저장소 변경 작업은 별도 정리 요청이 없어도 첫 변경·작업 branch/worktree 생성 전에 "
            "completion_hook.py arm으로 이 session_id를 연결하고 "
            "docs/task-completion.md의 범위 고정·검증·리뷰·실제 적용·자원 정리를 끝까지 수행하세요. "
            "정리에는 commit/main/remote, 실제 최신 사용본, 배포 판단·상태, 산출물, 브랜치/worktree, "
            "임시 파일/프로세스/탭/자식 작업, 중단 작업 재개가 포함됩니다. 명시적인 정리 요청도 같은 계약을 따릅니다. "
            "상태 보고는 실제 남은 조치를 쓰고 파일 손상·유실은 확인된 경우에만 보고하세요. "
            "읽기 전용 조사·리뷰와 단순 질문만 하는 작업은 새 변경 작업으로 추정하지 마세요. "
            "직접 작업의 임시 완료 기록은 저장소 밖 전용 record_root에 모으고 scope에 고정하세요. "
            "complete/unbind 성공 시 그 기록 폴더까지 제거하며 완료 receipt 파일을 다시 만들지 마세요. 완료는 현재 관측으로 확인하세요.")


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
    if len(c.canonical(value)) + 1 > c.MAX_BYTES:
        raise c.CompletionError("completion state exceeds input limit")
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
    c.fields(value, ("task_id", "session_id", "intent", "scope", "review", "final_review", "result_path", "resources", "outcome"), ("status_basis", "owner_recovery", "source_preservation", "filesystem_recovery", "cleanup"))
    if value["intent"] != "closure": raise c.CompletionError("invalid closure intent")
    return value, c.sha(raw)


def task_context(state, filename, session):
    """Read the bound goal, without executing its observations or inferring a new one."""
    if state["session_id"] != session:
        raise c.CompletionError("session binding changed")
    context = {"task_id": state["task_id"], "outcome": state["outcome"], "scope": state["scope"]}
    if "cleanup" in state:
        context.update(request_digest=state["cleanup"]["request_digest"], record_cleanup_pending=True)
        return context
    if state["scope"] is not None:
        scope = c.read_reference(state["scope"])
        if (scope["task_id"], scope["session_id"], scope["state_path"]) != (state["task_id"], session, str(filename)):
            raise c.CompletionError("goal differs from the frozen session binding")
        context.update(request=c.reference(scope["request"]), request_digest=c.digest(scope["request_digest"]),
                       obligations=[check["id"] for check in scope["checks"]])
    if state["outcome"] in {"waiting", "blocked", "cancelled"}:
        context["status_basis"] = c.reference(state["status_basis"])
    return context


def remove_records(record, manifest):
    """Resume exact-file cleanup without following paths out of the owned directory."""
    current = c.record_inventory(record)
    if any(name not in manifest or manifest[name] != value for name, value in current.items()):
        raise c.CompletionError("task records changed or gained unreviewed files")
    if not os.path.lexists(record["path"]):
        return
    root = c.path(record["path"])
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(root_fd)
        if (info.st_dev, info.st_ino) != (record["device"], record["inode"]):
            raise c.CompletionError("record root identity changed")
        for name in sorted(current, key=lambda name: (len(Path(name).parts), name), reverse=True):
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
                raise c.CompletionError("invalid record manifest path")
            parent_fd = os.dup(root_fd)
            try:
                prefix = Path()
                for part in relative.parts[:-1]:
                    prefix /= part
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                    os.close(parent_fd); parent_fd = child
                    info = os.fstat(parent_fd)
                    wanted = manifest[str(prefix)]
                    if (info.st_dev, info.st_ino) != (wanted["device"], wanted["inode"]):
                        raise c.CompletionError("record parent identity changed")
                wanted = manifest[name]
                info = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (wanted["device"], wanted["inode"]):
                    raise c.CompletionError("task record identity changed")
                if wanted.get("directory"):
                    os.rmdir(relative.name, dir_fd=parent_fd)
                else:
                    if c.sha(c.raw_file(str(root / relative))) != wanted["sha256"]:
                        raise c.CompletionError("task record bytes changed")
                    os.unlink(relative.name, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
    finally:
        os.close(root_fd)
    if c.record_directory(str(root)) != record:
        raise c.CompletionError("record root identity changed")
    root.rmdir()
    if os.path.lexists(root):
        raise c.CompletionError("record root remains")


def finish_cleanup(filename, state, before):
    cleanup = state["cleanup"]
    c.fields(cleanup, ("record_root", "manifest", "request_digest", "answer"))
    answer = cleanup["answer"]
    if answer.get("complete") is not True or (answer.get("task_id"), answer.get("scope_sha256")) != (state["task_id"], state["scope"]["sha256"]):
        raise c.CompletionError("cleanup has no successful bound observation")
    if c.sha(c.raw_file(str(filename))) != before:
        raise c.CompletionError("pointer changed")
    remove_records(cleanup["record_root"], cleanup["manifest"])
    if c.sha(c.raw_file(str(filename))) != before:
        raise c.CompletionError("pointer changed")
    filename.unlink()
    return {**answer, "binding_released": True, "records_removed": cleanup["record_root"]["path"]}


def close_completed(filename, state, before, scope, answer):
    if c.sha(c.raw_file(str(filename))) != before:
        raise c.CompletionError("pointer changed")
    if "record_root" not in scope:
        filename.unlink()
        return {**answer, "binding_released": True}
    scope = c.filesystem_scope(state["scope"], scope, state)
    record = scope["record_root"]
    for value in (state["scope"]["path"], state["review"]["path"], state["result_path"], state["final_review"]["path"]):
        if not c.path(value).is_relative_to(c.path(record["path"])):
            raise c.CompletionError("completion inputs must be inside the task record root")
    manifest = c.record_inventory(record)
    reviewed = c.read_reference(state["final_review"])
    review_name = str(c.path(state["final_review"]["path"]).relative_to(c.path(record["path"])))
    if manifest[review_name]["sha256"] != state["final_review"]["sha256"]:
        raise c.CompletionError("final review changed after inspection")
    if c.reviewed_record_inventory(record, state["final_review"]["path"], inventory=manifest) != reviewed["record_inventory_sha256"]:
        raise c.CompletionError("task records changed after final review")
    state["cleanup"] = {"record_root": record, "manifest": manifest,
                        "request_digest": scope["request_digest"], "answer": answer}
    before = replace(filename, state, before)["sha256"]
    return finish_cleanup(filename, state, before)


def desktop_workspace(cwd, desktop):
    if cwd.is_relative_to(desktop):
        return True
    try:
        common = c.git(str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()
        return c.path(common).resolve().is_relative_to(desktop)
    except (c.CompletionError, OSError, ValueError):
        return False


def hook(payload, root=ROOT):
    event = payload.get("hook_event_name")
    if event not in {"SessionStart", "UserPromptSubmit", "Stop"}: return {}
    filename = pointer(root, c.text(payload.get("session_id")))
    cwd = c.path(payload.get("cwd"))
    desktop = Path(__file__).resolve().parents[2]
    # Bound sessions and new worktrees of Desktop repositories keep the same entry.
    if not filename.exists() and not desktop_workspace(cwd, desktop): return {}
    if event != "Stop":
        context = GUIDANCE + " 현재 session_id=" + payload["session_id"]
        if filename.exists():
            try:
                state, _ = load_state(filename)
                goal = task_context(state, filename, payload["session_id"])
                context += ("\n기존 연결 작업=" + json.dumps(goal, ensure_ascii=False) +
                            "\n이 상태·대기는 위 작업에 한정됩니다. 현재 사용자 지시·우선순위·승인 철회를 따르되, "
                            "독립 작업까지 차단하거나 과거 질문의 답을 이 작업의 산출물로 대신하지 마세요. "
                            "complete/unbind에는 현재 목표의 --task-id와 --request-digest를 명시하세요.")
            except (c.CompletionError, OSError, ValueError, TypeError, KeyError) as exc:
                context += "\n기존 목표 연결 미확인: " + str(exc)[:1000] + ". 이 오류로 다른 독립 작업의 승인 상태를 추정하지 마세요."
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
    if not filename.exists():
        return {"systemMessage": "정리 완료 검증에 연결되지 않은 세션입니다. 정리 의도가 있으면 먼저 arm/bind 하세요. 미연결 상태는 완료 보증이 아닙니다."}
    try:
        state, _ = load_state(filename)
        if state["session_id"] != payload["session_id"]:
            raise c.CompletionError("session binding changed")
        if "cleanup" in state:
            raise c.CompletionError("검사 통과 후 임시 기록 삭제가 미완료입니다. 현재 task/request와 pointer SHA로 complete/unbind를 재실행하세요")
        if state["outcome"] in {"waiting", "blocked", "cancelled"}:
            c.reference(state.get("status_basis"))
            return {"systemMessage": "정리 미완료: " + state["outcome"] + ". 작업 종료 조건이 남은 상태입니다. 파일 손상 여부를 이 상태만으로 판단하지 마세요. 남은 범위·이유는 상태 근거에 보존되어 있습니다. 완료로 보고하지 마세요."}
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
    for name in ("arm", "bind", "reserve", "attach", "finalize", "complete", "outcome", "inspect", "unbind", "release", "recover-owner", "preserve-source", "recover-filesystem"):
        item = subs.add_parser(name)
        item.add_argument("--session", required=True)
        if name == "arm": item.add_argument("--task-id", required=True)
        else: item.add_argument("--expected-sha256", required=True, help="current pointer SHA; CAS under a local lock")
        if name in {"complete", "unbind"}:
            item.add_argument("--task-id", required=True, help="current user goal identity, not a task inferred from an old session")
            item.add_argument("--request-digest", required=True, help="current goal's frozen request digest; completion binding does not supply caller intent")
        if name == "bind":
            item.add_argument("--scope", required=True); item.add_argument("--scope-sha256", required=True)
            item.add_argument("--review", required=True); item.add_argument("--review-sha256", required=True)
        if name in {"reserve", "attach"}:
            item.add_argument("--id", required=True); item.add_argument("--basis", required=True); item.add_argument("--basis-sha256", required=True)
        if name == "attach": item.add_argument("--resource", required=True, help="JSON with fixed kind,target; actual tool handle and owner record")
        if name in {"finalize", "complete"}:
            item.add_argument("--result", required=True); item.add_argument("--review", required=True); item.add_argument("--review-sha256", required=True)
        if name in {"recover-owner", "preserve-source", "recover-filesystem"}:
            item.add_argument("--evidence", required=True); item.add_argument("--evidence-sha256", required=True)
            item.add_argument("--review", required=True); item.add_argument("--review-sha256", required=True)
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
                    if "cleanup" in state:
                        if args.command not in {"complete", "unbind"}:
                            raise c.CompletionError("only complete/unbind can resume pending record cleanup")
                        if (args.task_id, args.request_digest) != (state["task_id"], state["cleanup"]["request_digest"]):
                            raise c.CompletionError("current task/request differs from pending record cleanup")
                        answer = finish_cleanup(filename, state, before)
                        print(c.canonical(answer).decode()); return 0
                    if args.command in {"complete", "unbind"}:
                        goal = task_context(state, filename, args.session)
                        if (c.text(args.task_id), c.digest(args.request_digest)) != (goal["task_id"], goal.get("request_digest")):
                            raise c.CompletionError("current task/request differs from the frozen binding; preserve it and review the current goal")
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
                            if resource["kind"] in {"ref_absent", "remote_ref_absent"}:
                                c.validate_ref_observer(resource["kind"], resource["target"].get("ref"))
                            if resource["kind"] == "remote_ref_absent":
                                target = resource["target"]
                                target["remote_binding"] = c.remote_identity(target["repository"], target["remote"])
                            found[0].update(resource, basis=basis)
                    elif args.command == "recover-filesystem":
                        if state["outcome"] != "waiting" or state["scope"] is None or any(k in state for k in ("filesystem_recovery", "source_preservation")):
                            raise c.CompletionError("filesystem recovery requires a waiting task without prior filesystem/source recovery")
                        c.reference(state.get("status_basis"))
                        binding = {"evidence": {"path": args.evidence, "sha256": args.evidence_sha256},
                                   "review": {"path": args.review, "sha256": args.review_sha256}}
                        evidence = c.read_reference(binding["evidence"])
                        if evidence["previous_state_sha256"] != before:
                            raise c.CompletionError("filesystem recovery is not for the exact previous pointer")
                        proposed = {**state, "filesystem_recovery": binding}
                        scope = c.filesystem_scope(state["scope"], c.read_reference(state["scope"]), proposed)
                        recovery = c.owner_recovery(state["scope"], scope, state["owner_recovery"]) if "owner_recovery" in state else None
                        c.validate_scope(scope, recovery)
                        state["filesystem_recovery"] = binding
                    elif args.command == "recover-owner":
                        if state["outcome"] != "waiting" or state["scope"] is None:
                            raise c.CompletionError("owner recovery requires the existing waiting binding")
                        c.reference(state.get("status_basis"))
                        binding = {"evidence": {"path": args.evidence, "sha256": args.evidence_sha256},
                                   "review": {"path": args.review, "sha256": args.review_sha256}}
                        scope = c.filesystem_scope(state["scope"], c.read_reference(state["scope"]), state)
                        evidence = c.read_reference(binding["evidence"])
                        if evidence.get("previous") != state.get("owner_recovery"):
                            raise c.CompletionError("owner recovery must preserve the exact previous evidence and review")
                        if (scope["task_id"], scope["session_id"], scope["state_path"]) != (state["task_id"], args.session, str(filename)):
                            raise c.CompletionError("scope task/session/resource pointer differs")
                        recovery = c.owner_recovery(state["scope"], scope, binding)
                        c.validate_scope(scope, recovery)
                        state["owner_recovery"] = binding
                    elif args.command == "preserve-source":
                        if state["outcome"] != "waiting" or state["scope"] is None or "source_preservation" in state:
                            raise c.CompletionError("source preservation requires the existing waiting binding and cannot replace a preservation")
                        c.reference(state.get("status_basis"))
                        binding = {"evidence": {"path": args.evidence, "sha256": args.evidence_sha256},
                                   "review": {"path": args.review, "sha256": args.review_sha256}}
                        scope = c.filesystem_scope(state["scope"], c.read_reference(state["scope"]), state)
                        if (scope["task_id"], scope["session_id"], scope["state_path"]) != (state["task_id"], args.session, str(filename)):
                            raise c.CompletionError("scope task/session/resource pointer differs")
                        if c.read_reference(binding["evidence"])["previous_state_sha256"] != before:
                            raise c.CompletionError("source preservation is not for the exact previous pointer")
                        recovery = c.owner_recovery(state["scope"], scope, state["owner_recovery"]) if "owner_recovery" in state else None
                        c.validate_scope(scope, recovery)
                        c.source_preservation(state["scope"], scope, binding)
                        state["source_preservation"] = binding
                    elif args.command in {"finalize", "complete"}:
                        state.update(result_path=str(c.path(args.result)), final_review=c.reference({"path": args.review, "sha256": args.review_sha256}))
                        if args.command == "complete":
                            # Preserve reviewed inputs for recovery before observing. A failed
                            # inspection leaves this CAS-updated pointer bound for retry.
                            before = replace(filename, state, before)["sha256"]
                            scope = c.read_reference(state["scope"])
                            answer = c.check_scope(state["scope"], state["result_path"], task_id=state["task_id"],
                                                   request_digest=args.request_digest, workspace=scope["environment"]["workspace"], review_ref=state["review"], report=True)
                            if answer["complete"]:
                                answer = close_completed(filename, state, before, scope, answer)
                            answer["binding_released"] = answer["complete"]
                            print(c.canonical(answer).decode()); return 0 if answer["complete"] else 2
                    elif args.command == "outcome":
                        state.update(outcome=args.status, status_basis=c.reference({"path": args.basis, "sha256": args.basis_sha256}))
                    else:
                        scope = c.read_reference(state["scope"])
                        answer = c.check_scope(state["scope"], state["result_path"], task_id=state["task_id"], workspace=scope["environment"]["workspace"], review_ref=state["review"], stopped=args.command == "release")
                        if args.command in {"unbind", "release"}:
                            if not (answer["stopped"] if args.command == "release" else answer["complete"]): raise c.CompletionError("unfinished task remains bound")
                            if args.command == "unbind":
                                answer = close_completed(filename, state, before, scope, answer)
                            else:
                                if c.sha(c.raw_file(str(filename))) != before: raise c.CompletionError("pointer changed")
                                filename.unlink()
                        print(c.canonical(answer).decode()); return 0 if answer["complete"] or answer["stopped"] else 2
                    answer = replace(filename, state, before)
        print(c.canonical(answer).decode()); return 0
    except (c.CompletionError, OSError, ValueError, TypeError, KeyError) as exc:
        print(c.canonical({"complete": False, "error": str(exc)}).decode()); return 2


if __name__ == "__main__": sys.exit(main())
