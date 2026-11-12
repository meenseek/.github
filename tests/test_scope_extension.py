"""Follow-up scopes preserve the original work, including unfinished resources."""
import copy
import json
import subprocess
import sys
import unittest

import test_completion_hook as fixture

h = fixture.h
c = h.c


class ScopeExtensionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.HookTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.records = self.f.f.root / 'followup-records'
        self.f.records.mkdir()
        self.f.arm_bind()
        self.f.call('outcome', '--status', 'waiting', '--basis', str(self.f.f.request),
                    '--basis-sha256', self.f.f.ref(self.f.f.request)['sha256'])
        self.before = json.loads(self.f.state.read_bytes())
        self.scope = json.loads(self.f.f.scope_path.read_bytes())
        proof = self.f.records / 'followup-evidence.json'
        self.f.f.write(proof, {'verified': True})
        self.scope['checks'].append({'id': 'followup', 'category': 'quality', 'kind': 'evidence',
                                    'target': {'path': str(proof), 'assertions': [
                                        {'path': ['verified'], 'equals': True}]}})
        self.scope['categories']['quality']['checks'].append('followup')
        self.scope['categories']['quality']['not_applicable'] = None

    def inputs(self, suffix=""):
        f = self.f.f
        target = self.f.records / ('followup-scope' + suffix + '.json')
        f.write(target, self.scope)
        normal = self.f.records / ('followup-review' + suffix + '.json')
        f.write(normal, c.scope_review_value(f.ref(target), self.before.get('owner_recovery')))
        delta = self.f.records / ('delta-review' + suffix + '.json')
        value = {'previous_scope_sha256': self.before['scope']['sha256'],
                 'scope_sha256': f.ref(target)['sha256'],
                 'authorization_sha256': f.ref(f.request)['sha256'],
                 'previous_state_sha256': f.ref(self.f.state)['sha256'], 'status': 'No Findings'}
        if 'owner_recovery' in self.before:
            value['owner_recovery_sha256'] = self.before['owner_recovery']['evidence']['sha256']
        f.write(delta, value)
        return ['--scope', str(target), '--scope-sha256', f.ref(target)['sha256'],
                '--review', str(normal), '--review-sha256', f.ref(normal)['sha256'],
                '--authorization', str(f.request), '--authorization-sha256', f.ref(f.request)['sha256'],
                '--delta-review', str(delta), '--delta-review-sha256', f.ref(delta)['sha256']]

    def recover_snapshot_owner(self, suffix=""):
        f = self.f.f
        prior = json.loads(self.f.state.read_bytes()).get('owner_recovery')
        original = f.root / 'snapshot-owner-original.md'
        if prior is None:
            original.write_bytes(f.owner.read_bytes())
        f.owner.write_text('Reviewed current owner' + suffix + '\n')
        current = f.root / ('snapshot-owner-current' + suffix + '.md')
        current.write_bytes(f.owner.read_bytes())
        state = json.loads(self.f.state.read_bytes())
        evidence = {'scope_sha256': state['scope']['sha256'], 'task_id': f.seed['task_id'],
                    'session_id': f.seed['session_id'], 'authorization': f.ref(f.request),
                    'owners': [{'owner_path': str(f.owner), 'snapshots': {
                        'original': f.ref(original), 'current': f.ref(current)}}]}
        if prior:
            evidence['previous'] = prior
        proof = f.root / ('owner-recovery' + suffix + '.json')
        review = f.root / ('owner-recovery-review' + suffix + '.json')
        f.write(proof, evidence)
        f.write(review, {'scope_sha256': state['scope']['sha256'],
                        'recovery_sha256': f.ref(proof)['sha256'], 'status': 'No Findings'})
        self.f.call('recover-owner', '--evidence', str(proof), '--evidence-sha256', f.ref(proof)['sha256'],
                    '--review', str(review), '--review-sha256', f.ref(review)['sha256'])
        self.before = json.loads(self.f.state.read_bytes())

    def test_recovered_owner_can_extend_and_complete_without_rewriting_original_proof(self):
        self.recover_snapshot_owner()
        proof = c.raw_file(self.before['owner_recovery']['evidence']['path'])
        self.f.call('extend', *self.inputs())
        after = json.loads(self.f.state.read_bytes())
        self.assertEqual(after['owner_recovery'], self.before['owner_recovery'])
        self.assertEqual(after['scope_history'][0]['owner_recovery'], self.before['owner_recovery'])
        self.assertEqual(proof, c.raw_file(after['owner_recovery']['evidence']['path']))
        self.publish_result()
        self.assertTrue(self.f.call('inspect')['complete'])

    def test_extension_state_cannot_drop_the_carried_recovery(self):
        self.recover_snapshot_owner()
        self.f.call('extend', *self.inputs())
        state = json.loads(self.f.state.read_bytes())
        state.pop('owner_recovery')
        with self.assertRaisesRegex(c.CompletionError, 'lost its owner recovery'):
            c.scope_history(state['scope'], state)

    def test_recovered_extension_preserves_an_unresolved_resource(self):
        self.recover_snapshot_owner()
        self.f.call('reserve', '--id', 'still-unknown', '--basis', str(self.f.f.request),
                    '--basis-sha256', self.f.f.ref(self.f.f.request)['sha256'])
        self.before = json.loads(self.f.state.read_bytes())
        self.f.call('extend', *self.inputs())
        self.publish_result()
        result = self.f.call('inspect', ok=False)
        self.assertIn('resource:still-unknown', [x['id'] for x in result['failures']])

    def test_recovery_and_multiple_extensions_keep_each_original_scope(self):
        self.recover_snapshot_owner('-first')
        first = self.before['owner_recovery']
        self.f.call('extend', *self.inputs())
        self.recover_snapshot_owner('-second')
        second = self.before['owner_recovery']
        self.scope = c.read_control(self.before['scope'])
        self.scope['checks'].append({'id': 'second', 'category': 'quality', 'kind': 'evidence',
                                    'target': self.scope['checks'][-1]['target']})
        self.scope['categories']['quality']['checks'].append('second')
        self.f.call('extend', *self.inputs('-second'))
        state = json.loads(self.f.state.read_bytes())
        self.assertEqual([x['owner_recovery'] for x in state['scope_history']], [first, second])
        self.publish_result()
        self.assertTrue(self.f.call('inspect')['complete'])

    def test_recovered_extension_requires_both_reviews_to_bind_the_exact_recovery(self):
        self.recover_snapshot_owner()
        for filename, flag in [('followup-review.json', '--review-sha256'),
                               ('delta-review.json', '--delta-review-sha256')]:
            for wrong in (None, '0' * 64):
                with self.subTest(filename=filename, wrong=wrong):
                    args = self.inputs()
                    file = self.f.records / filename
                    value = json.loads(file.read_bytes())
                    if wrong is None:
                        value.pop('owner_recovery_sha256')
                    else:
                        value['owner_recovery_sha256'] = wrong
                    self.f.f.write(file, value)
                    args[args.index(flag) + 1] = self.f.f.ref(file)['sha256']
                    before = self.f.state.read_bytes()
                    self.f.call('extend', *args, ok=False)
                    self.assertEqual(self.f.state.read_bytes(), before)

    def test_foreign_recovery_scope_or_omitted_extension_binding_is_refused(self):
        self.recover_snapshot_owner()
        self.f.call('extend', *self.inputs())
        state = json.loads(self.f.state.read_bytes())
        original = c.read_control(state['owner_recovery']['evidence'])
        for scope_sha in ('0' * 64, state['scope']['sha256']):
            value = {**original, 'scope_sha256': scope_sha}
            proof = self.f.records / ('forged-' + scope_sha + '.json')
            review = proof.with_suffix('.review.json')
            self.f.f.write(proof, value)
            self.f.f.write(review, {'scope_sha256': scope_sha,
                                   'recovery_sha256': self.f.f.ref(proof)['sha256'], 'status': 'No Findings'})
            binding = {'evidence': self.f.f.ref(proof), 'review': self.f.f.ref(review)}
            with self.subTest(scope=scope_sha), self.assertRaises(c.CompletionError):
                c.owner_recovery(state['scope'], c.read_control(state['scope']), binding,
                                 state={**state, 'owner_recovery': binding})

    def test_git_owner_context_survives_unrelated_main_advance_but_rejects_new_owner_drift(self):
        other = fixture.HookTests()
        other.setUp()
        self.addCleanup(other.doCleanups)
        self.f = other
        f = other.f
        f.seed['state_path'] = str(other.state)
        f.recovery_fixture()
        owner = f.recovery['owners'][0]
        owner['source']['definition']['target']['files'] = ['README.md']
        owner['source']['expected']['files'] = {'README.md': owner['current']['sha256']}
        f.write(f.recovery_path, f.recovery)
        f.write(f.recovery_review_path, {'scope_sha256': f.scope_ref['sha256'],
                                      'recovery_sha256': f.ref(f.recovery_path)['sha256'], 'status': 'No Findings'})
        other.root.mkdir(mode=0o700)
        waiting = {**f.state, 'outcome': 'waiting', 'status_basis': f.ref(f.request)}
        waiting.pop('owner_recovery')
        f.write(other.state, waiting)
        other.call('recover-owner', '--evidence', str(f.recovery_path), '--evidence-sha256', f.ref(f.recovery_path)['sha256'],
                   '--review', str(f.recovery_review_path), '--review-sha256', f.ref(f.recovery_review_path)['sha256'])
        other.records = f.root / 'followup-records'
        other.records.mkdir()
        self.before = json.loads(other.state.read_bytes())
        self.scope = c.read_control(self.before['scope'])
        proof = other.records / 'followup-evidence.json'
        f.write(proof, {'verified': True})
        self.scope['checks'].append({'id': 'followup', 'category': 'quality', 'kind': 'evidence',
                                    'target': {'path': str(proof), 'assertions': [{'path': ['verified'], 'equals': True}]}})
        self.scope['categories']['quality']['checks'].append('followup')
        self.scope['categories']['quality']['not_applicable'] = None
        other.call('extend', *self.inputs())
        (f.repo / 'unrelated.py').write_text("print('independent published change')\n")
        f.git('add', 'unrelated.py'); f.git('commit', '-m', 'test: advance unrelated code'); f.git('push', 'origin', 'main')
        self.publish_result()
        self.assertTrue(other.call('inspect')['complete'])
        f.owner.write_text('Unreviewed new owner drift.\n')
        f.git('commit', '-am', 'test: unreviewed owner drift'); f.git('push', 'origin', 'main')
        self.assertFalse(other.call('inspect', ok=False)['complete'])

    def test_actual_cli_preserves_old_verdict_and_unknown_resource(self):
        self.f.call('reserve', '--id', 'unreturned-create', '--basis', str(self.f.f.request),
                    '--basis-sha256', self.f.f.ref(self.f.f.request)['sha256'])
        self.before = json.loads(self.f.state.read_bytes())
        original = self.f.f.scope_path.read_bytes()
        self.f.call('extend', *self.inputs())
        after = json.loads(self.f.state.read_bytes())
        self.assertEqual(self.before['resources'], after['resources'])
        self.assertEqual(self.before['control_records'], after['control_records'][:len(self.before['control_records'])])
        self.assertEqual(original, self.f.f.scope_path.read_bytes())
        self.assertIsNone(after['result_path'])
        self.assertIsNone(after['final_review'])
        self.assertEqual(after['outcome'], 'waiting')
        step = after['scope_history'][0]
        self.assertEqual(step['scope'], self.before['scope'])
        self.assertEqual(step['final_review'], self.before['final_review'])
        self.assertEqual(step['result']['path'], self.before['result_path'])
        with c.stored_controls(after, self.f.state):
            c.scope_history(after['scope'], after)

    def test_removed_obligation_changed_request_or_inventory_refused_without_mutation(self):
        original = copy.deepcopy(self.scope)
        for change in ('obligation', 'request', 'inventory', 'owner'):
            with self.subTest(change=change):
                self.scope = copy.deepcopy(original)
                if change == 'obligation':
                    self.scope['checks'].pop(0)
                elif change == 'request':
                    self.scope['request_digest'] = 'a' * 64
                elif change == 'inventory':
                    next(iter(self.scope['baseline']['repositories'].values()))['refs'] = {}
                else:
                    self.scope['owners'] = []
                before = self.f.state.read_bytes()
                self.f.call('extend', *self.inputs(), ok=False)
                self.assertEqual(before, self.f.state.read_bytes())

    def test_stale_cas_does_not_publish_extension(self):
        args = self.inputs()
        previous = self.f.f.ref(self.f.state)['sha256']
        self.f.call('reserve', '--id', 'concurrent-resource', '--basis', str(self.f.f.request),
                    '--basis-sha256', self.f.f.ref(self.f.f.request)['sha256'])
        before = self.f.state.read_bytes()
        p = subprocess.run([sys.executable, '-I', str(fixture.SCRIPT), '--bindings', str(self.f.root),
                            'extend', '--session', self.f.f.seed['session_id'], '--expected-sha256', previous,
                            *args], capture_output=True, timeout=35)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(before, self.f.state.read_bytes())

    def test_delta_review_must_bind_current_pointer_and_authorization(self):
        args = self.inputs()
        delta = self.f.records / 'delta-review.json'
        value = json.loads(delta.read_bytes())
        value['authorization_sha256'] = '0' * 64
        self.f.f.write(delta, value)
        args[-1] = self.f.f.ref(delta)['sha256']
        before = self.f.state.read_bytes()
        self.f.call('extend', *args, ok=False)
        self.assertEqual(before, self.f.state.read_bytes())

    def test_generic_publication_cannot_introduce_scope_history(self):
        proposed = {**self.before, 'scope_history': []}
        with self.assertRaisesRegex(c.CompletionError, 'preserve owner state'):
            h.publication_admissions(proposed, self.before, final=False)

    def test_frozen_record_manifest_cannot_be_reset_by_extension(self):
        self.f.f.write(self.f.state, {**self.before, 'record_manifest': {}})
        before = self.f.state.read_bytes()
        self.f.call('extend', *self.inputs(), ok=False)
        self.assertEqual(before, self.f.state.read_bytes())

    def test_final_review_binds_entire_scope_history(self):
        self.f.call('extend', *self.inputs())
        after = json.loads(self.f.state.read_bytes())
        with c.stored_controls(after, self.f.state):
            scope = c.read_control(after['scope'])
            with unittest.mock.patch.object(c, 'checked_record_inventory', return_value='f' * 64):
                value = c.final_review_fields(after['scope'], scope, after, 'a' * 64, 'review')
        self.assertEqual(value['scope_history_sha256'], c.sha(c.canonical(after['scope_history'])))

    def test_later_filesystem_projection_does_not_rewrite_frozen_lineage(self):
        self.f.call('extend', *self.inputs())
        after = json.loads(self.f.state.read_bytes())
        projected = json.loads((self.f.records / 'followup-scope.json').read_bytes())
        next(iter(projected['baseline']['repositories'].values()))['identity']['device'] += 100
        with c.stored_controls(after, self.f.state):
            value = c.final_review_fields(after['scope'], projected, after, 'a' * 64, 'review')
        self.assertEqual(value['scope_history_sha256'], c.sha(c.canonical(after['scope_history'])))

    def test_unaffected_na_decision_cannot_be_reworded(self):
        name = next(k for k, v in self.scope['categories'].items() if not v['checks'])
        self.scope['categories'][name]['not_applicable']['reason'] = 'Changed old decision'
        before = self.f.state.read_bytes()
        self.f.call('extend', *self.inputs(), ok=False)
        self.assertEqual(before, self.f.state.read_bytes())

    def publish_result(self):
        self.f.call('outcome', '--status', 'working', '--basis', str(self.f.f.request),
                    '--basis-sha256', self.f.f.ref(self.f.f.request)['sha256'])
        state = json.loads(self.f.state.read_bytes())
        result = c.draft_result(state['scope'])
        output = self.f.records / 'extended-result.json'
        review = self.f.records / 'extended-final-review.json'
        self.f.f.write(output, result)
        value = {'scope_sha256': state['scope']['sha256'],
                 'result_sha256': self.f.f.ref(output)['sha256'],
                 'scope_history_sha256': c.sha(c.canonical(state['scope_history'])), 'status': 'No Findings'}
        if 'owner_recovery' in state:
            value['owner_recovery_sha256'] = state['owner_recovery']['evidence']['sha256']
        self.f.f.write(review, value)
        self.f.call('finalize', '--result', str(output), '--review', str(review),
                    '--review-sha256', self.f.f.ref(review)['sha256'])

    def test_actual_followup_checks_are_enforced_by_completion(self):
        self.f.call('extend', *self.inputs())
        self.f.f.write(self.f.records / 'followup-evidence.json', {'verified': False})
        self.publish_result()
        self.assertFalse(self.f.call('inspect', ok=False)['complete'])

    def test_extended_scope_can_complete_with_new_result_and_review(self):
        self.f.call('extend', *self.inputs())
        self.publish_result()
        self.assertTrue(self.f.call('inspect')['complete'])


if __name__ == '__main__':
    unittest.main()
