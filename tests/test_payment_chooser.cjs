// Offline DOM simulation: choice, cancellation, default path and auth fallback.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
let dialog;
class Element {
  constructor(tag) { this.tag = tag; this.style = {}; this.children = []; this.events = {}; }
  setAttribute() {}
  append(...children) { this.children.push(...children); }
  addEventListener(name, callback) { this.events[name] = callback; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  remove() { this.removed = true; }
  focus() {}
  querySelector(tag) { return this.children.find(child => child.tag === tag); }
}
const context = {
  window: {},
  document: {
    activeElement: new Element('button'), createElement: tag => new Element(tag),
    body: { append(element) { dialog = element; } },
  },
};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/nabex-payments.js'), 'utf8'), context);
const choose = context.window.NabexPayments.choose;
const load = (methods, status = 200) => async () => ({ status, ok: status === 200, json: async () => ({ ok: true, methods, default: 'legacy' }) });
(async () => {
  assert.equal(await choose(load([{ id: 'legacy' }])), 'legacy');
  assert.equal(await choose(load([{ id: 'new' }])), 'new');
  assert.equal(await choose(load([])), '');
  assert.equal(await choose(load([], 401)), '');
  assert.equal(await choose(load([], 404)), '');
  await assert.rejects(() => choose(load([], 503)));
  const methods = [{ id: 'legacy', label: 'Карта / СБП' }, { id: 'new', label: 'Карта / СБП — способ 2' }];
  let pending = choose(load(methods));
  await new Promise(resolve => setImmediate(resolve));
  dialog.children.filter(c => c.tag === 'button')[1].events.click();
  assert.equal(await pending, 'new');
  assert.equal(dialog.removed, true);
  pending = choose(load(methods));
  await new Promise(resolve => setImmediate(resolve));
  dialog.events.cancel({ preventDefault() {} });
  assert.equal(await pending, null);
  console.log('Payment chooser: 8 offline checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
