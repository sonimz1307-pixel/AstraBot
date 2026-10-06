"""Regression: reply-keyboard -> inline launch -> signed new-shop checkout.

All Telegram, database and payment calls are local stubs. No money is charged.
"""
import ast
import hashlib
import hmac
import json
import os
import re
import time
import types
import unittest
import urllib.parse
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, Mock, patch

from test_yookassa_dual_shop import ROOT, ENV, accounts, extract_functions


TOKEN = 'offline-cabinet-bot-token'


def signed_data(uid=42, *, age=0, token=TOKEN):
    data = {'auth_date': str(int(time.time()) - age), 'user': json.dumps({'id': uid}),
            'query_id': 'offline-session', 'signature': 'offline-telegram-signature'}
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    data['hash'] = hmac.new(secret, '\n'.join(f'{k}={v}' for k, v in sorted(data.items())).encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


class CabinetAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, dict(ENV, YOOKASSA_NEW_MODE='all',
            YOOKASSA_DEFAULT_ACCOUNT='new', YOOKASSA_LEGACY_CHECKOUT_ENABLED='false'), clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.send = AsyncMock()
        self.save_email = Mock()
        self.create = AsyncMock(return_value=('offline-payment', 'https://checkout.example.invalid/paid'))
        self.scope = extract_functions({
            '_verify_telegram_webapp_init_data', '_tg_payment_authenticated', '_tg_user_id_from_request',
            'tg_topup_create', 'tg_subscription_create', '_handle_account_entry_message',
            '_main_menu_keyboard',
        }, dict(
            Any=Any, Dict=Dict, Optional=Optional, Request=object,
            urllib=urllib, json=json, time=time, hmac=hmac, hashlib=hashlib,
            TELEGRAM_BOT_TOKEN=TOKEN, WEBAPP_ACCOUNT_URL='https://app.example.invalid/webapp/account',
            WEBAPP_KLING_URL='https://app.example.invalid/webapp/kling',
            WEBAPP_MUSIC_URL='https://app.example.invalid/webapp/music',
            WEBAPP_PROMPTS_URL='https://app.example.invalid/webapp/prompts',
            _with_uid=lambda url, uid: url + '?' + urllib.parse.urlencode({'uid': uid}),
            _telegram_message_text=lambda message: message.get('text', ''),
            tg_send_message=self.send, _yookassa_enabled=lambda: True,
            _find_pack_by_tokens=lambda tokens: {'tokens': 5, 'rub': 65, 'stars': 33} if tokens == 5 else None,
            checkout_shop=accounts.checkout_shop, create_yookassa_payment=self.create,
            sb_get_user_email=Mock(return_value='buyer@example.invalid'), sb_set_user_email=self.save_email,
            _EMAIL_RE=re.compile(r'.+@.+\..+'),
            _public_subscription_plan_for_tg=lambda code: {'name': 'Pulse', 'price_rub': 210, 'tokens': 20, 'duration_days': 30},
            get_current_subscription=lambda uid: {'is_active': False},
            _subscription_public_payload_for_tg=lambda uid: {},
        ))

    @staticmethod
    def message(text='', data=None, *, private=True):
        msg = {'text': text, 'from': {'id': 42}, 'chat': {'id': 42 if private else -100,
                'type': 'private' if private else 'group'}}
        if data is not None:
            msg['web_app_data'] = {'data': json.dumps(data)}
        return msg

    def request(self, raw, *, selected='new'):
        return types.SimpleNamespace(headers={'X-Telegram-Init-Data': raw}, query_params={'uid': '777'},
            json=AsyncMock(return_value={'uid': 777, 'tokens': 5, 'plan_code': 'pulse',
                'payment_account': selected, 'email': 'buyer@example.invalid'}))

    async def test_keyboard_then_inline_launch_uses_trusted_sender(self):
        keyboard = self.scope['_main_menu_keyboard'](user_id=42)
        button = next(b for row in keyboard['keyboard'] for b in row if b['text'] == '👤 Кабинет')
        self.assertNotIn('web_app', button)
        self.assertTrue(await self.scope['_handle_account_entry_message'](self.message(button['text'])))
        markup = self.send.call_args.kwargs['reply_markup']
        self.assertEqual(markup['inline_keyboard'][0][0]['web_app']['url'],
                         'https://app.example.invalid/webapp/account?uid=42')

    async def test_old_keyboard_bridge_ignores_client_user_id(self):
        self.assertTrue(await self.scope['_handle_account_entry_message'](
            self.message(data={'action': 'open_account', 'uid': 999, 'url': 'https://evil.example.invalid'})))
        self.assertEqual(self.send.call_args.args[0], 42)
        url = self.send.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]['web_app']['url']
        self.assertEqual(url, 'https://app.example.invalid/webapp/account?uid=42')

    async def test_cabinet_commands_and_deep_link(self):
        for text in ['/cabinet', '/account', '/start cabinet', '/cabinet@ExampleBot']:
            with self.subTest(text=text):
                self.assertTrue(await self.scope['_handle_account_entry_message'](self.message(text)))

    async def test_other_messages_and_payout_still_reach_existing_handlers(self):
        for msg in [self.message('/start referral_code'), self.message('generation prompt'),
                    self.message(data={'action': 'partner_payout'})]:
            self.assertFalse(await self.scope['_handle_account_entry_message'](msg))
        self.send.assert_not_awaited()

    async def test_group_chat_never_receives_personal_webapp_launch(self):
        self.assertTrue(await self.scope['_handle_account_entry_message'](self.message('/cabinet', private=False)))
        self.assertNotIn('reply_markup', self.send.call_args.kwargs)

    async def test_reply_keyboard_without_init_data_cannot_write_email_or_create_payment(self):
        for route in ['tg_topup_create', 'tg_subscription_create']:
            with self.subTest(route=route):
                result = await self.scope[route](self.request(''))
                self.assertFalse(result['ok'])
                self.assertEqual(result['error'], 'telegram_auth_required')
                self.assertIn('/cabinet', result['message'])
        self.create.assert_not_awaited()
        self.save_email.assert_not_called()

    async def test_fresh_inline_session_can_buy_tokens_and_subscription_after_legacy_is_off(self):
        for route in ['tg_topup_create', 'tg_subscription_create']:
            with self.subTest(route=route):
                result = await self.scope[route](self.request(signed_data()))
                self.assertTrue(result['ok'], result)
                args = self.create.call_args.kwargs
                self.assertEqual(args['payment_account'], 'new')
                self.assertEqual(args['user_id'], 42)  # ignores spoofed uid=777
                self.assertTrue(args['authenticated_user'])

    async def test_expired_forged_and_other_bot_sessions_still_fail_closed(self):
        for raw in [signed_data(age=90000), signed_data().replace('offline-session', 'forged'),
                    signed_data(token='another-bot')]:
            for route in ['tg_topup_create', 'tg_subscription_create']:
                result = await self.scope[route](self.request(raw))
                self.assertEqual(result['error'], 'telegram_auth_required')
        self.create.assert_not_awaited()
        self.save_email.assert_not_called()

    async def test_disabled_legacy_selection_is_not_silently_reassigned(self):
        result = await self.scope['tg_topup_create'](self.request(signed_data(), selected='legacy'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'payment_create_failed')
        self.create.assert_not_awaited()

    def test_entry_dispatch_precedes_existing_webapp_and_state_handlers(self):
        source = (ROOT / 'main.py').read_text()
        entry = source.index('if await _handle_account_entry_message(message):')
        self.assertLess(entry, source.index('# --- Telegram WebApp -> ordinary bot: partner payout ---'))
        ast.parse(source)


if __name__ == '__main__':
    unittest.main()
