"""Standalone one-attempt sender. No HTTP retries; database gate prevents replay."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from lfx.custom import Component
from lfx.io import BoolInput, MultilineInput, Output, SecretStrInput, StrInput
from lfx.schema import Data


class TransportError(ValueError):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def secret(value):
    result = value.get_secret_value() if hasattr(value, 'get_secret_value') else str(value or '')
    if not result.strip():
        raise ValueError('Required API key is missing.')
    return result


class Transport:
    def __init__(self):
        self.opener = build_opener(NoRedirects())

    def request(self, url, headers, payload=None):
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode('utf-8')
        headers = {**headers, 'Accept': 'application/json', 'Content-Type': 'application/json'}
        try:
            with self.opener.open(Request(url, data=data, headers=headers), timeout=120) as response:
                return json.load(response)
        except HTTPError as exc:
            detail = ''
            # Only expose known database diagnostics, never arbitrary response bodies.
            if '/rest/v1/rpc/' in url:
                try:
                    body = json.loads(exc.read(8192))
                    code = body.get('code') if isinstance(body, dict) else None
                    hints = {
                        'PGRST202': 'RPC missing from schema cache. Check that the send-gate migration was applied.',
                        '42883': 'Required database function is missing. Check the migrations.',
                        '42703': 'Required database column is missing. Check the migrations.',
                        '42501': 'Database permission denied. Check the backend key and RPC grants.',
                        'PGRST301': 'Database authentication failed. Check the backend key.',
                    }
                    if code in hints:
                        detail = ' ' + hints[code]
                    elif code == 'P0001' and body.get('message') in (
                        'Attempt unavailable', 'Claim token mismatch', 'Recipient is suppressed',
                        'Reviewed sender is no longer available',
                        'Campaign is not an eligible plain-text submission',
                    ):
                        detail = ' ' + body['message'] + '.'
                except (ValueError, OSError):
                    pass
            raise TransportError(f'API request failed (HTTP {exc.code}).{detail}') from None
        except (URLError, OSError, TimeoutError, ValueError):
            raise TransportError('API response unavailable or invalid; request was not retried.') from None


class SendDatabase:
    def __init__(self, url, key):
        url = str(url).strip()
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('Supabase URL must be an HTTPS project origin.')
        self.url = url.rstrip('/')
        key = secret(key)
        self.headers = {'apikey': key}
        if not key.startswith('sb_secret_'):
            self.headers['Authorization'] = f'Bearer {key}'
        self.transport = Transport()

    def rpc(self, name, payload):
        return self.transport.request(f'{self.url}/rest/v1/rpc/{name}', self.headers, payload)

    def begin(self, user_id, attempt_id, token):
        return self.rpc('begin_outreach_send', {
            'p_user_id': user_id, 'p_attempt_id': attempt_id, 'p_claim_token': token,
        })

    def finish(self, payload):
        return self.rpc('finish_outreach_attempt', payload)


class OutlookProxy:
    # Live-verified with the corrected Composio project API key.
    BASE = 'https://backend.composio.dev/api/v3.1'

    def __init__(self, key):
        self.headers = {'x-api-key': secret(key)}
        self.transport = Transport()

    def proxy(self, job, method, endpoint, body=None):
        payload = {'connected_account_id': job['connected_account_id'], 'user_id': job['entity_id'],
                   'method': method, 'endpoint': endpoint}
        if body is not None:
            payload['body'] = body
        return self.transport.request(self.BASE + '/tools/execute/proxy', self.headers, payload)

    def preflight(self, job):
        try:
            account = self.transport.request(
                self.BASE + '/connected_accounts/' + quote(job['connected_account_id'], safe=''), self.headers)
        except TransportError as exc:
            raise ValueError(
                'Composio connected-account lookup failed (GET /api/v3.1/connected_accounts/{id}). '
                f'{exc} Verify API version, account ID, and that the API key belongs to the account project.'
            ) from None
        if not isinstance(account, dict) or account.get('id') != job['connected_account_id']:
            raise ValueError('Connected account ID could not be verified.')
        if not account.get('user_id'):
            raise ValueError('Composio omitted account user_id; ownership verification needs adapting.')
        if account.get('user_id') != job['entity_id']:
            raise ValueError('Composio account owner differs from the saved entity ID.')
        if account.get('status') != 'ACTIVE':
            raise ValueError('Composio connected account is not ACTIVE.')
        toolkit = account.get('toolkit')
        if not isinstance(toolkit, dict) or toolkit.get('slug') != 'outlook':
            raise ValueError('Connected account toolkit is not outlook.')
        if account.get('is_disabled') is not False:
            raise ValueError('Connected account is disabled or its enabled state is unverified.')
        try:
            profile = self.proxy(job, 'GET', 'https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName')
        except TransportError as exc:
            raise ValueError(
                'Composio mailbox proxy request failed (POST /api/v3.1/tools/execute/proxy). '
                f'{exc} Connected-account metadata checks passed; no sendMail call was made.'
            ) from None
        if not isinstance(profile, dict) or profile.get('status') != 200 or not isinstance(profile.get('data'), dict):
            status = profile.get('status') if isinstance(profile, dict) else None
            suffix = f' (Graph HTTP {status})' if type(status) is int else ''
            raise ValueError('Outlook mailbox verification failed' + suffix + '. Check profile-read permission.')
        mailbox = profile['data'].get('mail') or profile['data'].get('userPrincipalName')
        if not isinstance(mailbox, str) or mailbox.strip().lower() != job['sender_email'].strip().lower():
            raise ValueError('Authenticated Outlook mailbox differs from the reviewed sender.')

    def send(self, job):
        return self.proxy(job, 'POST', 'https://graph.microsoft.com/v1.0/me/sendMail', {
            'message': {'subject': job['subject'], 'body': {'contentType': 'Text', 'content': job['body']},
                        'toRecipients': [{'emailAddress': {'address': job['recipient_email']}}]},
            'saveToSentItems': True,
        })


def uuid_value(value, label):
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f'{label} must be a UUID.') from None


def parse_object(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON keys are not allowed.')
            result[key] = value
        return result
    try:
        result = json.loads(text, object_pairs_hook=pairs)
    except (ValueError, TypeError):
        raise ValueError('Paste one valid JSON object.') from None
    if not isinstance(result, dict):
        raise ValueError('Expected a JSON object.')
    return result


def record_result(database, receipt):
    payload = {key: receipt[key] for key in (
        'p_user_id', 'p_attempt_id', 'p_claim_token', 'p_status',
        'p_provider_status_code', 'p_provider_message_id', 'p_error')}
    recorded = False
    try:
        recorded = database.finish(payload) == receipt['p_status']
    except Exception:
        # A committed finish can also lose its response. Retry only finish, never send.
        pass
    result = {'operation': 'outreach_send_result', 'attempt_id': receipt['p_attempt_id'],
              'outcome': receipt['p_status'], 'provider_status_code': receipt['p_provider_status_code'],
              'recorded': recorded}
    if not recorded:
        result['recovery_receipt'] = payload
        result['next_action'] = 'Use Record Only with this recovery receipt. Never resend this attempt.'
    return result


def send_and_record(claim, trusted_user_id, database, outlook):
    user_id = uuid_value(trusted_user_id, 'Trusted User ID')
    if claim.get('operation') != 'recipient_claim' or claim.get('claimed') is not True:
        raise ValueError('A successful saved recipient claim is required.')
    if uuid_value(claim.get('user_id'), 'Claim user ID') != user_id:
        raise ValueError('Claim owner differs from the trusted user.')
    attempt_id = uuid_value(claim.get('attempt_id'), 'Attempt ID')
    token = uuid_value(claim.get('claim_token'), 'Claim token')
    campaign_id = uuid_value(claim.get('campaign_id'), 'Campaign ID')
    recipient_id = uuid_value(claim.get('recipient_id'), 'Recipient ID')
    try:
        job = database.begin(user_id, attempt_id, token)
    except TransportError as exc:
        raise ValueError(f'Send gate could not be confirmed. {exc} No provider call was made by this execution. Inspect the attempt; do not reset its dispatch marker.') from None
    except Exception:
        raise ValueError('Send gate could not be confirmed. No provider call was made by this execution. Inspect the attempt; do not reset its dispatch marker.') from None
    if not isinstance(job, dict) or type(job.get('authorized')) is not bool:
        raise ValueError('Invalid send gate response. No provider call was made; inspect the attempt.')
    if not job['authorized']:
        return {'operation': 'outreach_send_result', 'attempt_id': attempt_id,
                'send_skipped': True, 'attempt_status': job.get('attempt_status'),
                'reason': 'Attempt already dispatched or finalized; no provider call made.'}
    receipt = {'p_user_id': user_id, 'p_attempt_id': attempt_id, 'p_claim_token': token,
               'p_status': 'failed', 'p_provider_status_code': None,
               'p_provider_message_id': None, 'p_error': None}
    try:
        if job.get('campaign_id') != campaign_id or job.get('recipient_id') != recipient_id or job.get('is_html') is not False:
            raise ValueError('Send gate returned mismatched identifiers.')
        for key in ('entity_id', 'connected_account_id', 'sender_email', 'recipient_email', 'subject', 'body'):
            if not isinstance(job.get(key), str) or not job[key].strip():
                raise ValueError('Incomplete stored message.')
        outlook.preflight(job)
    except Exception:
        receipt['p_error'] = 'Preflight failed before sendMail; no email send was attempted.'
        return record_result(database, receipt)
    # From this point, any lost or unexpected response is uncertain, not retryable.
    receipt.update(p_status='unknown', p_error='Send response unavailable or unrecognized; do not resend.')
    try:
        response = outlook.send(job)
        status = response.get('status') if isinstance(response, dict) else None
        if type(status) is int and 100 <= status <= 599:
            receipt['p_provider_status_code'] = status
        if type(status) is int and status == 202:
            receipt.update(p_status='submitted', p_error=None)
        elif type(status) is int and 400 <= status < 500 and status != 408:
            receipt.update(p_status='failed', p_error=f'Microsoft Graph rejected sendMail (HTTP {status}).')
    except Exception:
        pass
    return record_result(database, receipt)


def recover_record(receipt, trusted_user_id, database):
    user_id = uuid_value(trusted_user_id, 'Trusted User ID')
    if receipt.get('p_user_id') != user_id:
        raise ValueError('Recovery receipt owner differs from the trusted user.')
    for field in ('p_attempt_id', 'p_claim_token'):
        uuid_value(receipt.get(field), field)
    if receipt.get('p_status') not in ('submitted', 'failed', 'unknown'):
        raise ValueError('Invalid recovery outcome.')
    code = receipt.get('p_provider_status_code')
    if code is not None and (type(code) is not int or not 100 <= code <= 599):
        raise ValueError('Invalid provider status code.')
    if receipt['p_status'] == 'submitted' and code != 202:
        raise ValueError('Submitted requires verified Outlook 202 evidence.')
    for field in ('p_provider_message_id', 'p_error'):
        if field not in receipt or (receipt[field] is not None and not isinstance(receipt[field], str)):
            raise ValueError('Incomplete recovery receipt.')
    return record_result(database, receipt)


def diagnose_preflight(claim, trusted_user_id, outlook):
    if claim.get('operation') != 'recipient_claim' or claim.get('claimed') is not True:
        raise ValueError('Use the original successful saved claim for diagnosis.')
    if uuid_value(claim.get('user_id'), 'Claim user ID') != uuid_value(trusted_user_id, 'Trusted User ID'):
        raise ValueError('Claim owner differs from the trusted user.')
    for key in ('entity_id', 'connected_account_id', 'sender_email'):
        if not isinstance(claim.get(key), str) or not claim[key].strip():
            raise ValueError('Saved claim lacks sender metadata.')
    result = {'operation': 'outreach_preflight_diagnostic', 'send_attempted': False,
              'database_modified': False}
    try:
        outlook.preflight(claim)
    except ValueError as exc:
        # OutlookProxy emits only local messages and sanitized transport errors.
        return {**result, 'preflight_passed': False, 'diagnostic': str(exc)}
    return {**result, 'preflight_passed': True,
            'diagnostic': 'Current account and mailbox checks passed. This does not reopen the failed attempt.'}


class OutreachSendAndRecord(Component):
    display_name = 'Send & Record Outreach Attempt'
    description = 'One-time Outlook send through Composio, with record-only recovery.'
    icon = 'Send'
    name = 'OutreachSendAndRecord'
    inputs = [
        MultilineInput(name='claim_json', display_name='Saved Claim JSON', value='',
                       info='Paste the original successful claim for this isolated test; do not rerun upstream nodes.'),
        StrInput(name='trusted_user_id', display_name='Trusted User ID', required=True),
        StrInput(name='supabase_url', display_name='Supabase Project URL', required=True),
        SecretStrInput(name='supabase_key', display_name='Supabase Backend Key', required=True),
        SecretStrInput(name='composio_key', display_name='Composio API Key'),
        BoolInput(name='record_only', display_name='Record Only', value=False),
        BoolInput(name='preflight_only', display_name='Preflight Only (No Send)', value=False,
                  info='Read-only account/mailbox diagnosis using the saved claim. No database changes or sendMail call.'),
        MultilineInput(name='recovery_json', display_name='Recovery Receipt JSON', value='', advanced=True),
    ]
    outputs = [Output(name='send_result', display_name='Send Result', method='execute_attempt')]

    def execute_attempt(self) -> Data:
        if getattr(self, 'preflight_only', False):
            if self.record_only:
                raise ValueError('Choose either Preflight Only or Record Only.')
            result = diagnose_preflight(parse_object(self.claim_json), self.trusted_user_id,
                                        OutlookProxy(self.composio_key))
            self.status = result['diagnostic']
            return Data(data=result)
        database = SendDatabase(self.supabase_url, self.supabase_key)
        if self.record_only:
            result = recover_record(parse_object(self.recovery_json), self.trusted_user_id, database)
        else:
            result = send_and_record(parse_object(self.claim_json), self.trusted_user_id,
                                     database, OutlookProxy(self.composio_key))
        self.status = 'Send skipped' if result.get('send_skipped') else f"{result['outcome']}; recorded={result['recorded']}"
        return Data(data=result)
