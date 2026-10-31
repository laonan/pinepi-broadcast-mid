/* PinePi Home v2 payload construction. Keep terminal objects free of transport fields. */
(function (root) {
  'use strict';
  const MIME = { IMAGE: ['image/jpeg', 'image/png'], AUDIO: ['audio/mpeg'], VIDEO: ['video/mp4'] };
  const PRESETS = {
    private: { kind: 'TEXT', textMode: 'PRIVATE', severity: 'NORMAL', source: '私人助理', title: '私人提醒', text: '这是一条隐私文本示例：请记得带上家门钥匙。' },
    tts: { kind: 'TEXT', textMode: 'TTS', severity: 'NORMAL', source: '早间新闻', title: '今日新闻', text: '早上好。这是一条正文朗读示例，请先准备与这段正文对应的 MP3 音频。', speechUrl: 'http://192.168.31.36:8766/media/news-example.mp3' },
    image: { kind: 'IMAGE', severity: 'NORMAL', source: '家庭相册', title: '今日照片', mediaUrl: 'http://192.168.31.36:8766/media/photo-example.jpg', mimeType: 'image/jpeg' },
    audio: { kind: 'AUDIO', severity: 'NORMAL', source: '家庭消息', title: '一段语音留言', mediaUrl: 'http://192.168.31.36:8766/media/audio-example.mp3', mimeType: 'audio/mpeg' },
    video: { kind: 'VIDEO', severity: 'NORMAL', source: '家庭消息', title: '一段视频', mediaUrl: 'http://192.168.31.36:8766/media/video-example.mp4', mimeType: 'video/mp4' },
    critical: { kind: 'TEXT', textMode: 'PRIVATE', severity: 'CRITICAL', source: '家庭安全（示例）', title: '安全确认（虚构示例）', text: '这是一条虚构的严重消息示例，请联系家人确认安全。' }
  };

  function requireValue(ok, message) { if (!ok) throw new Error(message); }
  function validateUrl(value) {
    requireValue(typeof value === 'string' && value.length > 0 && value.length <= 4096, 'Media URL is required (maximum 4096 UTF-16 units).');
    requireValue(/^https?:\/\//.test(value) && !value.includes('\\') && !/[\u0000-\u0020\u007f]/.test(value) && !value.includes('#') && !/^\w+:\/\/[^/?#]*@/.test(value), 'URLs cannot contain whitespace, user information or fragments.');
    let parsed;
    try { parsed = new URL(value); } catch { throw new Error('Enter a valid absolute media URL.'); }
    requireValue(!parsed.username && !parsed.password, 'URLs cannot contain user information.');
    if (parsed.protocol === 'https:') return value;
    const host = parsed.hostname;
    const parts = host.split('.');
    const privateIPv4 = parts.length === 4 && parts.every(p => /^\d{1,3}$/.test(p) && Number(p) <= 255) &&
      (Number(parts[0]) === 10 || (Number(parts[0]) === 172 && Number(parts[1]) >= 16 && Number(parts[1]) <= 31) ||
       (Number(parts[0]) === 192 && Number(parts[1]) === 168));
    requireValue(parsed.protocol === 'http:' && privateIPv4, 'Use HTTPS, or HTTP with a private IPv4 address (10.*, 172.16–31.*, 192.168.*).');
    return value;
  }

  function buildMessage(draft, identity) {
    requireValue(identity && /^[A-Za-z0-9_-]{1,80}$/.test(identity.id), 'Message ID must contain 1–80 ASCII letters, digits, underscores or hyphens.');
    requireValue(typeof identity.createdAt === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.test(identity.createdAt) && Number.isFinite(Date.parse(identity.createdAt)) && Date.parse(identity.createdAt) >= 0, 'created_at must be an ISO 8601 timestamp with timezone, from 1970 onward.');
    requireValue(['TEXT', 'IMAGE', 'AUDIO', 'VIDEO'].includes(draft.kind), 'Choose a message type.');
    const source = (draft.source || '').trim();
    const title = (draft.title || '').trim();
    requireValue(source.length > 0 && source.length <= 100, 'Source is required (maximum 100 UTF-16 units).');
    requireValue(title.length <= 200, 'Title must be at most 200 UTF-16 units.');
    requireValue(['', 'NORMAL', 'CRITICAL'].includes(draft.severity), 'Choose NORMAL, CRITICAL, or omit severity for older devices.');
    const message = { type: 'home.message', version: 2, id: identity.id, source, created_at: identity.createdAt, kind: draft.kind };
    if (draft.severity) message.severity = draft.severity;
    if (title) message.title = title;
    if (draft.kind === 'TEXT') {
      requireValue(['PRIVATE', 'TTS'].includes(draft.textMode), 'Choose PRIVATE or TTS for text.');
      const text = (draft.text || '').trim();
      requireValue(text.length > 0 && text.length <= 8192 && new TextEncoder().encode(text).length <= 32768, 'Text is required (maximum 8192 UTF-16 units / 32 KiB UTF-8).');
      message.text = text;
      message.text_mode = draft.textMode;
      if (draft.textMode === 'TTS') message.speech = { url: validateUrl((draft.speechUrl || '').trim()), mime_type: 'audio/mpeg' };
    } else {
      requireValue(MIME[draft.kind].includes(draft.mimeType), 'MIME type does not match the selected message type.');
      message.media = { url: validateUrl((draft.mediaUrl || '').trim()), mime_type: draft.mimeType };
    }
    return message;
  }

  function buildRequest(draft, identity, targets) {
    requireValue(Array.isArray(targets) && targets.length > 0 && targets.every(t => typeof t === 'string' && t.trim()), 'Enter at least one target device.');
    const request = { targets: [...new Set(targets)], event: buildMessage(draft, identity) };
    requireValue(new TextEncoder().encode(JSON.stringify(request)).length <= 65536, 'Request exceeds the Mid 64 KiB JSON limit.');
    return request;
  }

  const api = { MIME, PRESETS, validateUrl, buildMessage, buildRequest };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.PinePiProtocol = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
