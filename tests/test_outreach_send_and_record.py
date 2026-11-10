import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.error import HTTPError


def load_component():
    modules = {n: types.ModuleType(n) for n in ('lfx', 'lfx.custom', 'lfx.io', 'lfx.schema')}
    modules['lfx.custom'].Component = type('Component', (), {})
    class Data:
        def __init__(self, data):
            self.data = data
    modules['lfx.schema'].Data = Data
    for name in ('BoolInput', 'MultilineInput', 'Output', 'SecretStrInput', 'StrInput'):
        setattr(modules['lfx.io'], name, lambda **kwargs: kwargs)
    path = Path(__file__).resolve().parents[1] / 'langflow/components/outreach_send_and_record.py'
    spec = importlib.util.spec_from_file_location('sender_under_test', path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


sender = load_component()
USER = '50b93869-6c3f-4344-bc9b-2a62ca2ba4b7'
CAMPAIGN = '1736f580-a789-436a-b636-0f9efcd6966f'
RECIPIENT = '266877cd-44cf-4c73-bab8-8165c043ac3c'
ATTEMPT = '30b3b608-cb6e-4820-9975-44fe5f3f17d3'
TOKEN = '11111111-1111-4111-8111-111111111111'


class FakeDatabase:
    def __init__(self):
        self.lock = threading.Lock()
        self.dispatched = False
        self.outcomes = []
        self.fail_finish = False
        self.job = {'authorized': True, 'campaign_id': CAMPAIGN, 'recipient_id': RECIPIENT,
                    'entity_id': 'entity-A', 'connected_account_id': 'ca_A',
                    'sender_email': 'sender@example.com', 'recipient_email': 'person@example.com',
                    'subject': 'Saved subject', 'body': 'Hi,\n\nSaved text.', 'is_html': False}

    def begin(self, user, attempt, token):
        assert (user, attempt, token) == (USER, ATTEMPT, TOKEN)
        with self.lock:
            if self.dispatched:
                return {'authorized': False, 'attempt_status': self.outcomes[-1]['p_status'] if self.outcomes else 'sending'}
            self.dispatched = True
            return copy.deepcopy(self.job)

    def finish(self, payload):
        if self.fail_finish:
            raise TimeoutError()
        self.outcomes.append(payload)
        return payload['p_status']


class FakeOutlook:
    def __init__(self):
        self.sent = []
        self.response = {'status': 202, 'data': None}
        self.preflight_error = False
        self.send_error = False

    def preflight(self, job):
        if self.preflight_error:
            raise ValueError('Wrong mailbox')

    def send(self, job):
        self.sent.append(copy.deepcopy(job))
        if self.send_error:
            raise TimeoutError()
        return self.response


class SendTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDatabase()
        self.outlook = FakeOutlook()
        self.claim = {'operation': 'recipient_claim', 'claimed': True, 'user_id': USER,
                      'campaign_id': CAMPAIGN, 'recipient_id': RECIPIENT,
                      'attempt_id': ATTEMPT, 'claim_token': TOKEN, 'body': 'tampered input'}

    def execute(self):
        return sender.send_and_record(self.claim, USER, self.db, self.outlook)

    def test_accepted_uses_database_message_and_records_202(self):
        result = self.execute()
        self.assertEqual(result['outcome'], 'submitted')
        self.assertTrue(result['recorded'])
        self.assertEqual(self.outlook.sent[0]['body'], 'Hi,\n\nSaved text.')
        self.assertEqual(self.db.outcomes[0]['p_provider_status_code'], 202)
        self.assertNotIn('claim_token', result)

    def test_replay_never_calls_provider(self):
        self.execute()
        self.assertTrue(self.execute()['send_skipped'])
        self.assertEqual(len(self.outlook.sent), 1)

    def test_concurrent_workers_respect_atomic_gate_contract(self):
        # This tests component behavior with an atomic fake, not PostgreSQL locking.
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.execute(), range(2)))
        self.assertEqual(sum(r.get('send_skipped', False) for r in results), 1)
        self.assertEqual(len(self.outlook.sent), 1)

    def test_preflight_failure_records_failed_without_send(self):
        self.outlook.preflight_error = True
        result = self.execute()
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(self.outlook.sent, [])

    def test_lost_send_response_records_unknown(self):
        self.outlook.send_error = True
        result = self.execute()
        self.assertEqual(result['outcome'], 'unknown')
        self.assertTrue(result['recorded'])
        self.execute()
        self.assertEqual(len(self.outlook.sent), 1)

    def test_only_integer_graph_202_is_acceptance(self):
        for response, expected in (({'status': 403}, 'failed'), ({'status': 429}, 'failed'),
                                   ({'status': 500}, 'unknown'), ({'status': 408}, 'unknown'),
                                   ({'status': 200}, 'unknown'), ({'status_code': 202}, 'unknown'),
                                   ({'status': 202.0}, 'unknown'), ({'status': '202'}, 'unknown')):
            with self.subTest(response=response):
                self.db = FakeDatabase()
                self.outlook.response = response
                self.assertEqual(self.execute()['outcome'], expected)

    def test_record_only_recovery_never_resends(self):
        self.db.fail_finish = True
        result = self.execute()
        self.assertFalse(result['recorded'])
        receipt = result['recovery_receipt']
        self.db.fail_finish = False
        self.assertTrue(sender.recover_record(receipt, USER, self.db)['recorded'])
        self.assertEqual(len(self.outlook.sent), 1)
        self.assertTrue(self.execute()['send_skipped'])

    def test_lost_gate_response_never_calls_provider(self):
        with patch.object(self.db, 'begin', side_effect=TimeoutError()):
            with self.assertRaisesRegex(ValueError, 'gate could not be confirmed'):
                self.execute()
        self.assertEqual(self.outlook.sent, [])

    def test_gate_diagnostic_is_preserved_without_provider_call(self):
        with patch.object(self.db, 'begin', side_effect=sender.TransportError('API request failed (HTTP 404). RPC missing from schema cache.')):
            with self.assertRaisesRegex(ValueError, 'RPC missing from schema cache'):
                self.execute()
        self.assertEqual(self.outlook.sent, [])

    def test_database_http_errors_only_expose_allowlisted_diagnostics(self):
        for body, expected in (({'code': 'PGRST202', 'message': 'private-data'}, 'migration'),
                               ({'code': 'P0001', 'message': 'Claim token mismatch'}, 'Claim token mismatch'),
                               ({'code': 'P0001', 'message': 'private-data'}, 'HTTP 400')):
            transport = sender.Transport()
            error = HTTPError('url', 400, 'private-data', {}, io.BytesIO(json.dumps(body).encode()))
            with patch.object(transport.opener, 'open', side_effect=error):
                with self.assertRaises(sender.TransportError) as caught:
                    transport.request('https://example.supabase.co/rest/v1/rpc/begin_outreach_send', {}, {})
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn('private-data', str(caught.exception))

    def test_invalid_owner_or_unsuccessful_claim_blocks(self):
        self.claim['user_id'] = CAMPAIGN
        with self.assertRaises(ValueError):
            self.execute()
        self.claim['user_id'] = USER
        self.claim['claimed'] = False
        with self.assertRaises(ValueError):
            self.execute()
        self.assertFalse(self.db.dispatched)

    def test_proxy_selects_exact_account_and_plain_text_graph_request(self):
        proxy = sender.OutlookProxy('test-secret')
        with patch.object(proxy.transport, 'request', return_value={'status': 202}) as mock:
            proxy.send(self.db.job)
        url, headers, payload = mock.call_args.args
        self.assertTrue(url.endswith('/api/v3.1/tools/execute/proxy'))
        self.assertEqual(payload['connected_account_id'], 'ca_A')
        self.assertEqual(payload['user_id'], 'entity-A')
        self.assertEqual(payload['endpoint'], 'https://graph.microsoft.com/v1.0/me/sendMail')
        self.assertEqual(payload['body']['message']['body'], {'contentType': 'Text', 'content': 'Hi,\n\nSaved text.'})
        self.assertNotIn('from', payload['body']['message'])

    def test_preflight_rejects_wrong_account_owner_and_mailbox(self):
        proxy = sender.OutlookProxy('secret')
        account = {'id': 'ca_A', 'user_id': 'entity-A', 'status': 'ACTIVE',
                   'toolkit': {'slug': 'outlook'}, 'is_disabled': False}
        profile = {'status': 200, 'data': {'mail': 'sender@example.com'}}
        with patch.object(proxy.transport, 'request', side_effect=[account, profile]):
            proxy.preflight(self.db.job)
        for bad in ({**account, 'user_id': 'entity-B'}, {**account, 'status': 'EXPIRED'}):
            with patch.object(proxy.transport, 'request', return_value=bad), self.assertRaises(ValueError):
                proxy.preflight(self.db.job)
        with patch.object(proxy.transport, 'request', side_effect=[account, {'status': 200, 'data': {'mail': 'other@example.com'}}]):
            with self.assertRaisesRegex(ValueError, 'differs'):
                proxy.preflight(self.db.job)

    def test_database_rpc_contracts(self):
        db = sender.SendDatabase('https://example.supabase.co', 'sb_secret_test')
        with patch.object(db.transport, 'request', return_value={'authorized': False}) as mock:
            db.begin(USER, ATTEMPT, TOKEN)
        url, headers, payload = mock.call_args.args
        self.assertTrue(url.endswith('/rpc/begin_outreach_send'))
        self.assertNotIn('Authorization', headers)
        self.assertEqual(payload, {'p_user_id': USER, 'p_attempt_id': ATTEMPT, 'p_claim_token': TOKEN})

    def test_preflight_transport_errors_identify_stage(self):
        proxy = sender.OutlookProxy('secret')
        account = {'id': 'ca_A', 'user_id': 'entity-A', 'status': 'ACTIVE',
                   'toolkit': {'slug': 'outlook'}, 'is_disabled': False}
        error = sender.TransportError('API request failed (HTTP 404).')
        with patch.object(proxy.transport, 'request', side_effect=error):
            with self.assertRaisesRegex(ValueError, 'connected-account lookup failed'):
                proxy.preflight(self.db.job)
        with patch.object(proxy.transport, 'request', side_effect=[account, error]):
            with self.assertRaisesRegex(ValueError, 'mailbox proxy request failed'):
                proxy.preflight(self.db.job)

    def test_transport_never_retries_and_blocks_redirects(self):
        transport = sender.Transport()
        with patch.object(transport.opener, 'open', side_effect=TimeoutError()) as mock:
            with self.assertRaises(sender.TransportError):
                transport.request('https://example.com', {}, {})
            self.assertEqual(mock.call_count, 1)
        self.assertIsNone(sender.NoRedirects().redirect_request(None, None, 302, '', {}, 'https://other.com'))

    def test_record_only_component_does_not_initialize_composio(self):
        self.db.fail_finish = True
        receipt = self.execute()['recovery_receipt']
        self.db.fail_finish = False
        node = sender.OutreachSendAndRecord()
        node.record_only = True
        node.recovery_json = json.dumps(receipt)
        node.trusted_user_id = USER
        node.supabase_url = 'https://example.supabase.co'
        node.supabase_key = 'secret'
        with patch.object(sender, 'SendDatabase', return_value=self.db), patch.object(sender, 'OutlookProxy') as mock:
            self.assertTrue(node.execute_attempt().data['recorded'])
            mock.assert_not_called()

    def test_preflight_only_component_never_touches_database_or_sends(self):
        node = sender.OutreachSendAndRecord()
        node.preflight_only = True
        node.record_only = False
        node.claim_json = json.dumps({**self.claim, **{key: self.db.job[key] for key in
                                                     ('entity_id', 'connected_account_id', 'sender_email')}})
        node.trusted_user_id = USER
        node.composio_key = 'secret'
        self.outlook.preflight_error = True
        with patch.object(sender, 'SendDatabase') as db, patch.object(sender, 'OutlookProxy', return_value=self.outlook):
            result = node.execute_attempt().data
            db.assert_not_called()
        self.assertFalse(result['preflight_passed'])
        self.assertEqual(result['diagnostic'], 'Wrong mailbox')
        self.assertFalse(result['send_attempted'])
        self.assertEqual(self.outlook.sent, [])

    def test_gate_migration_parses_when_parser_available(self):
        try:
            from pglast import parse_sql
        except ImportError:
            self.skipTest('pglast is not installed')
        path = Path(__file__).resolve().parents[1] / 'supabase/migrations/202611100002_outreach_send_gate.sql'
        self.assertEqual(len(parse_sql(path.read_text())), 6)


if __name__ == '__main__':
    unittest.main()
