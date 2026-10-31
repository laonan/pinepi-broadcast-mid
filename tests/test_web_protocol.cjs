const { test } = require('node:test');
const assert = require('node:assert/strict');
const { PRESETS, buildRequest, buildMessage, validateUrl } = require('../examples/web/message-protocol.js');
const identity = { id: 'web-test-001', createdAt: '2026-10-31T07:30:00+08:00' };

test('all six examples produce complete strict v2 payloads', () => {
  for (const preset of Object.values(PRESETS)) {
    const { event, targets } = buildRequest(preset, identity, ['screen']);
    assert.deepEqual(targets, ['screen']);
    assert.equal(event.type, 'home.message');
    assert.equal(event.version, 2);
    assert.equal(event.id, identity.id);
    assert.equal(event.created_at, identity.createdAt);
    const common = ['type', 'version', 'id', 'source', 'created_at', 'kind', 'severity', 'title'];
    const fields = event.kind === 'TEXT' ? ['text', 'text_mode', ...(event.text_mode === 'TTS' ? ['speech'] : [])] : ['media'];
    assert.deepEqual(Object.keys(event).sort(), [...common, ...fields].sort());
    if (event.media) assert.deepEqual(Object.keys(event.media).sort(), ['mime_type', 'url']);
    if (event.speech) assert.deepEqual(Object.keys(event.speech).sort(), ['mime_type', 'url']);
  }
});

test('PRIVATE never includes speech even after switching from a populated TTS draft', () => {
  const event = buildMessage({ ...PRESETS.tts, textMode: 'PRIVATE', severity: 'CRITICAL' }, identity);
  assert.equal(event.severity, 'CRITICAL');
  assert.equal(event.text_mode, 'PRIVATE');
  assert.ok(!('speech' in event));
  assert.ok(!('play_now' in event));
  assert.ok(!('autoplay' in event));
});

test('TTS requires a real MP3 URL, without silently changing privacy mode', () => {
  assert.throws(() => buildMessage({ ...PRESETS.tts, speechUrl: '' }, identity), /URL/);
  const event = buildMessage(PRESETS.tts, identity);
  assert.equal(event.speech.mime_type, 'audio/mpeg');
  assert.equal(event.text, PRESETS.tts.text);
});

test('media variants omit stale text and speech; MIME must match kind', () => {
  for (const kind of ['IMAGE', 'AUDIO', 'VIDEO']) {
    const draft = { ...PRESETS.tts, ...PRESETS[kind.toLowerCase()] };
    const event = buildMessage(draft, identity);
    assert.ok(!('text' in event));
    assert.ok(!('text_mode' in event));
    assert.ok(!('speech' in event));
    assert.throws(() => buildMessage({ ...draft, mimeType: 'application/octet-stream' }, identity), /MIME/);
  }
  assert.equal(buildMessage({ ...PRESETS.image, mimeType: 'image/png' }, identity).media.mime_type, 'image/png');
});

test('NORMAL, CRITICAL and omitted severity; optional title is omitted', () => {
  assert.equal(buildMessage(PRESETS.private, identity).severity, 'NORMAL');
  assert.equal(buildMessage(PRESETS.critical, identity).severity, 'CRITICAL');
  const old = buildMessage({ ...PRESETS.private, severity: '', title: '' }, identity);
  assert.ok(!('severity' in old));
  assert.ok(!('title' in old));
  for (const severity of ['critical', null, 0, '紧急']) {
    assert.throws(() => buildMessage({ ...PRESETS.private, severity }, identity), /NORMAL/);
  }
});

test('terminal URL policy accepts HTTPS and private IPv4 HTTP only', () => {
  for (const url of ['https://example.org/a', 'http://10.0.0.1/a', 'http://172.16.0.1/a', 'http://172.31.0.1/a', 'http://192.168.1.2:8766/a']) {
    assert.equal(validateUrl(url), url);
  }
  for (const url of ['http://example.org/a', 'http://127.0.0.1/a', 'http://172.15.1.1/a', 'http://172.32.1.1/a', 'file:///tmp/a', 'https:example.org/a', 'https://example.org\\@evil.example/a', 'https://user:secret@example.org/a', 'https://@example.org/a', 'https://example.org/a#', 'https://example.org/a#fragment', 'https://example.org:99999/a', 'https://exam\nple.org/a']) {
    assert.throws(() => validateUrl(url), /URL|HTTPS/);
  }
});

test('protocol lengths, required fields and request byte cap', () => {
  assert.throws(() => buildMessage({ ...PRESETS.private, source: '' }, identity), /Source/);
  assert.throws(() => buildMessage({ ...PRESETS.private, source: '😀'.repeat(51) }, identity), /Source/);
  assert.throws(() => buildMessage({ ...PRESETS.private, title: 'a'.repeat(201) }, identity), /Title/);
  assert.throws(() => buildMessage({ ...PRESETS.private, text: '' }, identity), /Text/);
  assert.throws(() => buildMessage({ ...PRESETS.private, text: 'a'.repeat(8193) }, identity), /Text/);
  assert.throws(() => buildMessage(PRESETS.private, { ...identity, id: 'invalid id' }), /ID/);
  assert.throws(() => buildMessage(PRESETS.private, { ...identity, createdAt: '2026-10-31T07:30:00' }), /ISO/);
  assert.throws(() => buildRequest(PRESETS.private, identity, []), /target/);
  assert.throws(() => buildRequest(PRESETS.private, identity, ['x'.repeat(65536)]), /64 KiB/);
});

test('unchanged retries preserve the supplied identity and target deduplication', () => {
  const first = buildRequest(PRESETS.private, identity, ['screen', 'screen']);
  assert.deepEqual(first, buildRequest(PRESETS.private, identity, ['screen']));
  assert.deepEqual(first.targets, ['screen']);
});
