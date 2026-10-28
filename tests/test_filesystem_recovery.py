"""Explicit device recovery keeps immutable scope, inode and full completion gates."""
import copy
import json
from pathlib import Path
import unittest
import test_completion_hook as hook_fixture


class FilesystemRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.hooks = hook_fixture.HookTests()
        self.hooks.setUp()
        self.addCleanup(self.hooks.doCleanups)
        self.hooks.record_fixture(nested=True)
        self.hooks.arm_bind(finalize=False)
        self.f = self.hooks.f
        self.c = hook_fixture.h.c
        scope = json.loads(self.f.scope_path.read_bytes())
        slots = [x['identity'] for x in scope['baseline']['repositories'].values()]
        for check in scope['checks']:
            binding = check['target'].get('remote_binding', {})
            slots.extend(binding[k] for k in ('local', 'destination') if k in binding)
        slots.append(scope['record_root'])
        rows = {}
        for slot in slots:
            location = slot.get('common_dir', slot.get('path'))
            current = {k: slot[k] for k in ('device', 'inode')}
            slot['device'] += 100
            rows[location] = {'path': location, 'before': {k: slot[k] for k in ('device', 'inode')}, 'after': current}
        self.f.write(self.f.scope_path, scope)
        self.f.scope_ref = self.f.ref(self.f.scope_path)
        self.f.write(self.f.review_path, {'scope_sha256': self.f.scope_ref['sha256'], 'status': 'No Findings'})
        state = json.loads(self.hooks.state.read_bytes())
        state.update(scope=self.f.scope_ref, review=self.f.ref(self.f.review_path), outcome='waiting', status_basis=self.f.ref(self.f.request))
        self.f.write(self.hooks.state, state)
        self.f.result['scope_sha256'] = self.f.scope_ref['sha256']
        self.f.write(self.f.result_path, self.f.result)
        self.frozen = self.f.scope_path.read_bytes()
        self.evidence_path = self.hooks.records / 'filesystem-evidence.json'
        self.review_path = self.hooks.records / 'filesystem-review.json'
        self.evidence = {'scope_sha256': self.f.scope_ref['sha256'], 'task_id': state['task_id'], 'session_id': state['session_id'],
                         'previous_state_sha256': self.f.ref(self.hooks.state)['sha256'], 'authorization': self.f.ref(self.f.request),
                         'identities': list(rows.values())}
        self.save_evidence()

    def save_evidence(self):
        self.f.write(self.evidence_path, self.evidence)
        self.f.write(self.review_path, {'scope_sha256': self.f.scope_ref['sha256'],
                                      'filesystem_recovery_sha256': self.f.ref(self.evidence_path)['sha256'], 'status': 'No Findings'})

    def recover(self, ok=True):
        return self.hooks.call('recover-filesystem', '--evidence', str(self.evidence_path), '--evidence-sha256', self.f.ref(self.evidence_path)['sha256'],
                               '--review', str(self.review_path), '--review-sha256', self.f.ref(self.review_path)['sha256'], ok=ok)

    def test_explicit_recovery_completes_actual_cli_and_removes_only_records(self):
        self.recover()
        self.assertEqual(self.f.scope_path.read_bytes(), self.frozen)
        self.hooks.call('outcome', '--status', 'working', '--basis', str(self.f.request), '--basis-sha256', self.f.ref(self.f.request)['sha256'])
        state = json.loads(self.hooks.state.read_bytes())
        reviewed = {'scope_sha256': self.f.scope_ref['sha256'], 'result_sha256': self.f.ref(self.f.result_path)['sha256'], 'status': 'No Findings',
                    'filesystem_recovery_sha256': state['filesystem_recovery']['evidence']['sha256'],
                    'record_inventory_sha256': self.c.reviewed_record_inventory(self.c.record_directory(str(self.hooks.records)), str(self.f.final_review_path))}
        self.f.write(self.f.final_review_path, reviewed)
        result = self.hooks.call('complete', *self.hooks.completion_args())
        self.assertTrue(result['complete'])
        self.assertTrue(result['binding_released'])
        self.assertFalse(self.hooks.records.exists())
        self.assertFalse(self.hooks.state.exists())
        self.assertTrue(self.f.repo.exists())
        self.assertTrue(self.f.owner.exists())

    def test_inode_change_does_not_become_recoverable(self):
        self.evidence['identities'][0]['after']['inode'] += 1
        self.save_evidence()
        self.recover(ok=False)
        self.assertEqual(self.f.scope_path.read_bytes(), self.frozen)

    def test_missing_or_unrelated_identity_is_rejected(self):
        original = copy.deepcopy(self.evidence)
        self.evidence['identities'].pop()
        self.save_evidence(); self.recover(ok=False)
        self.evidence = original
        self.evidence['identities'].append({'path': str(self.f.root), 'before': {'device': 1, 'inode': 1}, 'after': {'device': 2, 'inode': 1}})
        self.save_evidence(); self.recover(ok=False)

    def test_stale_pointer_and_wrong_task_are_rejected(self):
        original = copy.deepcopy(self.evidence)
        self.evidence['previous_state_sha256'] = '0' * 64
        self.save_evidence(); self.recover(ok=False)
        self.evidence = original
        self.evidence['task_id'] = 'different-task'
        self.save_evidence(); self.recover(ok=False)

    def test_recovery_does_not_waive_actual_source_mismatch(self):
        self.recover()
        self.hooks.call('outcome', '--status', 'working', '--basis', str(self.f.request), '--basis-sha256', self.f.ref(self.f.request)['sha256'])
        self.f.installed.unlink(); self.f.installed.write_text('unreviewed replacement')
        state = json.loads(self.hooks.state.read_bytes())
        self.f.write(self.f.final_review_path, {'scope_sha256': self.f.scope_ref['sha256'], 'result_sha256': self.f.ref(self.f.result_path)['sha256'], 'status': 'No Findings',
            'filesystem_recovery_sha256': state['filesystem_recovery']['evidence']['sha256'],
            'record_inventory_sha256': self.c.reviewed_record_inventory(self.c.record_directory(str(self.hooks.records)), str(self.f.final_review_path))})
        result = self.hooks.call('complete', *self.hooks.completion_args(), ok=False)
        self.assertFalse(result['complete'])
        self.assertTrue(self.hooks.records.exists())
        self.assertTrue(self.hooks.state.exists())


if __name__ == '__main__':
    unittest.main()
