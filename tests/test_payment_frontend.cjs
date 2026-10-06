// Exercise the actual workspace fetch/checkout functions without a network or DOM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'web_workspace_frontend/app.js'), 'utf8');

function extract(name, next) {
  const start = source.indexOf(`async function ${name}(`);
  const end = source.indexOf(next, start);
  assert.ok(start >= 0 && end > start, `Function not found: ${name}`);
  return source.slice(start, end);
}
function response(status, data) {
  return { status, ok: status < 300, text: async () => JSON.stringify(data), json: async () => data };
}
function setup(fetch) {
  const state = { storage: new Map([['pending_topup', '10']]), clears: 0, bootClears: 0, saved: [] };
  const location = { href: 'https://example.invalid/account?auth=login&topup=10&ref=keep#workspace' };
  const context = {
    Headers, URL, fetch, window: { location }, BOOT_QUERY: new URL(location.href).searchParams,
    history: { replaceState(_state, _title, value) { location.href = new URL(value, location.href).href; state.bootClears++; } },
    getApiBaseUrl: () => 'https://api.example.invalid', shouldUseLegacyMigrationAuth: () => false,
    isLegalConsentRequiredError: () => false, requireAuth: () => true,
    PENDING_TOPUP_KEY: 'pending_topup', PENDING_TOPUP_RETURN_KEY: 'pending_return',
    safeStorageGet: (key, fallback) => state.storage.get(key) || fallback,
    safeStorageRemove: key => { state.storage.delete(key); if (key === 'pending_topup') state.clears++; },
    safeStorageSet: (...args) => state.saved.push(args), PENDING_YOOKASSA_PAYMENT_KEY: 'pending_payment',
    runtime: { pendingTopupInFlight: false }, state: { authToken: 'offline', me: { id: 42 } }, toast() {},
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, 'static/nabex-payments.js'), 'utf8'), context);
  vm.runInContext(extract('apiFetch', 'function isInvalidWorkspaceSessionError'), context);
  vm.runInContext(source.slice(source.indexOf('function readPendingTopupTokens('), source.indexOf('async function startWorkspaceTopup(')), context);
  vm.runInContext(extract('startWorkspaceTopup', 'async function reconcilePendingYookassaPayment'), context);
  vm.runInContext(extract('resumePendingTopup', 'function pushRun'), context);
  return { context, state };
}

(async () => {
  let checks = 0;
  for (const status of [401, 403, 404]) {
    const { context } = setup(async () => response(status, { detail: 'Unavailable' }));
    assert.equal(await context.window.NabexPayments.choose(() => context.apiFetch('/api/workspace/payment/methods')), '');
    checks++;
  }

  const requests = [];
  const oldBackend = setup(async (url, options) => {
    requests.push({ url, options });
    return url.endsWith('/methods') ? response(404, { detail: 'Not Found' })
      : response(200, { ok: true, payment_id: 'old-payment', confirmation_url: 'https://checkout.example.invalid/old' });
  });
  await oldBackend.context.startWorkspaceTopup(10);
  assert.equal(requests.length, 2);
  assert.equal(JSON.parse(requests[1].options.body).payment_account, null);
  assert.equal(oldBackend.context.window.location.href, 'https://checkout.example.invalid/old');
  checks++;

  for (const status of [503, 429]) {
    let posts = 0;
    const { context } = setup(async (url, options) => {
      if (options.method === 'POST') posts++;
      return response(status, { detail: 'Try later' });
    });
    await assert.rejects(() => context.startWorkspaceTopup(10), error => error.status === status);
    assert.equal(posts, 0);
    checks++;
  }

  const canceled = setup(async () => { throw new Error('Canceled checkout must not send a request'); });
  canceled.context.window.NabexPayments = { choose: async () => null };
  assert.equal(await canceled.context.resumePendingTopup(), false);
  assert.equal(canceled.context.readPendingTopupTokens(), 0);
  assert.equal(canceled.state.storage.has('pending_topup'), false);
  assert.equal(canceled.context.window.location.href, 'https://example.invalid/account?ref=keep#workspace');
  assert.equal(canceled.state.clears, 1);
  assert.equal(canceled.state.bootClears, 1);
  assert.equal(canceled.context.runtime.pendingTopupInFlight, false);
  assert.equal(await canceled.context.resumePendingTopup(), false);
  assert.equal(canceled.state.clears, 1);
  checks++;

  const selectedRequests = [];
  const newBackend = setup(async (url, options) => {
    selectedRequests.push({ url, options });
    return url.endsWith('/methods') ? response(200, { ok: true, methods: [{ id: 'new' }], default: 'new' })
      : response(200, { ok: true, payment_id: 'new-payment', confirmation_url: 'https://checkout.example.invalid/new' });
  });
  assert.equal(await newBackend.context.resumePendingTopup(), true);
  assert.equal(JSON.parse(selectedRequests[1].options.body).payment_account, 'new');
  assert.deepEqual(newBackend.state.saved, [['pending_payment', 'new-payment']]);
  checks++;
  assert.equal(fs.readFileSync(path.join(root, 'static/nabex-payments.js'), 'utf8'),
    fs.readFileSync(path.join(root, 'web_workspace_frontend/nabex-payments.js'), 'utf8'));
  console.log(`Workspace payment integration: ${checks} offline checks passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
