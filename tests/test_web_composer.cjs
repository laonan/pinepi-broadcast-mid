const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const P = require('../examples/web/message-protocol.js');
const html = fs.readFileSync(require.resolve('../examples/web/index.html'), 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match => match[1]).join('\n');

// Execute the actual page handlers with only the DOM surfaces they use.
function openComposer(protocol = P, history = []) {
  function element() {
    const listeners = {};
    return {
      value: '', textContent: '', checked: false, files: [], hidden: false, style: {},
      classList: { add() {}, remove() {}, toggle() {} },
      setAttribute() {}, removeAttribute() {}, append() {}, replaceChildren() {},
      addEventListener(type, handler) { (listeners[type] ||= []).push(handler); },
      async trigger(type) { for (const handler of listeners[type] || []) await handler({}); }
    };
  }
  const elements = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], element()]));
  const values = new Map(Object.entries({ apiUrl: 'https://mid.example.org/api/send', token: 'test-producer', devices: 'epaper', msgHistory: JSON.stringify(history) }));
  const requests = [];
  const context = {
    PinePiProtocol: protocol, crypto: webcrypto, Uint32Array, URL, TextEncoder,
    setTimeout() { return 1; }, clearTimeout() {}, navigator: {},
    window: { matchMedia() { return { matches: false, addEventListener() {} }; } },
    document: {
      documentElement: element(), getElementById(id) { assert.ok(elements[id], id); return elements[id]; },
      querySelectorAll() { return []; }, querySelector() { return element(); }, createElement: element
    },
    localStorage: { getItem(key) { return values.get(key) ?? null; }, setItem(key, value) { values.set(key, value); } },
    async fetch(url, options) {
      requests.push({ url, body: JSON.parse(options.body) });
      return { ok: true, async json() { return { delivered: 1, buffered: 0 }; } };
    }
  };
  vm.runInNewContext(script, context, { filename: 'index.html' });
  return { elements, requests, values };
}

test('page preview and send both use the original protocol in ordinary-device mode', async () => {
  const { elements, requests } = openComposer();
  assert.equal(elements.deliveryMode.value, 'DIRECT');
  const preview = JSON.parse(elements.payloadPreview.textContent);
  assert.deepEqual(Object.keys(preview.event).sort(), ['content', 'play_now', 'title']);
  await elements.sendBtn.trigger('click');
  assert.deepEqual(requests[0].body, preview);
  assert.match(elements.status.textContent, /无需 Hub/);
});

test('a stale shared script cannot preview, copy or send a Hub payload in ordinary mode', async () => {
  const stale = { ...P };
  delete stale.COMPOSER_VERSION;
  stale.buildRequest = () => ({ targets: ['epaper'], event: { type: 'home.message', version: 2 } });
  const { elements, requests } = openComposer(stale);
  assert.match(elements.payloadPreview.textContent, /版本不一致/);
  await elements.copyPayload.trigger('click');
  assert.match(elements.status.textContent, /版本不一致/);
  await elements.sendBtn.trigger('click');
  assert.match(elements.status.textContent, /版本不一致/);
  assert.equal(requests.length, 0);
});

test('restoring a Hub history record then switching to ordinary mode changes the actual send format', async () => {
  const event = P.buildMessage({ ...P.PRESETS.text, severity: 'NORMAL', textMode: 'TTS' }, { id: 'saved-hub', createdAt: '2026-11-03T09:38:36Z' });
  const { elements, requests } = openComposer(P, [{ event, targets: ['epaper'] }]);
  assert.equal(elements.deliveryMode.value, 'HUB');
  assert.equal(JSON.parse(elements.payloadPreview.textContent).event.version, 2);
  elements.deliveryMode.value = 'DIRECT';
  await elements.messageCard.trigger('input');
  assert.equal(JSON.parse(elements.payloadPreview.textContent).event.content, event.text);
  await elements.sendBtn.trigger('click');
  assert.deepEqual(requests[0].body.event, { title: event.title, content: event.text, play_now: 0 });
});

test('HTML and offline shell request the versioned shared script', () => {
  const asset = `message-protocol.js?v=${P.COMPOSER_VERSION}`;
  assert.ok(html.includes(`src="${asset}"`));
  const worker = fs.readFileSync(require.resolve('../examples/web/sw.js'), 'utf8');
  assert.ok(worker.includes(`'${asset}'`));
});
