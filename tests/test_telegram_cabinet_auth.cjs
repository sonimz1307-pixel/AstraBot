'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '..');
const helper = fs.readFileSync(path.join(root, 'static/nabex-telegram-auth.js'), 'utf8');
const account = fs.readFileSync(path.join(root, 'webapp_account.html'), 'utf8');
const topup = fs.readFileSync(path.join(root, 'webapp_topup.html'), 'utf8');
let checks = 0;

function scriptFunction(html, name, next) {
  const start = html.indexOf('    async function ' + name + '(');
  const syncStart = html.indexOf('    function ' + name + '(');
  const actual = start < 0 ? syncStart : start;
  const end = html.indexOf('\n    ' + next, actual + 1);
  assert(actual >= 0 && end > actual, name);
  return html.slice(actual, end);
}

function makeContext(tg) {
  const events = { sent: [], alerts: [], requests: [], links: [] };
  const context = { window: { alert: message => events.alerts.push(message) }, tg, JSON, URLSearchParams };
  if (tg) {
    tg.sendData = value => events.sent.push(JSON.parse(value));
    tg.showAlert = message => events.alerts.push(message);
    tg.openTelegramLink = url => events.links.push(url);
  }
  vm.runInNewContext(helper, context);
  context.fetch = async (...args) => { events.requests.push(args); throw new Error('Unexpected payment request'); };
  return { context, events };
}

(async () => {
  for (const platform of ['android', 'ios', 'tdesktop', 'web']) {
    const { context, events } = makeContext({ initData: '', platform });
    assert.equal(context.window.NabexTelegramAuth.ensureSession(context.tg), false);
    assert.deepEqual(events.sent, [{ action: 'open_account' }]);
    checks++;
  }
  const browser = makeContext({ initData: '', platform: 'unknown' });
  assert.equal(browser.context.window.NabexTelegramAuth.ensureSession(browser.context.tg), false);
  assert.equal(browser.events.sent.length, 0);
  assert.match(browser.events.alerts[0], /\/cabinet/);
  checks++;
  const inline = makeContext({ initData: 'signed-data-validated-on-server', platform: 'android' });
  assert.equal(inline.context.window.NabexTelegramAuth.ensureSession(inline.context.tg), true);
  assert.equal(inline.events.sent.length, 0);
  checks++;

  // Exercise the actual entry of all three checkout functions: no initData means
  // no access to form fields, method lookup, email write or payment POST.
  for (const [html, name, next, args] of [
    [account, 'createTopupPayment', 'async function createSubscriptionPayment', [5, null]],
    [account, 'createSubscriptionPayment', 'async function ', ['pulse', null, 'buyer@example.invalid']],
    [topup, 'createTopup', 'async function handleBuy', [5, 'buyer@example.invalid']],
  ]) {
    const { context, events } = makeContext({ initData: '', platform: 'android' });
    vm.runInNewContext(scriptFunction(html, name, next), context);
    await context[name](...args);
    assert.deepEqual(events.sent, [{ action: 'open_account' }]);
    assert.equal(events.requests.length, 0);
    checks++;
  }

  // Authenticated launches must still reach the real checkout function bodies,
  // with the same signed header on discovery and creation and the new shop ID.
  for (const [html, name, next, args] of [
    [account, 'createTopupPayment', 'async function createSubscriptionPayment', [5, null]],
    [account, 'createSubscriptionPayment', 'async function ', ['pulse', null, 'buyer@example.invalid']],
    [topup, 'createTopup', 'async function handleBuy', [5, 'buyer@example.invalid']],
  ]) {
    const { context, events } = makeContext({ initData: 'signed-session', platform: 'android' });
    context.window.location = { href: 'https://app.example.invalid/webapp/account?uid=42#tgWebAppData=private-data' };
    context.query = new URLSearchParams('uid=42');
    context.topupEmailInput = { value: 'buyer@example.invalid' };
    context.state = { customerEmail: 'buyer@example.invalid' };
    context.setTopupLoading = () => {};
    context.showToast = () => {};
    context.openUrl = url => events.links.push(url);
    context.accountUrl = () => 'https://app.example.invalid/webapp/account?uid=42';
    vm.runInNewContext(fs.readFileSync(path.join(root, 'static/nabex-payments.js'), 'utf8'), context);
    context.fetch = async (url, options) => {
      events.requests.push({ url, options });
      return { ok: true, status: 200, json: async () => url.endsWith('/methods')
        ? { ok: true, methods: [{ id: 'new' }], default: 'new' }
        : { ok: true, payment_id: 'offline-payment', confirmation_url: 'https://checkout.example.invalid/paid' } };
    };
    vm.runInNewContext(scriptFunction(html, name, next), context);
    await context[name](...args);
    assert.equal(events.sent.length, 0);
    assert.equal(events.alerts.length, 0);
    assert.equal(events.requests.length, 2);
    for (const request of events.requests) assert.equal(request.options.headers['X-Telegram-Init-Data'], 'signed-session');
    const payload = JSON.parse(events.requests[1].options.body);
    assert.equal(payload.payment_account, 'new');
    assert.equal(payload.return_url, 'https://app.example.invalid/webapp/account?uid=42');
    checks++;
  }

  // A newly authenticated cabinet is an inline Mini App. Payout must use its
  // existing Telegram deep link; sendData works only in reply-keyboard apps.
  for (const signed of [true, false]) {
    const { context, events } = makeContext({ initData: signed ? 'signed' : '', platform: 'android' });
    context.state = { payoutBotUrl: 'https://t.me/ExampleBot?start=payout' };
    vm.runInNewContext(scriptFunction(account, 'startPartnerPayoutInBot', 'function buildTopupUrl'), context);
    context.startPartnerPayoutInBot();
    if (signed) {
      assert.equal(events.sent.length, 0);
      assert.deepEqual(events.links, ['https://t.me/ExampleBot?start=payout']);
    } else {
      assert.deepEqual(events.sent, [{ action: 'partner_payout' }]);
    }
    checks++;
  }
  console.log(`Telegram cabinet frontend: ${checks} regression checks passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
