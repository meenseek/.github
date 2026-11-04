"""Fresh-process lifecycle tests use real isolated Git and filesystem state."""
import hashlib
import io
import json
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

import test_completion_hook as fixture
import test_filesystem_recovery as recovery_fixture
import test_improvement as improvement_fixture


class HistoricalDraftTests(unittest.TestCase):
    def draft_after_removal(self, kind):
        f = fixture.HookTests()
        f.setUp()
        improvement = None
        try:
            f.record_fixture()
            if kind == 'improvement':
                improvement = improvement_fixture.ImprovementTests()
                improvement.setUp()
                improvement.plan['task_id'] = f.f.seed['task_id']
                improvement.freeze()
                proof = improvement.record_path
                check = {'id': 'historical', 'category': 'quality', 'kind': kind,
                         'target': {'path': str(proof), 'plan': improvement.plan_ref}}
                f.f.seed['owners'].append(improvement.ref(improvement.basis))
            else:
                proof = (f.f.root if kind in ('file', 'installed_file') else f.records) / 'evidence.json'
                proof.write_text('{"verified":true}\n')
                check = {'id': 'historical', 'category': 'quality', 'kind': 'evidence',
                         'target': {'path': str(proof)}}
                if kind in ('file', 'installed_file'):
                    source = f.f.root / 'current-source.json'
                    source.write_bytes(proof.read_bytes())
                    f.f.seed['checks'].append({'id': 'live-target', 'category': 'artifacts', 'kind': kind,
                        'target': {'path': str(proof), **({'source': str(source)} if kind == 'installed_file' else {})}})
            f.f.seed['checks'].append(check)
            f.f.reset_categories()
            f.arm_bind(finalize=False)
            expected = {'sha256': f.f.ref(proof)['sha256']}
            f.f.result['checks']['historical'] = expected
            if kind in ('file', 'installed_file'):
                f.f.result['checks']['live-target'] = expected
            f.f.write(f.f.result_path, f.f.result)
            f.f.final_review()
            f.call('finalize', *f.completion_args())
            self.assertTrue(f.call('inspect')['complete'])
            if kind in ('file', 'installed_file', 'improvement'):
                proof.unlink()
            if kind in ('evidence', 'improvement'):
                shutil.rmtree(f.records)
            before = f.state.read_bytes()
            output = f.f.root / 'fresh-draft.json'
            run = subprocess.run([sys.executable, '-I', str(fixture.SCRIPT.with_name('check_completion.py')),
                'draft', '--state', str(f.state), '--output', str(output)], capture_output=True, text=True, timeout=35)
            self.assertEqual(f.state.read_bytes(), before)
            if kind in ('evidence', 'improvement'):
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertEqual(json.loads(output.read_bytes())['checks']['historical'], expected)
                self.assertFalse(f.records.exists())
            else:
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertFalse(output.exists())
        finally:
            f.doCleanups()
            if improvement:
                improvement.doCleanups()

    def test_fresh_draft_reuses_saved_observations_after_staging_removed(self):
        for kind in ('evidence', 'improvement'):
            with self.subTest(kind=kind):
                self.draft_after_removal(kind)

    def test_fresh_draft_does_not_freeze_current_files_also_saved_as_evidence(self):
        for kind in ('file', 'installed_file'):
            with self.subTest(kind=kind):
                self.draft_after_removal(kind)


class ContainedLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.HookTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.record_fixture()
        self.f.arm_bind(finalize=False)
        self.session = self.f.f.seed['session_id']

    def call(self, command, *args, payload=None, owner=None, ok=True):
        session = owner or self.session
        state = fixture.h.pointer(self.f.root, session)
        argv = [sys.executable, '-I', str(fixture.SCRIPT), '--bindings', str(self.f.root), command,
                '--session', session, '--expected-sha256', fixture.h.c.sha(state.read_bytes()), *args]
        run = subprocess.run(argv, input=None if payload is None else json.dumps(payload),
                             capture_output=True, text=True, timeout=35)
        self.assertEqual(run.returncode, 0 if ok else 2, run.stdout + run.stderr)
        return json.loads(run.stdout)

    def event(self, session):
        run = subprocess.run([sys.executable, '-I', str(fixture.SCRIPT), '--bindings', str(self.f.root), 'hook'],
            input=json.dumps({**self.f.payload, 'session_id': session, 'hook_event_name': 'SessionStart'}),
            capture_output=True, text=True, timeout=35)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)['hookSpecificOutput']['additionalContext']

    def final_inputs(self):
        c = fixture.h.c
        result = self.f.f.result
        review = {'scope_sha256': self.f.f.scope_ref['sha256'], 'result_sha256': c.sha(c.canonical(result)),
                  'record_inventory_sha256': c.sha(c.canonical(c.record_inventory(
                      c.read_reference(self.f.f.scope_ref)['record_root'])))
                      if self.f.records.exists() else c.sha(c.canonical({})), 'status': 'No Findings'}
        return {'result': result, 'review': review}

    def test_removed_staging_then_new_runtime_adopt_work_and_complete(self):
        original = self.f.f.scope_ref
        shutil.rmtree(self.f.records)
        self.assertIn('기존 연결 작업=', self.event(self.session))
        self.assertNotIn('기존 목표 연결 미확인', self.event(self.session))
        self.assertNotIn('기존 연결 작업=', self.event('independent-new-session'))
        old = self.session
        argv = [sys.executable, '-I', str(fixture.SCRIPT), '--bindings', str(self.f.root), 'adopt',
                '--session', 'successor', '--from-session', old, '--expected-sha256', fixture.h.c.sha(self.f.state.read_bytes()),
                *self.f.goal_args(), *self.f.basis()]
        run = subprocess.run(argv, capture_output=True, text=True, timeout=35)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.session = 'successor'
        current = fixture.h.pointer(self.f.root, self.session)
        state = json.loads(current.read_bytes())
        self.assertEqual(state['scope'], original)
        self.assertFalse(self.f.state.exists())
        self.assertIn('기존 연결 작업=', self.event(self.session))
        self.call('reserve', '--id', 'after-adoption', *self.f.basis())
        owned = self.f.f.root / 'owned-after-adoption.txt'
        owned.write_text('temporary actual resource')
        self.call('attach', '--id', 'after-adoption', '--resource', '-', *self.f.basis(),
                  payload={'kind': 'absent', 'target': {'path': str(owned)}})
        # Final inputs travel on stdin; the old staging directory is never recreated.
        self.call('finalize', '--inputs', '-', payload=self.final_inputs())
        self.call('inspect', ok=False)
        owned.unlink()
        answer = self.call('complete', *self.f.goal_args())
        self.assertTrue(answer['complete'])
        self.assertTrue(answer['binding_released'])
        self.assertFalse(current.exists())
        self.assertFalse(self.f.records.exists())
        self.assertNotIn('기존 연결 작업=', self.event(self.session))

    def test_stdin_finalization_with_existing_staging_cleans_reviewed_files(self):
        self.call('finalize', '--inputs', '-', payload=self.final_inputs())
        result = self.call('complete', *self.f.goal_args())
        self.assertTrue(result['complete'])
        self.assertFalse(self.f.state.exists())
        self.assertFalse(self.f.records.exists())

    def test_damaged_saved_control_and_stale_cas_are_rejected_without_changes(self):
        state = json.loads(self.f.state.read_bytes())
        state['control_records'][0]['data'] = 'corrupt'
        self.f.state.write_text(json.dumps(state))
        before = self.f.state.read_bytes()
        self.call('inspect', ok=False)
        self.assertEqual(self.f.state.read_bytes(), before)

    def test_actual_source_change_still_blocks_after_staging_removed(self):
        self.f.call('finalize', *self.f.completion_args())
        shutil.rmtree(self.f.records)
        (self.f.f.repo / 'tool.py').write_text('unreviewed source mutation')
        result = self.call('inspect', ok=False)
        self.assertFalse(result['complete'])
        self.assertTrue(self.f.state.exists())

    def test_interrupted_adoption_retries_exact_existing_state(self):
        argv = ['--bindings', str(self.f.root), 'adopt', '--session', 'successor', '--from-session', self.session,
                '--expected-sha256', fixture.h.c.sha(self.f.state.read_bytes()), *self.f.goal_args(), *self.f.basis()]
        with mock.patch.object(fixture.h.os, 'rename', side_effect=OSError('synthetic interrupted rename')), redirect_stdout(io.StringIO()):
            self.assertEqual(fixture.h.main(argv), 2)
        self.assertTrue(self.f.state.exists())
        argv[argv.index('--expected-sha256') + 1] = fixture.h.c.sha(self.f.state.read_bytes())
        run = subprocess.run([sys.executable, '-I', str(fixture.SCRIPT), *argv], capture_output=True, text=True, timeout=35)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertFalse(self.f.state.exists())
        self.assertTrue(fixture.h.pointer(self.f.root, 'successor').exists())

    def test_unknown_legacy_inputs_are_not_reconstructed(self):
        state = json.loads(self.f.state.read_bytes())
        state.pop('control_records')
        self.f.state.write_text(json.dumps(state))
        shutil.rmtree(self.f.records)
        self.assertIn('기존 목표 연결 미확인', self.event(self.session))
        self.call('inspect', ok=False)

    def test_adopted_state_copy_cannot_replace_current_locator(self):
        c = fixture.h.c
        argv = [sys.executable, '-I', str(fixture.SCRIPT), '--bindings', str(self.f.root), 'adopt',
                '--session', 'successor', '--from-session', self.session, '--expected-sha256',
                c.sha(self.f.state.read_bytes()), *self.f.goal_args(), *self.f.basis()]
        run = subprocess.run(argv, capture_output=True, text=True, timeout=35)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        pointer = fixture.h.pointer(self.f.root, 'successor')
        copied = pointer.with_suffix('.backup')
        copied.write_bytes(pointer.read_bytes())
        with self.assertRaisesRegex(c.CompletionError, 'current session identity changed'):
            with c.bound_state(str(copied)):
                self.fail('accepted copied state')
        with c.bound_state(str(pointer)) as state:
            self.assertEqual(state['session_id'], 'successor')

    def test_filesystem_recovery_survives_adopt_and_removed_staging(self):
        run = recovery_fixture.FilesystemRecoveryTests()
        run.setUp()
        self.addCleanup(run.doCleanups)
        run.recover()
        h, c = run.hooks, run.c
        h.call('outcome', '--status', 'working', '--basis', str(run.f.request), '--basis-sha256', run.f.ref(run.f.request)['sha256'])
        state = json.loads(h.state.read_bytes())
        review = {'scope_sha256': run.f.scope_ref['sha256'], 'result_sha256': run.f.ref(run.f.result_path)['sha256'],
                  'status': 'No Findings', 'filesystem_recovery_sha256': state['filesystem_recovery']['evidence']['sha256'],
                  'record_inventory_sha256': c.reviewed_record_inventory(c.record_directory(str(h.records)), str(run.f.final_review_path))}
        run.f.write(run.f.final_review_path, review)
        h.call('finalize', *h.completion_args())
        argv = [sys.executable, '-I', str(fixture.SCRIPT), '--bindings', str(h.root), 'adopt',
                '--session', 'recovered-successor', '--from-session', run.f.seed['session_id'], '--expected-sha256',
                c.sha(h.state.read_bytes()), *h.goal_args(), *h.basis()]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=35)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        shutil.rmtree(h.records)
        pointer = fixture.h.pointer(h.root, 'recovered-successor')
        result = subprocess.run([sys.executable, '-I', str(fixture.SCRIPT), '--bindings', str(h.root), 'complete',
            '--session', 'recovered-successor', '--expected-sha256', c.sha(pointer.read_bytes()), *h.goal_args()],
            capture_output=True, text=True, timeout=35)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(pointer.exists())
        self.assertFalse(h.records.exists())

    def test_control_parser_uses_one_digest_verified_buffer(self):
        c = fixture.h.c
        raw = c.canonical({'accepted': True})
        ref = {'path': str(self.f.f.root / 'new-control.json'), 'sha256': c.sha(raw)}
        with mock.patch.object(c, 'control_bytes', side_effect=[raw, b'{"accepted":false}']) as read:
            self.assertEqual(c.read_control(ref), {'accepted': True})
            self.assertEqual(read.call_count, 1)

    def test_unknown_legacy_execution_owner_is_not_assumed_direct(self):
        state = json.loads(self.f.state.read_bytes())
        state.pop('execution_owner')
        self.f.state.write_text(json.dumps(state))
        before = self.f.state.read_bytes()
        argv = [sys.executable, '-I', str(fixture.SCRIPT), '--bindings', str(self.f.root), 'adopt', '--session',
                'successor', '--from-session', self.session, '--expected-sha256', fixture.h.c.sha(before),
                *self.f.goal_args(), *self.f.basis()]
        run = subprocess.run(argv, capture_output=True, text=True, timeout=35)
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertEqual(self.f.state.read_bytes(), before)

    def test_registered_execution_owner_cannot_be_adopted_by_direct_cli(self):
        state = json.loads(self.f.state.read_bytes())
        state['execution_owner'] = 'operations'
        self.f.state.write_text(json.dumps(state))
        before = self.f.state.read_bytes()
        argv = [sys.executable, '-I', str(fixture.SCRIPT), '--bindings', str(self.f.root), 'adopt', '--session',
                'successor', '--from-session', self.session, '--expected-sha256', fixture.h.c.sha(before),
                *self.f.goal_args(), *self.f.basis()]
        run = subprocess.run(argv, capture_output=True, text=True, timeout=35)
        self.assertEqual(run.returncode, 2, run.stdout)
        self.assertEqual(self.f.state.read_bytes(), before)
