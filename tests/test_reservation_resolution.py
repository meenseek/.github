"""A reviewed denied attempt can finish without erasing its reservation."""
import base64
import copy
import json
import subprocess
import sys
import unittest

import test_completion_hook as fixture

h = fixture.h
c = h.c


class ReservationResolutionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.HookTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.record_fixture()
        self.f.arm_bind(finalize=False)
        self.reserve('denied-attempt')

    def reserve(self, key):
        self.f.call('reserve', '--id', key, '--basis', str(self.f.f.request),
                    '--basis-sha256', self.f.f.ref(self.f.f.request)['sha256'])

    def inputs(self, key='denied-attempt', change=None, review_change=None):
        state = json.loads(self.f.state.read_bytes())
        source = self.f.records / 'actual-tool-result.txt'
        source.write_text('Synthetic policy denial: request rejected before dispatch; no creation occurred.\n')
        evidence = {'task_id': state['task_id'], 'session_id': self.f.f.seed['session_id'],
                    'request_digest': self.f.f.seed['request_digest'], 'scope_sha256': state['scope']['sha256'],
                    'resource': next(row for row in state['resources'] if row['id'] == key),
                    'previous_state_sha256': c.sha(self.f.state.read_bytes()),
                    'disposition': 'not_created', 'dispatch_status': 'rejected_before_dispatch', 'created': False,
                    'source_tool': 'synthetic.create', 'source_call_id': 'synthetic-call-1',
                    'source': self.f.f.ref(source), 'reason': 'Exact synthetic pre-dispatch denial, independently checked.'}
        if change:
            change(evidence)
        target = self.f.records / 'denial-evidence.json'
        self.f.f.write(target, evidence)
        review = {'scope_sha256': evidence['scope_sha256'], 'evidence_sha256': self.f.f.ref(target)['sha256'],
                  'resource_sha256': c.sha(c.canonical(evidence['resource'])),
                  'previous_state_sha256': evidence['previous_state_sha256'], 'status': 'No Findings'}
        if review_change:
            review_change(review)
        reviewed = self.f.records / 'denial-review.json'
        self.f.f.write(reviewed, review)
        return ['--id', key, '--evidence', str(target), '--evidence-sha256', self.f.f.ref(target)['sha256'],
                '--review', str(reviewed), '--review-sha256', self.f.f.ref(reviewed)['sha256']]

    def final_inputs(self):
        state = json.loads(self.f.state.read_bytes())
        scope = c.read_control(state['scope'])
        review = c.final_review_fields(state['scope'], scope, state,
            c.sha(self.f.f.result_path.read_bytes()), str(self.f.f.final_review_path), fresh_inventory=True)
        self.f.f.write(self.f.f.final_review_path, review)
        return self.f.completion_args()

    def test_actual_cli_preserves_original_records_and_completes_reviewed_denial(self):
        before = json.loads(self.f.state.read_bytes())
        self.f.call('resolve-reservation', *self.inputs())
        after = json.loads(self.f.state.read_bytes())
        self.assertEqual(after['resources'], before['resources'])
        self.assertEqual(after['control_records'][:len(before['control_records'])], before['control_records'])
        self.assertEqual(len(after['reservation_resolutions']), 1)
        self.assertEqual(after['resources'][0]['kind'], 'reserved')
        with c.stored_controls(after, self.f.state):
            self.assertEqual(c.reservation_resolutions(c.read_control(after['scope']), after)
                             ['denied-attempt']['disposition'], 'not_created')
        answer = self.f.call('complete', *self.final_inputs())
        self.assertTrue(answer['complete'])
        self.assertTrue(answer['binding_released'])
        self.assertFalse(self.f.state.exists())
        self.assertFalse(self.f.records.exists())

    def test_unknown_other_attempt_still_prevents_completion(self):
        self.reserve('unreturned-create')
        self.f.call('resolve-reservation', *self.inputs())
        answer = self.f.call('complete', *self.final_inputs(), ok=False)
        self.assertFalse(answer['complete'])
        self.assertTrue(any(row['id'] == 'resource:unreturned-create' for row in answer['failures']))
        self.assertTrue(self.f.state.exists())
        self.assertTrue(self.f.records.exists())

    def test_wrong_identity_unknown_started_and_review_are_rejected_without_mutation(self):
        changes = [lambda e: e.update(task_id='different-task'),
                   lambda e: e.update(session_id='different-session'),
                   lambda e: e.update(request_digest='1' * 64),
                   lambda e: e.update(scope_sha256='1' * 64),
                   lambda e: e['resource'].update(id='different-reservation'),
                   lambda e: e['resource'].update(target={'pid': 123}),
                   lambda e: e.update(previous_state_sha256='1' * 64),
                   lambda e: e.update(disposition='unknown'),
                   lambda e: e.update(dispatch_status='dispatched'),
                   lambda e: e.update(created=True),
                   lambda e: e.update(created=0),
                   lambda e: e['source'].update(sha256='1' * 64)]
        for change in changes:
            with self.subTest(change=change):
                before = self.f.state.read_bytes()
                self.f.call('resolve-reservation', *self.inputs(change=change), ok=False)
                self.assertEqual(before, self.f.state.read_bytes())
        for key in ('status', 'resource_sha256', 'evidence_sha256', 'previous_state_sha256'):
            with self.subTest(review=key):
                before = self.f.state.read_bytes()
                self.f.call('resolve-reservation', *self.inputs(review_change=lambda r: r.update({key: 'wrong'})), ok=False)
                self.assertEqual(before, self.f.state.read_bytes())

    def test_stale_cas_duplicate_and_later_attach_are_rejected(self):
        args = self.inputs()
        previous = c.sha(self.f.state.read_bytes())
        self.reserve('other-attempt')
        before = self.f.state.read_bytes()
        done = subprocess.run([sys.executable, '-I', str(fixture.SCRIPT), '--bindings', str(self.f.root),
            'resolve-reservation', '--session', self.f.f.seed['session_id'], '--expected-sha256', previous,
            *args], capture_output=True, timeout=35)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(before, self.f.state.read_bytes())
        args = self.inputs()
        self.f.call('resolve-reservation', *args)
        before = self.f.state.read_bytes()
        self.f.call('resolve-reservation', *args, ok=False)
        resource = self.f.records / 'late-resource.json'
        self.f.f.write(resource, {'kind': 'absent', 'target': {'path': str(self.f.f.root / 'late')}})
        self.f.call('attach', '--id', 'denied-attempt', '--resource', str(resource),
                    '--basis', str(self.f.f.request), '--basis-sha256', self.f.f.ref(self.f.f.request)['sha256'], ok=False)
        self.assertEqual(before, self.f.state.read_bytes())

    def test_generic_publication_cannot_add_change_or_drop_resolution_history(self):
        self.f.call('resolve-reservation', *self.inputs())
        before = self.f.state.read_bytes()
        accepted = json.loads(before)
        for change in ('add', 'change', 'drop', 'attach'):
            state = copy.deepcopy(accepted)
            if change == 'add':
                state['reservation_resolutions'].append(copy.deepcopy(state['reservation_resolutions'][0]))
            elif change == 'change':
                state['reservation_resolutions'][0]['id'] = 'other'
            elif change == 'drop':
                del state['reservation_resolutions']
            else:
                state['resources'][0].update(kind='absent', target={'path': str(self.f.f.root / 'late')})
            with self.subTest(change=change), self.assertRaises(c.CompletionError):
                h.publish(self.f.state, state, c.sha(before))
            self.assertEqual(before, self.f.state.read_bytes())

    def test_saved_exact_controls_survive_source_changes_and_detect_snapshot_corruption(self):
        self.f.call('resolve-reservation', *self.inputs())
        state = json.loads(self.f.state.read_bytes())
        (self.f.records / 'actual-tool-result.txt').write_text('Changed unrelated replacement bytes')
        (self.f.records / 'denial-evidence.json').unlink()
        (self.f.records / 'denial-review.json').unlink()
        loaded, _ = h.load_state(self.f.state)
        with c.stored_controls(loaded, self.f.state):
            evidence = c.read_control(loaded['reservation_resolutions'][0]['evidence'])
            self.assertIn(b'rejected before dispatch', c.control_bytes(evidence['source']['path']))
        state['control_records'][-1]['data'] = base64.b64encode(b'corrupted').decode()
        self.f.f.write(self.f.state, state)
        with self.assertRaisesRegex(c.CompletionError, 'digest differs'):
            h.load_state(self.f.state)

    def test_final_review_must_cover_resolution_history(self):
        self.f.call('resolve-reservation', *self.inputs())
        args = self.final_inputs()
        review = json.loads(self.f.f.final_review_path.read_bytes())
        self.assertIn('reservation_resolutions_sha256', review)
        del review['reservation_resolutions_sha256']
        self.f.f.write(self.f.f.final_review_path, review)
        args[-1] = self.f.f.ref(self.f.f.final_review_path)['sha256']
        answer = self.f.call('complete', *args, ok=False)
        self.assertIn('final review', answer['error'])
        self.assertTrue(self.f.state.exists())

    def test_second_denial_appends_and_preserves_each_exact_control_version(self):
        self.reserve('second-denied-attempt')
        self.f.call('resolve-reservation', *self.inputs())
        before = json.loads(self.f.state.read_bytes())
        self.f.call('resolve-reservation', *self.inputs('second-denied-attempt'))
        after, _ = h.load_state(self.f.state)
        self.assertEqual(after['resources'], before['resources'])
        self.assertEqual(after['reservation_resolutions'][:1], before['reservation_resolutions'])
        self.assertEqual(after['control_records'][:len(before['control_records'])], before['control_records'])
        with c.stored_controls(after, self.f.state):
            self.assertEqual(set(c.reservation_resolutions(c.read_control(after['scope']), after)),
                             {'denied-attempt', 'second-denied-attempt'})

    def test_followup_scope_preserves_existing_resolution_and_original_reservation(self):
        self.f.call('resolve-reservation', *self.inputs())
        self.f.call('outcome', '--status', 'waiting', '--basis', str(self.f.f.request),
                    '--basis-sha256', self.f.f.ref(self.f.f.request)['sha256'])
        before = json.loads(self.f.state.read_bytes())
        scope = c.read_control(before['scope'])
        proof = self.f.records / 'followup-proof.json'
        self.f.f.write(proof, {'verified': True})
        scope['checks'].append({'id': 'followup', 'category': 'quality', 'kind': 'evidence',
            'target': {'path': str(proof), 'assertions': [{'path': ['verified'], 'equals': True}]}})
        scope['categories']['quality']['checks'].append('followup')
        scope['categories']['quality']['not_applicable'] = None
        target = self.f.records / 'followup-scope.json'
        self.f.f.write(target, scope)
        normal = self.f.records / 'followup-review.json'
        self.f.f.write(normal, {'scope_sha256': self.f.f.ref(target)['sha256'], 'status': 'No Findings'})
        delta = self.f.records / 'followup-delta.json'
        self.f.f.write(delta, {'previous_scope_sha256': before['scope']['sha256'],
            'scope_sha256': self.f.f.ref(target)['sha256'],
            'authorization_sha256': self.f.f.ref(self.f.f.request)['sha256'],
            'previous_state_sha256': c.sha(self.f.state.read_bytes()), 'status': 'No Findings'})
        args = []
        for name, filename in [('scope', target), ('review', normal),
                               ('authorization', self.f.f.request), ('delta-review', delta)]:
            args.extend(['--' + name, str(filename), '--' + name + '-sha256', self.f.f.ref(filename)['sha256']])
        self.f.call('extend', *args)
        after, _ = h.load_state(self.f.state)
        self.assertEqual(after['resources'], before['resources'])
        self.assertEqual(after['reservation_resolutions'], before['reservation_resolutions'])
        self.assertEqual(len(after['scope_history']), 1)

    def test_nonreserved_resource_cannot_be_resolved_as_not_created(self):
        args = self.inputs()
        resource = self.f.records / 'created-resource.json'
        self.f.f.write(resource, {'kind': 'absent', 'target': {'path': str(self.f.f.root / 'actual-target')}})
        self.f.call('attach', '--id', 'denied-attempt', '--resource', str(resource),
                    '--basis', str(self.f.f.request), '--basis-sha256', self.f.f.ref(self.f.f.request)['sha256'])
        args = self.inputs()
        before = self.f.state.read_bytes()
        self.f.call('resolve-reservation', *args, ok=False)
        self.assertEqual(before, self.f.state.read_bytes())


if __name__ == '__main__':
    unittest.main()
