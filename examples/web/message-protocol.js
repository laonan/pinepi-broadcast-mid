/* Producer submissions to the Hub via Mid. Hub-generated TTS omits speech here;
 * the Hub adds real audio before validating/delivering strict terminal v2 JSON. */
(function (root) {
  'use strict';
  const MIME = { IMAGE: ['image/jpeg', 'image/png'], AUDIO: ['audio/mpeg'], VIDEO: ['video/mp4'] };
  const FILE_LIMITS = { IMAGE: 8 * 1024 * 1024, AUDIO: 16 * 1024 * 1024, VIDEO: 100 * 1024 * 1024 };
  const ACCEPT = { IMAGE: 'image/jpeg,image/png,.jpg,.jpeg,.png', AUDIO: 'audio/mpeg,.mp3', VIDEO: 'video/mp4,.mp4' };
  const PRESETS = {
    text: { kind: 'TEXT', source: '家庭消息', title: '一条消息', text: '你好，这是一条发送给家人的消息。', textMode: 'PRIVATE' },
    image: { kind: 'IMAGE', source: '家庭相册', title: '今日照片' },
    audio: { kind: 'AUDIO', source: '家庭消息', title: '一段语音留言' },
    video: { kind: 'VIDEO', source: '家庭消息', title: '一段视频' }
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
      // TTS is entirely a Hub responsibility. Stale URL/upload fields in a
      // draft must never turn a text submission into a media or speech request.
    } else {
      const asset = validateAsset(draft.uploadedMedia, draft.kind);
      message.media = { url: `mid://${asset.media_id}`, mime_type: asset.mime_type };
    }
    return message;
  }

  function buildRequest(draft, identity, targets) {
    requireValue(Array.isArray(targets) && targets.length > 0 && targets.every(t => typeof t === 'string' && t.trim()), 'Enter at least one target device.');
    const request = { targets: [...new Set(targets)], event: buildMessage(draft, identity) };
    if (draft.kind !== 'TEXT') request.media_id = draft.uploadedMedia.media_id;
    requireValue(new TextEncoder().encode(JSON.stringify(request)).length <= 65536, 'Request exceeds the Mid 64 KiB JSON limit.');
    return request;
  }

  function validateFile(file, kind) {
    requireValue(FILE_LIMITS[kind], 'Choose an image, audio or video message.');
    requireValue(file && Number.isInteger(file.size) && file.size > 0, 'Choose a nonempty local file first.');
    requireValue(file.size <= FILE_LIMITS[kind], `${kind} files must be at most ${FILE_LIMITS[kind] / 1024 / 1024} MiB.`);
    return file;
  }

  function validateAsset(asset, kind) {
    requireValue(asset && /^[0-9a-f]{32}$/.test(asset.media_id || ''), 'Choose a local file and upload it before publishing.');
    requireValue(MIME[kind]?.includes(asset.mime_type), 'The uploaded file type does not match the selected message type.');
    requireValue(Number.isInteger(asset.size_bytes) && asset.size_bytes > 0 && asset.size_bytes <= FILE_LIMITS[kind], 'Uploaded file exceeds the terminal size limit.');
    requireValue(/^[0-9a-f]{64}$/.test(asset.sha256 || '') && Number.isFinite(asset.expires_at), 'Invalid upload metadata returned by Mid.');
    requireValue(asset.expires_at > Date.now() / 1000, 'This upload has expired. Upload the file again.');
    return asset;
  }

  function uploadEndpoint(apiUrl) {
    const url = new URL(apiUrl);
    requireValue(['http:', 'https:'].includes(url.protocol) && !url.username && !url.password && !url.search && !url.hash && /^\/api\/send\/?$/.test(url.pathname), 'Use the Broadcast Mid /api/send endpoint.');
    if (url.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) validateUrl(url.href);
    url.pathname = '/api/media';
    return url.href;
  }

  function needsHistoryRefresh(event, asset) {
    if (event.kind === 'TEXT') return Object.hasOwn(event, 'speech');
    return !asset || event.media?.url !== `mid://${asset.media_id}`;
  }

  const api = { MIME, FILE_LIMITS, ACCEPT, PRESETS, validateUrl, validateFile, validateAsset, uploadEndpoint, buildMessage, buildRequest, needsHistoryRefresh };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.PinePiProtocol = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
