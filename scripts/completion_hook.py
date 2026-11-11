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
import sqlite3
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
            "complete/unbind/release 성공 시 검토된 임시 기록 폴더까지 제거하며 완료 receipt 파일을 다시 만들지 마세요. 완료는 현재 관측으로 확인하세요.")


def pointer(root, session):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", session):
        raise c.CompletionError("invalid session identity")
    return c.path(str(root)) / (session + ".json")


def protect_records(state, filename, resources, *, observing=False):
    scope = None
    if state.get("scope"):
        scope = c.filesystem_scope(state["scope"], c.read_control(state["scope"]), state)
    c.protect_bound_records(scope, state, filename, resources, observing=observing)


def measurement_store(explicit_root=None):
    """Optional installed observer; completion authority stays in this module."""
    operations = Path(__file__).resolve().parents[2] / "operations"
    default = operations / ".state" / "llm-work"
    if explicit_root is None and not (default / "enabled.json").is_file():
        return None
    source = operations / "llm_work.py"
    spec = importlib.util.spec_from_file_location("operations_llm_work", source)
    module = importlib.util.module_from_spec(spec)
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(operations))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module, module.WorkStore(explicit_root or default)


def measurement_event(payload, explicit_root=None):
    try:
        bound = measurement_store(explicit_root)
        if bound:
            module, store = bound
            store.reconcile_cleanup()
            return module.consume_hook(payload, store)
    except (OSError, ValueError, TypeError, KeyError, ImportError, sqlite3.Error) as exc:
        print(json.dumps({"measurement": "pending", "error_type": type(exc).__name__}), file=sys.stderr)
    return {}


def measurement_cleanup(state, answer, *, binding_path, preparing=False, scope=None):
    """A missing observer cannot change a successful cleanup into task failure."""
    bound = None
    try:
        if Path(binding_path).parent != ROOT:
            return
        bound = measurement_store()
        if bound:
            _, store = bound
            if preparing:
                store.remember_cleanup(state["task_id"], {**answer,
                    'session_id':state['session_id'],
                    "binding_path": str(binding_path),
                    "record_root": scope.get("record_root", {}).get("path")})
            else:
                return store.finish_cleanup(state["task_id"], answer)
    except (OSError, ValueError, TypeError, KeyError, ImportError, sqlite3.Error) as exc:
        if preparing and bound and isinstance(exc, getattr(bound[0], 'RepairIncomplete', ())):
            raise c.CompletionError(str(exc)) from exc
        print(json.dumps({"measurement": "pending", "error_type": type(exc).__name__}), file=sys.stderr)


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
    c.fields(value, c.STATE_FIELDS, c.STATE_OPTIONAL)
    if value["intent"] != "closure": raise c.CompletionError("invalid closure intent")
    if "scope_history" in value or "reservation_resolutions" in value:
        with c.stored_controls(value, filename):
            c.scope_history(value["scope"], value)
            c.reservation_resolutions(c.read_control(value["scope"]) if value["scope"] else None, value)
    return value, c.sha(raw)


def task_context(state, filename, session):
    with c.stored_controls(state, filename):
        return _task_context(state, filename, session)


def _task_context(state, filename, session):
    """Read the bound goal, without executing its observations or inferring a new one."""
    if state["session_id"] != session:
        raise c.CompletionError("session binding changed")
    context = {"task_id": state["task_id"], "outcome": state["outcome"], "scope": state["scope"]}
    if "cleanup" in state:
        context.update(request_digest=state["cleanup"]["request_digest"], record_cleanup_pending=True)
        return context
    if state["scope"] is not None:
        scope = c.read_control(state["scope"])
        c.validate_state_identity(scope, state, filename)
        context.update(request=c.control_reference(scope["request"]), request_digest=c.digest(scope["request_digest"]),
                       obligations=[check["id"] for check in scope["checks"]])
        preview = c.control_bytes(scope["request"]["path"], scope["request"]["sha256"]).decode("utf-8", errors="replace")
        context.update(state_path=str(filename), request_preview=preview[:4096], request_preview_truncated=len(preview) > 4096)
    if state["outcome"] in {"waiting", "blocked", "cancelled"}:
        context["status_basis"] = c.control_reference(state["status_basis"])
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
                    if c.artifact_sha(root / relative) != wanted["sha256"]:
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
    if ((answer.get("complete") is True) == (answer.get("stopped") is True)
            or (answer.get("task_id"), answer.get("scope_sha256")) != (state["task_id"], state["scope"]["sha256"])):
        raise c.CompletionError("cleanup has no successful bound observation")
    if answer.get("stopped"):
        c.reference(state.get("status_basis"))
    if c.sha(c.raw_file(str(filename))) != before:
        raise c.CompletionError("pointer changed")
    protect_records(state, filename, state["resources"], observing=True)
    remove_records(cleanup["record_root"], cleanup["manifest"])
    if c.sha(c.raw_file(str(filename))) != before:
        raise c.CompletionError("pointer changed")
    filename.unlink()
    result = {**answer, "binding_released": True, "records_removed": cleanup["record_root"]["path"]}
    observed = measurement_cleanup(state, result, binding_path=filename)
    if observed: result["work_measurement"] = observed
    return result


def close_checked(filename, state, before, scope, answer, measurement_turns=None):
    with c.stored_controls(state, filename):
        return _close_checked(filename, state, before, scope, answer, measurement_turns)


def _close_checked(filename, state, before, scope, answer, measurement_turns=None):
    if measurement_turns:answer={**answer,'measurement_turns':list(dict.fromkeys(c.text(t) for t in measurement_turns))}
    if c.sha(c.raw_file(str(filename))) != before:
        raise c.CompletionError("pointer changed")
    if "record_root" not in scope:
        measurement_cleanup(state, answer, binding_path=filename, preparing=True, scope=scope)
        filename.unlink()
        result = {**answer, "binding_released": True}
        observed = measurement_cleanup(state, result, binding_path=filename)
        if observed: result["work_measurement"] = observed
        return result
    scope = c.filesystem_scope(state["scope"], scope, state)
    c.protect_bound_records(scope, state, filename, state["resources"], observing=True)
    record = scope["record_root"]
    if answer.get("stopped") and c.path(state["status_basis"]["path"]).is_relative_to(c.path(record["path"])):
        raise c.CompletionError("stopped disposition must remain in the owner records outside task records")
    for value in (state["scope"]["path"], state["review"]["path"], state["result_path"], state["final_review"]["path"]):
        if not c.path(value).is_relative_to(c.path(record["path"])):
            raise c.CompletionError("completion inputs must be inside the task record root")
    manifest = c.record_inventory(record)
    reviewed = c.read_control(state["final_review"])
    review_name = str(c.path(state["final_review"]["path"]).relative_to(c.path(record["path"])))
    if review_name in manifest and manifest[review_name]["sha256"] != state["final_review"]["sha256"]:
        raise c.CompletionError("final review changed after inspection")
    if c.checked_record_inventory(scope, state, state["final_review"]["path"]) != reviewed["record_inventory_sha256"]:
        raise c.CompletionError("task records changed after final review")
    measurement_cleanup(state, answer, binding_path=filename, preparing=True, scope=scope)
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


def hook(payload, root=ROOT, measurement_root=None):
    filename = pointer(root, c.text(payload.get("session_id")))
    if filename.exists():
        try:
            state, _ = load_state(filename)
            with c.stored_controls(state, filename):
                return _hook(payload, root, measurement_root)
        except (c.CompletionError, OSError, ValueError, TypeError, KeyError):
            # The normal handler emits scoped guidance or the bounded Stop error.
            return _hook(payload, root, measurement_root)
    return _hook(payload, root, measurement_root)


def _hook(payload, root=ROOT, measurement_root=None):
    measured = measurement_event(payload, measurement_root) if measurement_root is not None or root == ROOT else {}
    event = payload.get("hook_event_name")
    if event not in {"SessionStart", "UserPromptSubmit", "Stop"}: return measured
    filename = pointer(root, c.text(payload.get("session_id")))
    cwd = c.path(payload.get("cwd"))
    desktop = Path(__file__).resolve().parents[2]
    # Bound sessions and new worktrees of Desktop repositories keep the same entry.
    if not filename.exists() and not desktop_workspace(cwd, desktop): return measured
    if event != "Stop":
        context = GUIDANCE + " 현재 session_id=" + payload["session_id"]
        context += "\n" + measured.get("hookSpecificOutput", {}).get("additionalContext", "")
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
            command = "release" if state["cleanup"].get("answer", {}).get("stopped") else "complete/unbind"
            raise c.CompletionError("검사 통과 후 임시 기록 삭제가 미완료입니다. 현재 task/request와 pointer SHA로 " + command + "를 재실행하세요")
        if state["outcome"] in {"waiting", "blocked", "cancelled"}:
            c.control_reference(state.get("status_basis"))
            return {"systemMessage": "정리 미완료: " + state["outcome"] + ". 작업 종료 조건이 남은 상태입니다. 파일 손상 여부를 이 상태만으로 판단하지 마세요. 남은 범위·이유는 상태 근거에 보존되어 있습니다. 완료로 보고하지 마세요."}
        if state["outcome"] != "working" or state["scope"] is None:
            raise c.CompletionError("armed task has no frozen reviewed scope")
        scope = c.read_control(state["scope"])
        c.validate_state_identity(scope, state, filename)
        if not state.get('result_path') or not state.get('final_review'):
            raise c.CompletionError('required work observations and independent final review have not been finalized')
        answer = c.check_scope(state["scope"], state["result_path"], task_id=state["task_id"], workspace=str(cwd), review_ref=state["review"], state_path=str(filename))
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


def adopt_session(filename, state, before, args):
    """Explicitly transfer a direct task; never infer work from another session."""
    if args.from_session == args.session or state.get("execution_owner") != "direct":
        raise c.CompletionError("use a distinct session and the registered execution owner's handoff")
    scope = c.read_control(state["scope"])
    if (args.task_id, args.request_digest) != (scope["task_id"], scope["request_digest"]):
        raise c.CompletionError("handoff task/request differs")
    target = pointer(filename.parent, args.session)
    if os.path.lexists(target):
        raise c.CompletionError("target session already has a task")
    basis = c.control_reference({"path": args.basis, "sha256": args.basis_sha256})
    pending = (state.get("handoff", {}).get("from_session") == args.from_session
               and state["session_id"] == args.session)
    if not pending and state["session_id"] != args.from_session:
        raise c.CompletionError("handoff source session differs")
    c.validate_state_identity(scope, {**state, "session_id": args.from_session}, filename)
    proposed = {**state, "session_id": args.session,
                "origin": state.get("origin", {"session_id": scope["session_id"], "state_path": scope["state_path"]}),
                "handoff": {"from_session": args.from_session, "to_session": args.session, "basis": basis}}
    c.capture_controls(proposed, location=target)
    with c.stored_controls(proposed, target):
        protect_records(proposed, target, proposed["resources"])
    # Persist the intent first. An interrupted rename leaves the exact source
    # retryable, without reconstructing its scope or resource inventory.
    replace(filename, proposed, before)
    os.rename(filename, target)
    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return {"path": str(target), "sha256": c.sha(c.raw_file(str(target))),
            "task_id": state["task_id"], "origin_preserved": True}


def extend_scope(state, before, filename, args):
    if state["scope"] is None or state["outcome"] != "waiting":
        raise c.CompletionError("scope extension requires the existing waiting binding")
    if any(name in state for name in ("owner_recovery", "source_preservation", "filesystem_recovery")):
        raise c.CompletionError("resolve the existing recovery boundary before extending scope")
    if "cleanup" in state or "record_manifest" in state:
        raise c.CompletionError("reviewed record cleanup must finish before extending scope")
    c.control_reference(state.get("status_basis"))
    old_scope = c.read_control(state["scope"])
    new_ref = c.control_reference({"path": args.scope, "sha256": args.scope_sha256})
    new_scope = c.read_control(new_ref)
    c.additive_scope(old_scope, new_scope)
    c.validate_state_identity(new_scope, state, filename)
    c.validate_scope(new_scope)
    normal = c.control_reference({"path": args.review, "sha256": args.review_sha256})
    if c.read_control(normal) != {"scope_sha256": new_ref["sha256"], "status": "No Findings"}:
        raise c.CompletionError("scope review does not cover the extended scope")
    # Capture the old final verdict before invalidating it for the added work.
    c.capture_controls(state, location=filename)
    step = {"scope": state["scope"], "review": state["review"],
            "authorization": c.control_reference({"path": args.authorization, "sha256": args.authorization_sha256}),
            "delta_review": c.control_reference({"path": args.delta_review, "sha256": args.delta_review_sha256}),
            "previous_state_sha256": before,
            "result": ({"path": state["result_path"], "sha256": c.sha(c.control_bytes(state["result_path"]))}
                       if state["result_path"] else None), "final_review": state["final_review"]}
    proposed = {**state, "scope": new_ref, "review": normal,
                "scope_history": [*state.get("scope_history", []), step],
                "result_path": None, "final_review": None}
    c.scope_history(new_ref, proposed)
    c.protect_bound_records(new_scope, proposed, filename, proposed["resources"])
    return proposed


def resolve_reservation(state, before, args):
    if state["scope"] is None:
        raise c.CompletionError("reservation resolution requires a frozen scope")
    if any(row["id"] == args.id for row in state.get("reservation_resolutions", [])):
        raise c.CompletionError("reservation already resolved; preserve its original history")
    row = {"id": c.text(args.id), "evidence": {"path": args.evidence, "sha256": args.evidence_sha256},
           "review": {"path": args.review, "sha256": args.review_sha256}}
    evidence = c.read_control(row["evidence"])
    if evidence["previous_state_sha256"] != before:
        raise c.CompletionError("resolution is not for the exact previous pointer")
    proposed = {**state, "reservation_resolutions": [*state.get("reservation_resolutions", []), row]}
    c.reservation_resolutions(c.read_control(state["scope"]), proposed)
    return proposed


def publication_admissions(state, accepted, *, final):
    """Validate allowed deltas against the disk CAS state before capturing inputs."""
    c.fields(state, c.STATE_FIELDS, c.STATE_OPTIONAL)
    if state["intent"] != "closure":
        raise c.CompletionError("invalid closure intent")
    if not isinstance(state["resources"], list):
        raise c.CompletionError("expected accumulated resource list")
    if "cleanup" in state or accepted is not None and "cleanup" in accepted:
        raise c.CompletionError("pending record cleanup must use complete/unbind/release")
    if final and (accepted is None or accepted["scope"] is None):
        raise c.CompletionError("final publication requires an existing frozen scope")
    previous_rows = accepted.get("control_records", []) if accepted else []
    rows = state.get("control_records", [])
    if (not isinstance(previous_rows, list) or not isinstance(rows, list)
            or ("control_records" in state) != (accepted is not None and "control_records" in accepted)
            or c.canonical(rows) != c.canonical(previous_rows)):
        raise c.CompletionError("publication must preserve accepted controls before capture")
    if (("record_manifest" in state) != (accepted is not None and "record_manifest" in accepted)
            or c.canonical(state.get("record_manifest")) != c.canonical(accepted.get("record_manifest") if accepted else None)):
        raise c.CompletionError("publication must preserve the prior record manifest before capture")
    if accepted is None:
        # A fresh producer can create an initial state, not import historical authority.
        allowed = set(c.STATE_FIELDS) | {"execution_owner"}
        if (set(state) - allowed or rows or state["outcome"] != "working"
                or state["resources"] or state["result_path"] is not None or state["final_review"] is not None):
            raise c.CompletionError("initial publication cannot claim history or final inputs")
        if (state["scope"] is None) != (state["review"] is None):
            raise c.CompletionError("initial scope and review must be paired")
        if state["scope"] is not None:
            c.fields(state["scope"], ("path", "sha256")); c.fields(state["review"], ("path", "sha256"))
        return state["resources"]
    # Resource updates and reviewed final inputs are the only generic transitions.
    # bind/outcome/adopt/recover and cleanup retain their dedicated owner boundaries.
    allowed = {"resources"} | ({"result_path", "final_review"} if final else set())
    controlled = {"control_records", "record_manifest"}
    changed = {name for name in (state.keys() | accepted.keys()) - controlled
               if name not in state or name not in accepted
               or c.canonical(state[name]) != c.canonical(accepted[name])}
    if changed - allowed:
        raise c.CompletionError("publication must preserve owner state: " + ", ".join(sorted(changed - allowed)))
    if final and c.canonical(accepted["resources"]) != c.canonical(state["resources"]):
        raise c.CompletionError("final publication must preserve the accepted accumulated resources")
    if final and (state["result_path"] is None or state["final_review"] is None):
        raise c.CompletionError("final publication requires the result/review pair")
    for resource in state["resources"]:
        c.fields(resource, ("id", "kind", "target", "basis"))
        c.text(resource["id"])
    previous = {resource["id"]:resource for resource in accepted["resources"]}
    current = {resource["id"]:resource for resource in state["resources"]}
    resolved = {row["id"] for row in accepted.get("reservation_resolutions", [])}
    if len(previous) != len(accepted["resources"]) or len(current) != len(state["resources"]):
        raise c.CompletionError("duplicate accumulated resource identity")
    if list(current)[:len(previous)] != list(previous):
        raise c.CompletionError("publication must preserve accumulated resource order")
    for key, old in previous.items():
        new = current.get(key)
        if c.canonical(new) != c.canonical(old):
            if key in resolved:
                raise c.CompletionError("publication must preserve resolved original reservations")
            attached = (old["kind"] == "reserved" and new and new["kind"] in c.CLEANUP_KINDS
                        and {k:v for k,v in new.items() if k not in {"kind", "target"}}
                        == {k:v for k,v in old.items() if k not in {"kind", "target"}})
            if not attached:
                raise c.CompletionError("publication must preserve accumulated resource obligations")
    if any(resource["kind"] != "reserved" or resource["target"] != {}
           for resource in state["resources"][len(previous):]):
        raise c.CompletionError("publication must reserve new resources before attaching them")
    return state["resources"] if final else [resource for resource in state["resources"]
        if c.canonical(previous.get(resource["id"])) != c.canonical(resource)]


def publish(filename, state, before, *, final=False):
    """Shared publication boundary; the disk state owns authority and history."""
    accepted = None
    if os.path.lexists(filename):
        accepted, digest = load_state(filename)
        if digest != before:
            raise c.CompletionError("pointer changed; reread before publication")
    elif before is not None:
        raise c.CompletionError("pointer disappeared")
    admitted = publication_admissions(state, accepted, final=final)
    with c.stored_controls(state, filename):
        protect_records(state, filename, admitted, observing=final)
    c.capture_controls(state, final=final, location=filename, observing=final)
    previous_rows = accepted.get("control_records", []) if accepted else []
    if c.canonical(state.get("control_records", [])[:len(previous_rows)]) != c.canonical(previous_rows):
        raise c.CompletionError("capture changed the accepted control prefix")
    with c.stored_controls(state, filename):
        protect_records(state, filename, admitted, observing=final)
    return replace(filename, state, before)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bindings", default=str(ROOT), help="private per-session pointer directory; host installation uses the default")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("hook")
    config = subs.add_parser("configure")
    config.add_argument("--existing", required=True); config.add_argument("--output", required=True)
    for name in ("arm", "bind", "extend", "reserve", "attach", "resolve-reservation", "finalize", "complete", "outcome", "inspect", "unbind", "release", "recover-owner", "preserve-source", "recover-filesystem", "adopt"):
        item = subs.add_parser(name)
        item.add_argument("--session", required=True)
        if name == "arm": item.add_argument("--task-id", required=True)
        else: item.add_argument("--expected-sha256", required=True, help="current pointer SHA; CAS under a local lock")
        if name in {"complete", "unbind", "release", "adopt"}:
            item.add_argument('--work-turn',action='append',help='Current explicit goal turn(s) to attach to automatic cleanup measurement; never infer them from an old binding')
            item.add_argument("--task-id", required=True, help="current user goal identity, not a task inferred from an old session")
            item.add_argument("--request-digest", required=True, help="current goal's frozen request digest; completion binding does not supply caller intent")
        if name == "adopt": item.add_argument("--from-session", required=True)
        if name in {"bind", "extend"}:
            item.add_argument("--scope", required=True); item.add_argument("--scope-sha256", required=True)
            item.add_argument("--review", required=True); item.add_argument("--review-sha256", required=True)
        if name == "extend":
            item.add_argument("--authorization", required=True); item.add_argument("--authorization-sha256", required=True)
            item.add_argument("--delta-review", required=True); item.add_argument("--delta-review-sha256", required=True)
        if name in {"reserve", "attach", "adopt"}:
            if name != "adopt": item.add_argument("--id", required=True)
            item.add_argument("--basis", required=True); item.add_argument("--basis-sha256", required=True)
        if name == "attach": item.add_argument("--resource", required=True, help="JSON with fixed kind,target; actual tool handle and owner record")
        if name == "resolve-reservation": item.add_argument("--id", required=True)
        if name in {"finalize", "complete"}:
            item.add_argument("--result"); item.add_argument("--review"); item.add_argument("--review-sha256")
            item.add_argument("--inputs", choices=("-",), help="read {result,review} JSON from stdin without staging files")
        if name in {"recover-owner", "preserve-source", "recover-filesystem", "resolve-reservation"}:
            item.add_argument("--evidence", required=True); item.add_argument("--evidence-sha256", required=True)
            item.add_argument("--review", required=True); item.add_argument("--review-sha256", required=True)
        if name == "recover-filesystem":
            item.add_argument("--owner-recovery", help="Exact reviewed companion {evidence,review} binding when owner bytes also changed")
            item.add_argument("--owner-recovery-sha256")
        if name == "outcome":
            item.add_argument("--status", required=True, choices=("working", "waiting", "blocked", "cancelled"))
            item.add_argument("--basis", required=True); item.add_argument("--basis-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "hook":
            answer = hook(c.decode(sys.stdin.buffer.read()), Path(args.bindings))
        elif args.command == "configure":
            answer = c.write_new(args.output, configuration(c.decode(c.raw_file(args.existing))))
        else:
            filename = pointer(Path(args.bindings), args.from_session if args.command == "adopt" else args.session)
            with locked(filename.parent):
                if args.command == "arm":
                    if os.path.lexists(filename): raise c.CompletionError("session already armed; reuse its current pointer")
                    state = {"task_id": c.text(args.task_id), "session_id": args.session, "intent": "closure",
                             "scope": None, "review": None, "final_review": None, "result_path": None,
                             "resources": [], "outcome": "working", "execution_owner": "direct"}
                    answer = replace(filename, state, None)
                else:
                    state, before = load_state(filename)
                    if before != c.digest(args.expected_sha256) or (args.command != "adopt" and state["session_id"] != args.session):
                        raise c.CompletionError("stale pointer or wrong session")
                    with c.stored_controls(state, filename):
                        if args.command == "adopt":
                            answer = adopt_session(filename, state, before, args)
                            print(c.canonical(answer).decode()); return 0
                        if "cleanup" in state:
                            if args.command not in {"complete", "unbind", "release"}:
                                raise c.CompletionError("only complete/unbind/release can resume pending record cleanup")
                            if (args.task_id, args.request_digest) != (state["task_id"], state["cleanup"]["request_digest"]):
                                raise c.CompletionError("current task/request differs from pending record cleanup")
                            if (args.command == "release") != (state["cleanup"]["answer"].get("stopped") is True):
                                raise c.CompletionError("cleanup disposition differs from the checked outcome")
                            answer = finish_cleanup(filename, state, before)
                            print(c.canonical(answer).decode()); return 0
                        if args.command in {"complete", "unbind", "release"}:
                            goal = task_context(state, filename, args.session)
                            if (c.text(args.task_id), c.digest(args.request_digest)) != (goal["task_id"], goal.get("request_digest")):
                                raise c.CompletionError("current task/request differs from the frozen binding; preserve it and review the current goal")
                        if args.command == "extend":
                            state = extend_scope(state, before, filename, args)
                        elif args.command == "bind":
                            if state["scope"] is not None: raise c.CompletionError("scope already frozen; recovery must not replace original obligations")
                            scope_ref = c.control_reference({"path": args.scope, "sha256": args.scope_sha256})
                            review_ref = c.control_reference({"path": args.review, "sha256": args.review_sha256})
                            scope = c.read_control(scope_ref); c.validate_scope(scope)
                            c.validate_state_identity(scope, state, filename)
                            if c.read_control(review_ref) != {"scope_sha256": args.scope_sha256, "status": "No Findings"}:
                                raise c.CompletionError("review is not for this exact scope")
                            proposed = {**state, "scope": scope_ref, "review": review_ref}
                            protect_records(proposed, filename, state["resources"])
                            state.update(proposed)
                        elif args.command == "resolve-reservation":
                            state = resolve_reservation(state, before, args)
                        elif args.command in {"reserve", "attach"}:
                            basis = c.control_reference({"path": args.basis, "sha256": args.basis_sha256})
                            found = [r for r in state["resources"] if r["id"] == args.id]
                            if args.command == "reserve":
                                if found: raise c.CompletionError("duplicate resource reservation")
                                state["resources"].append({"id": c.text(args.id), "kind": "reserved", "target": {}, "basis": basis})
                            else:
                                if any(row["id"] == args.id for row in state.get("reservation_resolutions", [])):
                                    raise c.CompletionError("resolved reservation cannot later attach a resource")
                                if len(found) != 1 or found[0]["kind"] != "reserved": raise c.CompletionError("resource must be reserved before creation")
                                resource = c.decode(sys.stdin.buffer.read() if args.resource == "-"
                                                    else c.raw_file(args.resource))
                                c.fields(resource, ("kind", "target"))
                                if resource["kind"] not in {"absent", "ref_absent", "remote_ref_absent", "worktree_absent", "process_absent"} or not isinstance(resource["target"], dict):
                                    raise c.CompletionError("pointer covers temporary resources only; retained outputs/runtime need fixed owner scope checks")
                                if resource["kind"] in {"ref_absent", "remote_ref_absent"}:
                                    c.validate_ref_observer(resource["kind"], resource["target"].get("ref"))
                                if resource["kind"] == "remote_ref_absent":
                                    target = resource["target"]
                                    target["remote_binding"] = c.remote_identity(target["repository"], target["remote"])
                                protect_records(state, filename, [resource])
                                found[0].update(resource, basis=basis)
                        elif args.command == "recover-filesystem":
                            if state["outcome"] != "waiting" or state["scope"] is None or "filesystem_recovery" in state:
                                raise c.CompletionError("filesystem recovery requires a waiting task without prior filesystem recovery")
                            c.control_reference(state.get("status_basis"))
                            binding = {"evidence": {"path": args.evidence, "sha256": args.evidence_sha256},
                                       "review": {"path": args.review, "sha256": args.review_sha256}}
                            evidence = c.read_control(binding["evidence"])
                            if evidence["previous_state_sha256"] != before:
                                raise c.CompletionError("filesystem recovery is not for the exact previous pointer")
                            proposed = {**state, "filesystem_recovery": binding}
                            if bool(args.owner_recovery) != bool(args.owner_recovery_sha256):
                                raise c.CompletionError("companion owner recovery requires both path and SHA")
                            if args.owner_recovery:
                                companion = c.read_control({"path": args.owner_recovery, "sha256": args.owner_recovery_sha256})
                                c.fields(companion, ("evidence", "review"))
                                if c.read_control(companion["evidence"]).get("previous") != state.get("owner_recovery"):
                                    raise c.CompletionError("companion recovery must preserve the exact previous owner binding")
                                proposed["owner_recovery"] = companion
                            scope = c.filesystem_scope(state["scope"], c.read_control(state["scope"]), proposed)
                            recovery = c.owner_recovery(state["scope"], scope, proposed["owner_recovery"], state=proposed) if "owner_recovery" in proposed else None
                            c.validate_scope(scope, recovery)
                            state["filesystem_recovery"] = binding
                            if args.owner_recovery:
                                state["owner_recovery"] = companion
                        elif args.command == "recover-owner":
                            if state["outcome"] != "waiting" or state["scope"] is None:
                                raise c.CompletionError("owner recovery requires the existing waiting binding")
                            c.control_reference(state.get("status_basis"))
                            binding = {"evidence": {"path": args.evidence, "sha256": args.evidence_sha256},
                                       "review": {"path": args.review, "sha256": args.review_sha256}}
                            evidence = c.read_control(binding["evidence"])
                            if evidence.get("previous") != state.get("owner_recovery"):
                                raise c.CompletionError("owner recovery must preserve the exact previous evidence and review")
                            proposed = {**state, "owner_recovery": binding}
                            scope = c.filesystem_scope(state["scope"], c.read_control(state["scope"]), proposed)
                            c.validate_state_identity(scope, state, filename)
                            recovery = c.owner_recovery(state["scope"], scope, binding, state=proposed)
                            c.validate_scope(scope, recovery)
                            state["owner_recovery"] = binding
                        elif args.command == "preserve-source":
                            if state["outcome"] != "waiting" or state["scope"] is None or "source_preservation" in state:
                                raise c.CompletionError("source preservation requires the existing waiting binding and cannot replace a preservation")
                            c.control_reference(state.get("status_basis"))
                            binding = {"evidence": {"path": args.evidence, "sha256": args.evidence_sha256},
                                       "review": {"path": args.review, "sha256": args.review_sha256}}
                            scope = c.filesystem_scope(state["scope"], c.read_control(state["scope"]), state)
                            c.validate_state_identity(scope, state, filename)
                            if c.read_control(binding["evidence"])["previous_state_sha256"] != before:
                                raise c.CompletionError("source preservation is not for the exact previous pointer")
                            recovery = c.owner_recovery(state["scope"], scope, state["owner_recovery"], state=state) if "owner_recovery" in state else None
                            c.validate_scope(scope, recovery)
                            c.source_preservation(state["scope"], scope, binding)
                            state["source_preservation"] = binding
                        elif args.command in {"finalize", "complete"}:
                            if args.inputs:
                                if any((args.result, args.review, args.review_sha256)):
                                    raise c.CompletionError("use stdin inputs or file inputs, not both")
                                payload = c.decode(sys.stdin.buffer.read())
                                c.fields(payload, ("result", "review"))
                                scope = c.read_control(state["scope"])
                                root = c.path(scope["record_root"]["path"])
                                result_raw, review_raw = c.canonical(payload["result"]), c.canonical(payload["review"])
                                result_origin, review_origin = root / ".state-result.json", root / ".state-final-review.json"
                                if os.path.lexists(result_origin) or os.path.lexists(review_origin):
                                    raise c.CompletionError("stdin control origins must not shadow staging files")
                                state.update(result_path=str(result_origin), final_review={
                                    "path": str(review_origin), "sha256": c.sha(review_raw)})
                                protect_records(state, filename, state["resources"], observing=True)
                                c.remember_control(state, {"path": state["result_path"], "sha256": c.sha(result_raw)}, result_raw)
                                c.remember_control(state, state["final_review"], review_raw)
                                c.capture_controls(state, final=True, result_raw=result_raw, location=filename, observing=True)
                            elif any((args.result, args.review, args.review_sha256)):
                                if not all((args.result, args.review, args.review_sha256)):
                                    raise c.CompletionError("provide all result/review references together")
                                final = {"path": args.review, "sha256": args.review_sha256}
                                changed = (state.get("final_review") != final or state.get("result_path") != args.result
                                           or not any((r["path"], r["sha256"]) == (final["path"], final["sha256"])
                                                      for r in state.get("control_records", [])))
                                state.update(result_path=str(c.path(args.result)), final_review=c.control_reference(final))
                                c.capture_controls(state, final=changed, location=filename, observing=True)
                            elif not state.get("final_review") or not state.get("result_path"):
                                raise c.CompletionError("publish reviewed final inputs before completing")
                            protect_records(state, filename, state["resources"], observing=True)
                            if args.command == "complete":
                                # Preserve reviewed inputs for recovery before observing. A failed
                                # inspection leaves this CAS-updated pointer bound for retry.
                                c.capture_controls(state, location=filename, observing=True)
                                before = replace(filename, state, before)["sha256"]
                                scope = c.read_control(state["scope"])
                                answer = c.check_scope(state["scope"], state["result_path"], task_id=state["task_id"],
                                                       request_digest=args.request_digest, workspace=scope["environment"]["workspace"], review_ref=state["review"], state_path=str(filename), report=True)
                                if answer["complete"]:
                                    answer = close_checked(filename, state, before, scope, answer, args.work_turn)
                                answer["binding_released"] = answer["complete"]
                                print(c.canonical(answer).decode()); return 0 if answer["complete"] else 2
                        elif args.command == "outcome":
                            state.update(outcome=args.status, status_basis=c.control_reference({"path": args.basis, "sha256": args.basis_sha256}))
                            # Status evidence can record why the original scope is unavailable.
                            # Do not require that scope just to preserve this metadata.
                            checks = []
                            if state["scope"]:
                                try:
                                    checks = c.read_control(state["scope"])["checks"]
                                except (c.CompletionError, OSError, ValueError, TypeError):
                                    pass
                            for resource in [*checks, *state["resources"]]:
                                c.protect_completion_records(resource, [state["status_basis"]["path"]])
                        else:
                            scope = c.read_control(state["scope"])
                            answer = c.check_scope(state["scope"], state["result_path"], task_id=state["task_id"], workspace=scope["environment"]["workspace"], review_ref=state["review"], stopped=args.command == "release", state_path=str(filename))
                            if args.command in {"unbind", "release"}:
                                if not (answer["stopped"] if args.command == "release" else answer["complete"]): raise c.CompletionError("unfinished task remains bound")
                                answer = close_checked(filename, state, before, scope, answer, args.work_turn)
                            print(c.canonical(answer).decode()); return 0 if answer["complete"] or answer["stopped"] else 2
                        if args.command == "outcome":
                            c.remember_control(state, state["status_basis"], c.control_bytes(
                                state["status_basis"]["path"], state["status_basis"]["sha256"]))
                        else:
                            c.capture_controls(state, location=filename, observing=args.command == "finalize")
                        answer = replace(filename, state, before)
        print(c.canonical(answer).decode()); return 0
    except (c.CompletionError, OSError, ValueError, TypeError, KeyError) as exc:
        print(c.canonical({"complete": False, "error": str(exc)}).decode()); return 2


if __name__ == "__main__": sys.exit(main())
