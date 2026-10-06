"""Offline migration checks; provider HTTP and database transactions are simulated.

Run: python -m unittest discover -s tests -p 'test_yookassa_dual_shop.py' -v
No credentials, paid operations, network access or external packages are needed.
"""
import ast
import asyncio
import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import threading
import time
import types
import unittest
import urllib.parse
from unittest.mock import AsyncMock, Mock, patch
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Block real services at the import boundary, even on a configured developer host.
db = types.ModuleType('billing_db')
db.supabase = None
db.resolve_billing_user_id = lambda uid: uid
plans = types.ModuleType('subscriptions_db')
plans.get_subscription_plan = lambda code: {'duration_days': 30}
http = types.ModuleType('httpx')
http.AsyncClient = Mock(side_effect=AssertionError('Unexpected network access'))
with patch.dict(sys.modules, billing_db=db, subscriptions_db=plans, httpx=http):
    import yookassa_accounts as accounts
    import yookassa_flow as flow
    import yookassa_recovery as recovery
    import yookassa_store as store

# Also cover lazy imports made while checking linked tester identities.
_service_boundary = patch.dict(sys.modules, billing_db=db, subscriptions_db=plans, httpx=http)


def setUpModule():
    _service_boundary.start()


def tearDownModule():
    _service_boundary.stop()


ENV = {
    'YOOKASSA_SHOP_ID': '10001', 'YOOKASSA_SECRET_KEY': 'live_fake_old',
    'YOOKASSA_NEW_SHOP_ID': '1485446', 'YOOKASSA_NEW_SECRET_KEY': 'live_fake_new',
    'YOOKASSA_NEW_TAX_SYSTEM_CODE': '2', 'YOOKASSA_NEW_VAT_CODE': '1',
    'YOOKASSA_NEW_MODE': 'testers', 'YOOKASSA_NEW_TEST_USER_IDS': '42',
}


def provider(account='new', pid='payment-1', **changes):
    result = {
        'id': pid, 'status': 'succeeded', 'paid': True, 'test': False,
        'recipient': {'account_id': accounts.get_shop(account).shop_id},
        'amount': {'value': '100.00', 'currency': 'RUB'},
        'metadata': {'user_id': 42, 'tokens': 10, 'payment_type': 'topup', **accounts.get_shop(account).metadata()},
        'confirmation': {'confirmation_url': 'https://checkout.example.invalid/pay'},
    }
    result.update(changes)
    return result


def row(account='new', **changes):
    result = {
        'payment_id': 'payment-1', 'user_id': 42, 'tokens': 10, 'amount_rub': 100,
        'payment_type': 'topup', 'plan_code': '', 'duration_days': 0, 'state': 'pending',
        'metadata': accounts.get_shop(account).metadata(),
    }
    result.update(changes)
    return result


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True); self.env.start(); self.addCleanup(self.env.stop)

    def test_default_off_does_not_expose_new_shop(self):
        os.environ.pop('YOOKASSA_NEW_MODE')
        self.assertEqual([m['id'] for m in accounts.payment_methods(42, authenticated=True)['methods']], ['legacy'])

    def test_testers_require_authenticated_identity(self):
        self.assertEqual(len(accounts.payment_methods(42, authenticated=True)['methods']), 2)
        self.assertEqual(len(accounts.payment_methods(42, authenticated=False)['methods']), 1)
        self.assertEqual(len(accounts.payment_methods(43, authenticated=True)['methods']), 1)
        with self.assertRaises(ValueError): accounts.checkout_shop('new', 42, authenticated=False)

    def test_authenticated_linked_account_can_be_tester(self):
        with patch.dict(sys.modules, billing_db=db), patch.object(db, 'resolve_billing_user_id', return_value=42):
            self.assertEqual(accounts.checkout_shop('new', 500, authenticated=True).account, 'new')

    def test_switch_and_rollback_keep_legacy_credentials(self):
        os.environ.update(YOOKASSA_NEW_MODE='all', YOOKASSA_DEFAULT_ACCOUNT='new', YOOKASSA_LEGACY_CHECKOUT_ENABLED='false')
        self.assertEqual(accounts.checkout_shop(None, 77, authenticated=True).account, 'new')
        with self.assertRaises(ValueError): accounts.checkout_shop('legacy', 77, authenticated=True)
        accounts.get_shop('legacy').require_credentials()
        os.environ.update(YOOKASSA_NEW_MODE='off', YOOKASSA_DEFAULT_ACCOUNT='legacy', YOOKASSA_LEGACY_CHECKOUT_ENABLED='true')
        self.assertEqual(accounts.checkout_shop(None, 77, authenticated=True).account, 'legacy')
        accounts.get_shop('new').require_credentials()

    def test_tax_settings_independent_and_explicit_for_new(self):
        os.environ['YOOKASSA_TAX_SYSTEM_CODE'] = ''
        self.assertEqual(accounts.get_shop('legacy').tax_system_code(), 6)
        self.assertEqual(accounts.get_shop('new').tax_system_code(), 2)
        self.assertEqual(accounts.get_shop('new').vat_code(), 1)
        os.environ.pop('YOOKASSA_NEW_TAX_SYSTEM_CODE')
        with self.assertRaises(RuntimeError): accounts.get_shop('new').tax_system_code()
        self.assertEqual(accounts.get_shop('legacy').tax_system_code(), 6)

    def test_bad_account_equal_shops_and_test_keys_rejected(self):
        with self.assertRaises(ValueError): accounts.get_shop('unknown')
        os.environ['YOOKASSA_NEW_SHOP_ID'] = '10001'
        with self.assertRaises(RuntimeError): accounts.checkout_shop('new', 42, authenticated=True)
        os.environ['YOOKASSA_NEW_SHOP_ID'] = '1485446'
        os.environ['YOOKASSA_NEW_SECRET_KEY'] = 'test_fake'
        with self.assertRaises(RuntimeError): accounts.checkout_shop('new', 42, authenticated=True)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True); self.env.start(); self.addCleanup(self.env.stop)

    async def test_create_both_shops_sends_correct_auth_receipt_and_durable_metadata(self):
        for name, tax in [('legacy', 6), ('new', 2)]:
            with self.subTest(account=name):
                response = Mock(status_code=200); response.json.return_value = provider(name)
                client = AsyncMock(); client.__aenter__.return_value = client; client.post.return_value = response
                with patch.object(flow.httpx, 'AsyncClient', return_value=client), patch.object(flow, 'record_yookassa_payment_intent') as save:
                    result = await flow.create_yookassa_payment(amount_rub=100, description='Test', user_id=42, tokens=10,
                        customer_email='tester@example.invalid', payment_account=name, authenticated_user=True,
                        payment_metadata={'yookassa_account': 'evil', 'product_id': 'other'}, idempotence_key='stable-order')
                body = client.post.call_args.kwargs['json']; headers = client.post.call_args.kwargs['headers']
                self.assertEqual(base64.b64decode(headers['Authorization'].split()[1]).decode(),
                    accounts.get_shop(name).shop_id + ':' + accounts.get_shop(name).secret_key)
                self.assertEqual(headers['Idempotence-Key'], 'stable-order')
                self.assertEqual(body['receipt']['tax_system_code'], tax)
                self.assertEqual(body['receipt']['items'][0]['vat_code'], 1)
                self.assertEqual(body['metadata']['yookassa_account'], name)
                self.assertEqual(body['metadata']['product_id'], 'nabex')
                self.assertEqual(save.call_args.kwargs['metadata']['yookassa_shop_id'], accounts.get_shop(name).shop_id)
                self.assertEqual(result[0], 'payment-1')

    async def test_unauthorized_new_checkout_never_contacts_provider(self):
        with patch.object(flow.httpx, 'AsyncClient') as client:
            with self.assertRaises(ValueError):
                await flow.create_yookassa_payment(amount_rub=100, description='Test', user_id=42, tokens=10,
                    customer_email='test@example.invalid', payment_account='new', authenticated_user=False)
            client.assert_not_called()

    async def test_no_confirmation_link_returned_when_durable_save_fails(self):
        response = Mock(status_code=200); response.json.return_value = provider()
        client = AsyncMock(); client.__aenter__.return_value = client; client.post.return_value = response
        with patch.object(flow.httpx, 'AsyncClient', return_value=client), patch.object(flow, 'record_yookassa_payment_intent', side_effect=RuntimeError('db unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'recovery state was not saved'):
                await flow.create_yookassa_payment(amount_rub=100, description='Test', user_id=42, tokens=10,
                    customer_email='test@example.invalid', payment_account='new', authenticated_user=True)

    async def test_fetch_uses_original_shop_even_when_checkouts_disabled(self):
        os.environ.update(YOOKASSA_NEW_MODE='off', YOOKASSA_LEGACY_CHECKOUT_ENABLED='false')
        for name in ['legacy', 'new']:
            response = Mock(status_code=200); response.json.return_value = provider(name)
            client = AsyncMock(); client.__aenter__.return_value = client; client.get.return_value = response
            with patch.object(flow.httpx, 'AsyncClient', return_value=client):
                await flow.fetch_yookassa_payment('payment-1', account=name)
            auth = client.get.call_args.kwargs['headers']['Authorization']
            self.assertTrue(base64.b64decode(auth.split()[1]).decode().startswith(accounts.get_shop(name).shop_id + ':'))


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True); self.env.start(); self.addCleanup(self.env.stop)
        self.intent = row(); self.payment = provider(); self.credits = {}; self.applications = set(); self.saved = []
        self.guard = threading.RLock(); self.fail_credit = False
        self.fetch = AsyncMock(side_effect=lambda *a, **kw: copy.deepcopy(self.payment))
        patches = {
            'fetch_yookassa_payment': self.fetch,
            'get_yookassa_payment_intent': self.get_intent,
            'record_yookassa_payment_intent': self.save,
            'claim_yookassa_payment': self.claim,
            'update_yookassa_provider_status': self.status,
            'mark_yookassa_payment_applied': lambda *a, **kw: self.status(None, 'succeeded', state='applied'),
            'release_yookassa_payment': lambda *a: self.status(None, 'succeeded', state='pending'),
            'credit_tokens_once': self.credit,
            'claim_yookassa_subscription_user_lock': lambda *a, **kw: {'claimed': True},
            'release_yookassa_subscription_user_lock': lambda *a: None,
            'apply_yookassa_subscription_once': self.subscription,
        }
        for name, value in patches.items():
            p = patch.object(recovery, name, value); p.start(); self.addCleanup(p.stop)

    def get_intent(self, *args):
        with self.guard: return copy.deepcopy(self.intent)

    def save(self, **kwargs):
        with self.guard:
            self.saved.append(kwargs); self.intent = dict(kwargs, state='pending'); return copy.deepcopy(self.intent)

    def claim(self, *args, **kwargs):
        with self.guard:
            if self.intent['state'] in ['applied', 'processing']: return {'claimed': False}
            self.intent['state'] = 'processing'; return {'claimed': True}

    def status(self, pid, provider_status, *, state):
        with self.guard: self.intent.update(state=state, provider_status=provider_status)

    def credit(self, uid, tokens, **kwargs):
        with self.guard:
            if self.fail_credit: self.fail_credit = False; raise RuntimeError('simulated transient credit failure')
            key = kwargs['ref_id']; fresh = key not in self.credits
            self.credits[key] = tokens
            return {'credited': fresh, 'balance_tokens': sum(self.credits.values())}

    def subscription(self, uid, pid, **kwargs):
        with self.guard:
            fresh = pid not in self.applications; self.applications.add(pid); return {'applied': fresh}

    async def run_payment(self, **kwargs):
        return await recovery.reconcile_yookassa_payment('payment-1', **kwargs)

    async def test_old_untagged_intent_stays_legacy_after_switch(self):
        self.intent = row('legacy', metadata={}); self.payment = provider('legacy')
        self.payment['metadata'] = {'user_id': 42, 'tokens': 10}
        os.environ.update(YOOKASSA_DEFAULT_ACCOUNT='new', YOOKASSA_NEW_MODE='all', YOOKASSA_LEGACY_CHECKOUT_ENABLED='false')
        result = await self.run_payment()
        self.assertEqual(self.fetch.call_args.kwargs['account'], 'legacy')
        self.assertEqual(result['status'], 'applied')
        self.assertIn(str(uuid5(NAMESPACE_URL, 'nabex:yookassa:payment-1')), self.credits)

    async def test_new_intent_completes_after_new_checkout_rollback(self):
        os.environ['YOOKASSA_NEW_MODE'] = 'off'
        self.assertEqual((await self.run_payment())['status'], 'applied')
        self.assertEqual(self.fetch.call_args.kwargs['account'], 'new')

    async def test_duplicate_and_concurrent_notifications_credit_once(self):
        await asyncio.gather(*(self.run_payment() for _ in range(8)))
        result = await self.run_payment()
        self.assertEqual(sum(self.credits.values()), 10)
        self.assertFalse(result['newly_applied'])

    async def test_wrong_webhook_account_rejected_before_fetch(self):
        with self.assertRaises(RuntimeError): await self.run_payment(expected_account='legacy')
        self.fetch.assert_not_called(); self.assertFalse(self.credits)

    async def test_wrong_customer_rejected_before_fetch(self):
        with self.assertRaises(PermissionError): await self.run_payment(expected_user_id=99)
        self.fetch.assert_not_called(); self.assertFalse(self.credits)

    async def test_test_payments_and_corrupt_proofs_never_credit(self):
        mutations = [
            {'test': True}, {'test': 'false'}, {'test': None}, {'id': 'wrong'},
            {'recipient': {'account_id': '10001'}}, {'recipient': {}},
            {'amount': {'value': '99.00', 'currency': 'RUB'}},
            {'amount': {'value': '100.00', 'currency': 'USD'}}, {'amount': {}},
            {'metadata': dict(self.payment['metadata'], product_id='mamasmile_pro')},
            {'metadata': dict(self.payment['metadata'], yookassa_account='legacy')},
            {'metadata': dict(self.payment['metadata'], tokens=999)},
            {'metadata': dict(self.payment['metadata'], user_id=99)},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.payment = provider(**mutation)
                with self.assertRaises(RuntimeError): await self.run_payment()
                self.assertFalse(self.credits)

    async def test_pending_canceled_and_not_paid_never_credit(self):
        for change, expected in [({'status': 'pending', 'paid': False}, 'pending'), ({'status': 'canceled', 'paid': False}, 'canceled'), ({'paid': 'true'}, 'pending')]:
            self.intent = row(); self.payment = provider(**change)
            self.assertEqual((await self.run_payment())['status'], expected)
            self.assertFalse(self.credits)

    async def test_missing_new_intent_recovers_from_verified_new_webhook(self):
        self.intent = None
        result = await self.run_payment(expected_account='new')
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.saved[0]['metadata']['yookassa_shop_id'], '1485446')

    async def test_unknown_payment_for_wrong_user_does_not_create_intent(self):
        self.intent = None
        with self.assertRaises(PermissionError): await self.run_payment(expected_account='new', expected_user_id=99)
        self.assertFalse(self.saved)

    async def test_changed_shop_configuration_does_not_rebind_saved_payment(self):
        os.environ['YOOKASSA_NEW_SHOP_ID'] = '999'
        with self.assertRaises(RuntimeError): await self.run_payment()
        self.fetch.assert_not_called()

    async def test_subscription_retry_after_partial_failure_does_not_extend_twice(self):
        self.intent.update(payment_type='subscription', plan_code='spark', duration_days=30)
        self.payment['metadata'].update(payment_type='subscription', plan_code='spark', duration_days=30)
        self.fail_credit = True
        with self.assertRaises(RuntimeError): await self.run_payment()
        self.assertEqual(self.intent['state'], 'pending')
        self.assertEqual((await self.run_payment())['status'], 'applied')
        self.assertEqual(len(self.applications), 1); self.assertEqual(sum(self.credits.values()), 10)

    async def test_subscription_metadata_mismatch_rejected(self):
        self.intent.update(payment_type='subscription', plan_code='spark', duration_days=30)
        self.payment['metadata'].update(payment_type='subscription', plan_code='nexus', duration_days=30)
        with self.assertRaises(RuntimeError): await self.run_payment()
        self.assertFalse(self.applications); self.assertFalse(self.credits)


class StoreTests(unittest.TestCase):
    def test_insert_race_cannot_change_shop_or_purchase(self):
        fake = Mock()
        fake.table.return_value.insert.return_value.execute.side_effect = RuntimeError('duplicate')
        with patch.dict(os.environ, ENV, clear=True):
            existing = row(state='applied')
            values = dict(existing)
            values.pop('state')
            with patch.object(store, 'supabase', fake), patch.object(store, 'get_yookassa_payment_intent', return_value=existing):
                self.assertEqual(store.record_yookassa_payment_intent(**values)['state'], 'applied')
                for key, bad in [('metadata', accounts.get_shop('legacy').metadata()), ('tokens', 20), ('plan_code', 'nexus')]:
                    with self.subTest(key=key), self.assertRaises(RuntimeError):
                        store.record_yookassa_payment_intent(**dict(values, **{key: bad}))


def extract_functions(names, scope, filename='main.py'):
    tree = ast.parse((ROOT / filename).read_text())
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []; selected.append(node)
    exec(compile(ast.Module(body=selected, type_ignores=[]), 'main.py', 'exec'), scope)
    return scope


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_checkout_routes_forward_authenticated_choice(self):
        from typing import Any, Dict
        class HTTPException(Exception):
            def __init__(self, status_code, detail): self.status_code = status_code; super().__init__(detail)
        async def create(**kwargs):
            accounts.checkout_shop(kwargs['payment_account'], kwargs['user_id'], authenticated=kwargs['authenticated_user'])
            return 'payment-1', 'https://checkout.example.invalid/pay'
        call = AsyncMock(side_effect=create)
        scope = extract_functions({'workspace_topup_create', 'workspace_subscription_create'}, dict(
            WorkspaceTopupCreatePayload=object, WorkspaceSubscriptionCreatePayload=object, Dict=Dict, Any=Any,
            Depends=lambda f: None, get_current_workspace_user=lambda: None, HTTPException=HTTPException,
            ensure_workspace_account_from_claims=lambda user: {'id': 42, 'email': 'test@example.invalid'},
            _workspace_find_topup_pack=lambda tokens: {'tokens': 10, 'rub': 100},
            _workspace_public_subscription_plan=lambda code: {'price_rub': 100, 'tokens': 10, 'duration_days': 30},
            get_current_subscription=lambda uid: {}, create_yookassa_payment=call,
        ), 'app/routers/web_workspace_api.py')
        payload = types.SimpleNamespace(tokens=10, plan_code='spark', return_url=None, payment_account='new')
        with patch.dict(os.environ, ENV, clear=True):
            for name in ['workspace_topup_create', 'workspace_subscription_create']:
                result = await scope[name](payload, user={})
                self.assertTrue(result['ok']); self.assertTrue(call.call_args.kwargs['authenticated_user'])
                self.assertEqual(call.call_args.kwargs['payment_account'], 'new')
            os.environ['YOOKASSA_NEW_MODE'] = 'off'
            with self.assertRaises(HTTPException) as error: await scope['workspace_topup_create'](payload, user={})
            self.assertEqual(error.exception.status_code, 400)

    async def test_tg_checkout_routes_cannot_grant_new_shop_using_fallback_uid(self):
        import re
        async def create(**kwargs):
            accounts.checkout_shop(kwargs['payment_account'], kwargs['user_id'], authenticated=kwargs['authenticated_user'])
            return 'payment-1', 'https://checkout.example.invalid/pay'
        call = AsyncMock(side_effect=create)
        save_email = Mock()
        scope = extract_functions({'tg_topup_create', 'tg_subscription_create'}, dict(
            Request=object, _verify_telegram_webapp_init_data=lambda raw: None,
            _tg_user_id_from_request=lambda *args: 42, _tg_payment_authenticated=lambda *args: False,
            _find_pack_by_tokens=lambda tokens: {'tokens': 10, 'rub': 100, 'stars': 5},
            _public_subscription_plan_for_tg=lambda code: {'price_rub': 100, 'tokens': 10, 'duration_days': 30},
            get_current_subscription=lambda uid: {}, _yookassa_enabled=lambda: True,
            sb_get_user_email=lambda uid: 'test@example.invalid', _EMAIL_RE=re.compile(r'.+@.+'),
            sb_set_user_email=save_email, checkout_shop=accounts.checkout_shop,
            WEBAPP_ACCOUNT_URL='https://example.invalid/account', create_yookassa_payment=call,
        ))
        payload = {'uid': 42, 'tokens': 10, 'plan_code': 'spark', 'payment_account': 'new', 'email': 'other@example.invalid'}
        request = types.SimpleNamespace(headers={}, query_params={}, json=AsyncMock(return_value=payload))
        with patch.dict(os.environ, ENV, clear=True):
            for name in ['tg_topup_create', 'tg_subscription_create']:
                self.assertFalse((await scope[name](request))['ok'])
                call.assert_not_called()
                save_email.assert_not_called()
            scope['_tg_payment_authenticated'] = lambda *args: True
            for name in ['tg_topup_create', 'tg_subscription_create']:
                self.assertTrue((await scope[name](request))['ok'])
                self.assertTrue(call.call_args.kwargs['authenticated_user'])
            call.reset_mock(); save_email.reset_mock()
            os.environ['YOOKASSA_NEW_MODE'] = 'off'
            for name in ['tg_topup_create', 'tg_subscription_create']:
                self.assertFalse((await scope[name](request))['ok'])
                call.assert_not_called(); save_email.assert_not_called()

            # Existing legacy checkout remains compatible with the old browser flow.
            scope['_tg_payment_authenticated'] = lambda *args: False
            payload['payment_account'] = 'legacy'
            for name in ['tg_topup_create', 'tg_subscription_create']:
                self.assertTrue((await scope[name](request))['ok'])
                self.assertEqual(call.call_args.kwargs['payment_account'], 'legacy')

    async def test_webhook_routing_events_and_independent_secret(self):
        class Response:
            def __init__(self, status_code, content): self.status_code = status_code
        reconcile = AsyncMock(return_value={'status': 'pending'})
        scope = extract_functions({'yookassa_webhook'}, dict(Request=object, os=os, hmac=hmac, Response=Response,
            YOOKASSA_WEBHOOK_REQUIRE_SECRET=True, YOOKASSA_WEBHOOK_SECRET='old-token', ADMIN_IDS=set(),
            reconcile_yookassa_payment=reconcile, _yookassa_post_apply_side_effects=AsyncMock()))
        req = types.SimpleNamespace(url=types.SimpleNamespace(path='/api/yookassa/new/webhook'), headers={}, query_params={},
            json=AsyncMock(return_value={'event': 'payment.succeeded', 'object': {'id': 'payment-1'}}))
        with patch.dict(os.environ, ENV, clear=True):
            self.assertTrue((await scope['yookassa_webhook'](req))['ok'])
            self.assertEqual(reconcile.call_args.kwargs['expected_account'], 'new')
            req.json.return_value['event'] = 'payment.canceled'
            await scope['yookassa_webhook'](req); self.assertEqual(reconcile.await_count, 2)
            req.json.return_value['event'] = 'refund.succeeded'
            await scope['yookassa_webhook'](req); self.assertEqual(reconcile.await_count, 2)
            req.url.path = '/api/yookassa/webhook'
            self.assertEqual((await scope['yookassa_webhook'](req)).status_code, 401)
            req.query_params = {'token': 'old-token'}; req.json.return_value['event'] = 'payment.succeeded'
            await scope['yookassa_webhook'](req)
            self.assertEqual(reconcile.call_args.kwargs['expected_account'], 'legacy')
            os.environ['YOOKASSA_NEW_WEBHOOK_SECRET'] = 'new-token'; req.url.path = '/api/yookassa/new/webhook'
            self.assertEqual((await scope['yookassa_webhook'](req)).status_code, 401)
            req.query_params = {'token': 'new-token'}
            self.assertTrue((await scope['yookassa_webhook'](req))['ok'])

    async def test_tg_tester_requires_fresh_signed_data_and_matching_id(self):
        from typing import Optional
        scope = extract_functions({'_verify_telegram_webapp_init_data', '_tg_payment_authenticated'}, dict(
            Request=object, Optional=Optional, urllib=urllib, json=json, time=time, hmac=hmac, hashlib=hashlib,
            TELEGRAM_BOT_TOKEN='fake-bot-token'))
        def signed(uid, timestamp):
            parts = {'auth_date': str(timestamp), 'user': json.dumps({'id': uid})}
            secret = hmac.new(b'WebAppData', b'fake-bot-token', hashlib.sha256).digest()
            parts['hash'] = hmac.new(secret, '\n'.join(f'{k}={v}' for k,v in sorted(parts.items())).encode(), hashlib.sha256).hexdigest()
            return urllib.parse.urlencode(parts)
        req = types.SimpleNamespace(headers={'X-Telegram-Init-Data': signed(42, int(time.time()))})
        self.assertTrue(scope['_tg_payment_authenticated'](req, 42))
        self.assertFalse(scope['_tg_payment_authenticated'](req, 99))
        req.headers['X-Telegram-Init-Data'] = signed(42, int(time.time()) - 90000)
        self.assertFalse(scope['_tg_payment_authenticated'](req, 42))
        req.headers = {}; self.assertFalse(scope['_tg_payment_authenticated'](req, 42))


class NativeTelegramTests(unittest.IsolatedAsyncioTestCase):
    """Run the actual bot payment branches without importing unrelated AI services."""
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / 'main.py').read_text())
        nodes = [node for node in ast.walk(tree) if isinstance(node, ast.If)]
        callback = [node for node in nodes if ast.unparse(node.test) == "chat_id and user_id and data.startswith('topup:')"]
        email = [node for node in nodes if ast.unparse(node.test) == "sb_state == 'yk_wait_email' and isinstance(sb_payload, dict)"]
        assert len(callback) == len(email) == 1
        cls.branch_code = []
        for name, node in [('callback', callback[0]), ('email_resume', email[0])]:
            wrapper = ast.parse(f'async def {name}():\n    pass\n')
            wrapper.body[0].body = [copy.deepcopy(node)]
            ast.fix_missing_locations(wrapper)
            cls.branch_code.append(compile(wrapper, 'main.py', 'exec'))

    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True); self.env.start(); self.addCleanup(self.env.stop)
        async def create(**kwargs):
            accounts.checkout_shop(kwargs['payment_account'], kwargs['user_id'], authenticated=kwargs['authenticated_user'])
            return 'payment-1', 'https://checkout.example.invalid/pay'
        self.create = AsyncMock(side_effect=create)
        self.send = AsyncMock()
        self.save_state = Mock()
        self.scope = dict(
            chat_id=42, user_id=42, data='topup:pack:10',
            _find_pack_by_tokens=lambda tokens: {'tokens': 10, 'rub': 100, 'stars': 5},
            _yookassa_enabled=accounts.any_shop_configured, payment_methods=accounts.payment_methods,
            checkout_shop=accounts.checkout_shop, create_yookassa_payment=self.create,
            tg_send_message=self.send, _help_menu_for=lambda uid: {}, _topup_packs_kb=lambda: {},
            sb_get_user_email=Mock(return_value='test@example.invalid'), sb_set_user_state=self.save_state,
            sb_clear_user_state=Mock(), sb_set_user_email=Mock(return_value=True),
            sb_state='yk_wait_email', sb_payload={}, incoming_text='test@example.invalid',
        )
        for code in self.branch_code: exec(code, self.scope)

    async def test_native_tester_chooses_and_creates_new_payment(self):
        await self.scope['callback']()
        self.create.assert_not_called()
        buttons = self.send.call_args.kwargs['reply_markup']['inline_keyboard']
        self.assertEqual([line[0]['callback_data'] for line in buttons], ['topup:pack:10:legacy', 'topup:pack:10:new'])
        self.scope['data'] = 'topup:pack:10:new'
        await self.scope['callback']()
        self.assertEqual(self.create.call_args.kwargs['payment_account'], 'new')
        self.assertTrue(self.create.call_args.kwargs['authenticated_user'])

    async def test_native_denied_new_selection_does_not_create_or_save_state(self):
        self.scope.update(data='topup:pack:10:new', user_id=99)
        await self.scope['callback']()
        self.create.assert_not_called(); self.save_state.assert_not_called()
        self.scope['sb_get_user_email'].assert_not_called()

    async def test_native_email_resume_keeps_selected_shop_after_restart(self):
        self.scope.update(data='topup:pack:10:new')
        self.scope['sb_get_user_email'].return_value = ''
        await self.scope['callback']()
        self.create.assert_not_called()
        self.scope['sb_payload'] = dict(self.save_state.call_args.args[2])
        self.assertEqual(self.scope['sb_payload']['payment_account'], 'new')
        os.environ['YOOKASSA_DEFAULT_ACCOUNT'] = 'legacy'
        await self.scope['email_resume']()
        self.assertEqual(self.create.call_args.kwargs['payment_account'], 'new')
        self.assertTrue(self.create.call_args.kwargs['authenticated_user'])

    async def test_native_old_callback_follows_switch_and_rollback(self):
        for settings, expected in [
            ({'YOOKASSA_NEW_MODE': 'all', 'YOOKASSA_DEFAULT_ACCOUNT': 'new', 'YOOKASSA_LEGACY_CHECKOUT_ENABLED': 'false'}, 'new'),
            ({'YOOKASSA_NEW_MODE': 'off', 'YOOKASSA_DEFAULT_ACCOUNT': 'legacy', 'YOOKASSA_LEGACY_CHECKOUT_ENABLED': 'true'}, 'legacy'),
        ]:
            with self.subTest(expected=expected):
                os.environ.update(settings)
                await self.scope['callback']()
                self.assertEqual(self.create.call_args.kwargs['payment_account'], expected)


if __name__ == '__main__': unittest.main()
