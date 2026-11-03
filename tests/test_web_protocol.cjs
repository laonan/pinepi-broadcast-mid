const { test } = require('node:test');
const assert = require('node:assert/strict');
const P = require('../examples/web/message-protocol.js');
const identity = { id: 'web-test-001', createdAt: '2026-10-31T07:30:00+08:00' };
const asset = (kind = 'IMAGE') => ({ media_id: 'a'.repeat(32), mime_type: P.MIME[kind][0], size_bytes: 128, sha256: 'b'.repeat(64), expires_at: Date.now() / 1000 + 3600 });
const draft = (kind = 'TEXT', severity = 'NORMAL') => ({ ...P.PRESETS[kind.toLowerCase()], severity, ...(kind === 'TEXT' ? {} : { uploadedMedia: asset(kind) }) });

test('exactly four presets; severity is independent of every message type', () => {
  assert.deepEqual(Object.keys(P.PRESETS).sort(), ['audio', 'image', 'text', 'video']);
  for (const kind of ['TEXT', 'IMAGE', 'AUDIO', 'VIDEO']) {
    assert.ok(!Object.hasOwn(P.PRESETS[kind.toLowerCase()], 'severity'));
    for (const severity of ['NORMAL', 'CRITICAL']) {
      const request = P.buildRequest(draft(kind, severity), identity, ['hub']);
      assert.equal(request.event.kind, kind);
      assert.equal(request.event.severity, severity);
      assert.equal(request.event.id, identity.id);
      if (kind !== 'TEXT') {
        assert.equal(request.media_id, asset(kind).media_id);
        assert.deepEqual(request.event.media, { url: 'mid://' + asset(kind).media_id, mime_type: P.MIME[kind][0] });
      }
    }
  }
});

test('PRIVATE and TTS never attach speech, media, URLs or uploaded asset IDs', () => {
  for (const textMode of ['PRIVATE', 'TTS']) {
    const request = P.buildRequest({ ...draft(), textMode, speechSource: 'URL', speechUrl: 'https://example.org/fake.mp3', uploadedMedia: asset('AUDIO') }, identity, ['hub']);
    assert.equal(request.event.text_mode, textMode);
    for (const field of ['speech', 'media', 'autoplay', 'play_now']) assert.ok(!Object.hasOwn(request.event, field));
    assert.ok(!Object.hasOwn(request, 'media_id'));
  }
});

test('media requests require an actual uploaded asset, not a typed placeholder URL', () => {
  for (const kind of ['IMAGE', 'AUDIO', 'VIDEO']) {
    assert.throws(() => P.buildMessage({ ...draft(kind), uploadedMedia: null, mediaUrl: 'https://example.org/fake.mp4' }, identity), /upload/);
    const event = P.buildMessage({ ...draft(kind), text: 'stale', textMode: 'TTS', speechUrl: 'https://example.org/fake.mp3' }, identity);
    assert.ok(!Object.hasOwn(event, 'text'));
    assert.ok(!Object.hasOwn(event, 'text_mode'));
    assert.ok(!Object.hasOwn(event, 'speech'));
    assert.deepEqual(Object.keys(event.media).sort(), ['mime_type', 'url']);
  }
});

test('upload metadata rejects wrong kinds, altered IDs, sizes, hashes and expired files', () => {
  for (const changes of [{ media_id: '../file' }, { mime_type: 'video/mp4' }, { size_bytes: 0 }, { size_bytes: P.FILE_LIMITS.IMAGE + 1 }, { sha256: 'bad' }, { expires_at: 1 }]) {
    assert.throws(() => P.validateAsset({ ...asset(), ...changes }, 'IMAGE'));
  }
  assert.equal(P.validateAsset({ ...asset(), mime_type: 'image/png' }, 'IMAGE').mime_type, 'image/png');
});

test('local files are nonempty and respect each terminal size limit', () => {
  for (const kind of ['IMAGE', 'AUDIO', 'VIDEO']) {
    assert.throws(() => P.validateFile(null, kind), /Choose/);
    assert.throws(() => P.validateFile({ size: 0 }, kind), /nonempty/);
    assert.throws(() => P.validateFile({ size: P.FILE_LIMITS[kind] + 1 }, kind), /MiB/);
    assert.equal(P.validateFile({ size: 100 }, kind).size, 100);
  }
  assert.throws(() => P.validateFile({ size: 100 }, 'TEXT'), /Choose/);
});

test('upload endpoint stays on the configured Mid origin with no credential redirects', () => {
  assert.equal(P.uploadEndpoint('https://mid.example.org/api/send/'), 'https://mid.example.org/api/media');
  assert.equal(P.uploadEndpoint('http://127.0.0.1:8937/api/send'), 'http://127.0.0.1:8937/api/media');
  assert.throws(() => P.uploadEndpoint('http://public.example.org/api/send'), /HTTPS/);
  for (const value of ['https://u:secret@mid.example.org/api/send', 'https://mid.example.org/other', 'https://mid.example.org/api/send?token=x', 'https://mid.example.org/api/send#x']) assert.throws(() => P.uploadEndpoint(value));
});

test('historical speech URLs and old external media require a new submission', () => {
  assert.equal(P.needsHistoryRefresh({ kind: 'TEXT', speech: {} }), true);
  assert.equal(P.needsHistoryRefresh({ kind: 'TEXT', text_mode: 'TTS' }), false);
  assert.equal(P.needsHistoryRefresh({ kind: 'IMAGE', media: { url: 'https://example.org/fake.jpg' } }), true);
  assert.equal(P.needsHistoryRefresh({ kind: 'IMAGE', media: { url: 'mid://' + asset().media_id } }, asset()), false);
});

test('optional severity/title and strict enum validation', () => {
  const event = P.buildMessage({ ...draft(), severity: '', title: '' }, identity);
  assert.ok(!Object.hasOwn(event, 'severity'));
  assert.ok(!Object.hasOwn(event, 'title'));
  for (const severity of ['critical', null, 1, '紧急']) assert.throws(() => P.buildMessage({ ...draft(), severity }, identity), /NORMAL/);
});

test('common field lengths, identity and request byte cap are enforced', () => {
  for (const changes of [{ source: '' }, { source: '😀'.repeat(51) }, { title: 'a'.repeat(201) }, { text: '' }, { text: 'a'.repeat(8193) }]) assert.throws(() => P.buildMessage({ ...draft(), ...changes }, identity));
  assert.throws(() => P.buildMessage(draft(), { ...identity, id: 'invalid id' }), /ID/);
  assert.throws(() => P.buildMessage(draft(), { ...identity, createdAt: '2026-10-31T07:30:00' }), /ISO/);
  assert.throws(() => P.buildRequest(draft(), identity, []), /target/);
  assert.throws(() => P.buildRequest(draft(), identity, ['x'.repeat(65536)]), /64 KiB/);
  assert.deepEqual(P.buildRequest(draft(), identity, ['hub', 'hub']), P.buildRequest(draft(), identity, ['hub']));
});


test('ordinary device messages preserve the original epaper/speaker protocol without Hub fields', () => {
  for (const playNow of [false, true]) {
    const request = P.buildRequest({ ...draft('IMAGE'), deliveryMode: 'DIRECT', title: ' Hello ', text: ' World ', playNow }, null, ['epaper', 'speaker']);
    assert.deepEqual(request, { targets: ['epaper', 'speaker'], event: { title: 'Hello', content: 'World', play_now: playNow ? 1 : 0 } });
  }
  for (const changes of [{ title: '' }, { text: '' }]) {
    assert.throws(() => P.buildRequest({ deliveryMode: 'DIRECT', title: 'Hello', text: 'World', ...changes }, null, ['epaper']), /cannot be empty/);
  }
  assert.throws(() => P.buildRequest({ deliveryMode: 'DIRECT', title: 'Hello', text: 'x'.repeat(65536) }, null, ['epaper']), /64 KiB/);
});
