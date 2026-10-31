# Media relay protocol

[简体中文](media.zh-CN.md) · [README](../README.md)

## Upload and publish

Upload is asset creation, not publication. Uploads are private. `targets` must precede `file` in multipart order so Mid checks both identity and target ACL **before creating a temporary content file**. It accepts a comma-separated string or a JSON array of target device names. The original filename and declared MIME type are ignored for storage and validation.

```sh
curl https://mid.example.com/api/media \
  -H "Authorization: Bearer $PRODUCER_TOKEN" \
  -F 'targets=speaker,screen' -F 'file=@photo.jpg'
```

HTTP 201 returns `media_id` (random 128-bit hexadecimal), `mime_type`, `size_bytes`, `sha256` (lowercase hex), `expires_at` (Unix seconds UTC). Content is checked with Pillow or ffprobe; MP4 must contain H.264 video and optionally AAC audio, without other streams. Unsupported containers/codecs return 415; unavailable ffprobe returns 503. No conversion is performed. Validation checks format and decoding/probe structure, not compatibility with every terminal hardware decoder.

Publish using the same Producer identity and a subset of the upload's targets:

```sh
curl https://mid.example.com/api/send -H "Authorization: Bearer $PRODUCER_TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "targets":["speaker","screen"],
    "media_id":"ASSET_ID",
    "event":{"title":"Photo","media":{"url":"mid://ASSET_ID"}}
  }'
```

`event` is the existing terminal payload; keep the terminal's actual strict v2 schema. The minimal event above illustrates the URL placeholder, not a complete terminal v2 example. Mid leaves event fields unchanged. Hub replaces the exact `mid://ASSET_ID` URL with its local cache URL before terminal delivery. This version permits **one private asset per publish request** (`media_id`, or a single-element `media_ids` array). Multiple publications and multiple targets per asset are supported. All references must finish before deletion. Do not submit both ID forms or mix private IDs with external URLs.

Response includes `delivery_ids`, `publication_id`, `delivered` and `buffered`. Delivered counts successful WebSocket writes, not completed downloads. Pending deliveries remain durable in both cases. Every retry of POST `/api/send` is a new publication; Hub deduplicates replayed notifications using `delivery_id`. Producers should retain the successful response rather than automatically republishing after an ambiguous network failure.

## Mid → Hub delivery

Through the authenticated device WebSocket:

```json
{
  "type": "media.delivery",
  "delivery_id": "random-delivery-id",
  "publication_id": "random-publication-id",
  "event": {"media":{"url":"mid://ASSET_ID"}},
  "downloads": [{
    "media_id": "ASSET_ID",
    "url": "https://mid.example.com/api/media/ASSET_ID?ticket=SIGNED_JWT",
    "auth_mode": "DEVICE",
    "mime_type": "image/jpeg",
    "size_bytes": 12345,
    "sha256": "64-lowercase-hex-characters",
    "ticket_expires_at": 1793448300,
    "expires_at": 1793534400
  }]
}
```

`event` is terminal-facing. All download authorization/transport fields are in the outer `downloads` array, never injected into terminal `media`. Each device receives its own signed ticket. `delivery_id` identifies the durable delivery, `publication_id` identifies the publication. Unix timestamps are UTC seconds. `expires_at` is immutable; `ticket_expires_at` can change on refresh/replay.

### Download and refresh

```sh
curl "$DOWNLOAD_URL" -H "Authorization: Bearer $DEVICE_TOKEN" -o content.part
curl "$DOWNLOAD_URL" -H "Authorization: Bearer $DEVICE_TOKEN" -H 'Range: bytes=1048576-' -o tail.part
curl -X POST "https://mid.example.com/api/media/$MEDIA_ID/ticket" \
  -H "Authorization: Bearer $DEVICE_TOKEN"
```

Refresh returns a fresh download descriptor to a currently allowed Device, without changing asset expiration. Reconnect automatically replays every still-pending owned-media delivery with newly generated tickets. A connected Hub can refresh and retry HTTP after interruption/403 without republishing. Tickets permit multiple GETs and single byte Range requests until expiry. Ranges support `start-end`, `start-` and `-suffix`; invalid/multiple ranges return 416 with `Content-Range: bytes */SIZE`. Responses expose `Accept-Ranges`, `Content-Length`, SHA-256 `ETag` and `Content-Range` for 206. An unmatched `If-Range` returns the complete file. Finish integrity checks on the assembled complete file, not each range.

Download requires a valid Device Token **and** a valid HS256 JWT bound to asset, device, purpose `media.download`, issuer `pinepi-broadcast-mid` and audience `pinepi-hub-media`. No other algorithm is allowed. Token alone or ticket alone is insufficient. Refresh needs Device Token and current asset target permission. Asset ownership is checked again at publication; knowing another Producer's asset ID grants no rights. No public download or Producer delete endpoint exists.

### Cache confirmation

Only after Hub has downloaded all bytes, verified size and SHA-256, atomically saved the cache file **and durably saved the message/cache reference**, send:

```json
{"type":"media.cached","media_id":"ASSET_ID","delivery_id":"DELIVERY_ID","sha256":"COMPLETE_FILE_SHA256"}
```

Response is `{"type":"media.cached.result","status":"cached",...}` or an error with HTTP-style `code`/`detail`. Equivalent HTTP request:

```sh
curl "https://mid.example.com/api/media/$MEDIA_ID/cached" \
  -H "Authorization: Bearer $DEVICE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"delivery_id":"DELIVERY_ID","sha256":"COMPLETE_FILE_SHA256"}'
```

Device identity comes from the connection/token; a body `device` cannot select another device. Confirmation checks the delivery target and hash. Duplicates are successful, including after completed-asset deletion while the tombstone exists. A generic terminal message-saved ACK has no effect on media retention. HTTP/Range response completion and WebSocket write completion never mark cached.

## External URLs (`NONE`)

```json
{
  "targets": ["speaker"],
  "media_urls": ["https://cdn.example.org/video.mp4?external_signature=..."],
  "event": {"media":{"url":"https://cdn.example.org/video.mp4?external_signature=..."}}
}
```

`/api/send` adds outer `downloads:[{"url":"...","auth_mode":"NONE"}]`. It does not create a managed asset; external source files are never deleted. These use ordinary message delivery/buffering. Caller-supplied outer `downloads` or `auth_mode` is rejected. Mid URLs and `/api/media` paths cannot be submitted as external URLs, and switching to NONE cannot remove server-side download checks. Never infer auth mode from query parameter names, including `token`.

Mid does not fetch external URLs. It rejects non-HTTPS, userinfo, non-default ports, obvious private IPs/local names and Mid asset URLs at publication. **Hub must independently enforce the download boundary**: DEVICE credentials only to the exact configured trusted Mid HTTPS origin; no inherited Authorization on redirects (prefer rejecting all DEVICE redirects). NONE sends no Mid credential even for externally signed URLs. Resolve and validate every external destination and redirect, rejecting loopback, private, link-local, reserved and metadata addresses, including IPv6/mapped IPv4. Pin the validated address for the actual connection to resist DNS rebinding; check TLS hostname, bound redirects/time/bytes, and never forward Mid credentials. Publication-time hostname checks do not replace these Hub checks.

## Filesystem lifecycle

```
/var/lib/pinepi-broadcast-mid/media/
  .lock
  rates.json
  .upload-RANDOM             # only after target ACL check
  RANDOM_MEDIA_ID/
    content
    meta.json
```

`meta.json` stores hashed/stable uploader identity, allowed targets, MIME, actual size/hash, creation/deadline, state, publication events, and **all** delivery references and per-target `pending`/`cached`/`expired` states. No plaintext tokens/tickets are persisted. Atomic JSON replacement uses fsync; content is fsynced and renamed before metadata commit. Failed/abandoned upload files and orphan directories are cleaned. Upload attempts per Producer are durably rate-limited. Metadata corruption fails startup to preserve evidence rather than silently dropping pending notifications. Back up the complete data directory and configuration together while stopped.

All updates/download opens/cleanup use a process lock; a lifetime flock enforces one process. At most one active upload is accepted (others receive 429); each has a bounded duration. Actual bytes, multipart headers/targets, file count, format, producer capacity and total disk usage are bounded. Quotas include metadata/tombstones/orphans in service usage and retained content in producer usage; allow extra filesystem headroom for atomic metadata replacement. Count limits bound retained metadata. Uploads never use user filenames. JSON is limited independently, including chunked bodies; unauthenticated API requests are rejected before body reading/parsing.

After **every target of every publication** is cached, Mid deletes content after the grace period. An additional publication within that grace resets completion. An unpublished upload stays until the immutable deadline. At deadline, unfinished deliveries become `expired`, never `cached`. Downloads already opened retain their files until the last reader closes, even during cleanup. No new downloads/tickets are allowed after logical deletion/expiry. Tombstones keep outcomes for seven days, then are removed; content is not retained with them. Restart recovers state from disk; cleanup runs on startup and every interval. Delivery is replayed on reconnect, never assumed complete after sending. Terminal cache lifetime is independent: Mid deletion must not delete Hub content.

Status codes: 401 invalid/wrong credential role; 403 invalid/expired/mismatched ticket or forbidden Producer target; 404 unknown/inaccessible asset or delivery; 409 confirmation hash mismatch; 410 expired/deleted accessible asset; 413 size limit; 415 invalid/unsupported media; 422 malformed fields/order/targets/URL; 429 rate/capacity/active-upload/delivery limit; 507 service disk/count quota; 408 upload timeout; 503 media disabled/validator unavailable.

## Configuration defaults

All keys are under `media`. `signing_key_base64` has **no default**: generate an independent base64-encoded random key with at least 32 bytes. `public_base_url` must be your trusted public HTTPS origin (default example: `https://mid.example.com`), never inferred from request Host headers. Changing the key invalidates outstanding tickets; authorized Hubs can refresh.

| Key | Default |
|---|---:|
| `data_dir` | `/var/lib/pinepi-broadcast-mid/media` |
| `max_image_pixels` | 16000000 (16 megapixels) |
| `max_upload_bytes` | 104857600 (100 MiB) |
| `producer_capacity_bytes` | 524288000 (500 MiB) |
| `total_capacity_bytes` | 5368709120 (5 GiB) |
| `uploads_per_minute` | 10 (fixed 60-second window, includes failed attempts) |
| `retention_seconds` | 86400 (24 hours from upload) |
| `ticket_seconds` | 300 (5 minutes, capped by asset deadline) |
| `grace_seconds` | 60 |
| `cleanup_interval_seconds` | 30 |
| `upload_timeout_seconds` | 120 |
| `temporary_retention_seconds` | 600 |
| `tombstone_seconds` | 604800 (7 days) |
| `max_assets` | 10000 (includes tombstones) |
| `max_deliveries_per_asset` | 200 |

All numeric settings must be positive integers. Multipart request overhead allowance is 16 KiB, with targets and multipart headers each limited to 4 KiB. JSON requests remain 64 KiB; e-paper uploads allow 1 MiB plus overhead. Producer ownership can use a configured stable `id`; token rotation without it changes asset ownership. Removing a device denies its old credential after restart; pending references still expire at the original deadline.

## systemd and Nginx

The supplied service uses `StateDirectory=pinepi-broadcast-mid`, `StateDirectoryMode=0700`, `UMask=0077` and a writable `/var/lib/pinepi-broadcast-mid`. Config stays read-only to the application. Set matching `ReadWritePaths`/ownership if moving `data_dir`. Application code is installed together with `media_store.py`; FFmpeg is installed for validation. Public transport must be HTTPS/WSS.

Inside Nginx's TLS server (retain your certificate configuration):

```nginx
# http context: never log $request, $request_uri, query args or Authorization.
log_format mid_safe '$remote_addr $request_method $uri $status $body_bytes_sent';

# server context
access_log /var/log/nginx/mid-access.log mid_safe;
# Nginx error logs can include the original signed URL. Disable them for private media
# locations or configure a verified redacting log pipeline; don't use debug/request dumps.
location = /api/media {
    access_log /var/log/nginx/mid-access.log mid_safe;
    error_log /dev/null;
    client_max_body_size 101m; # 100 MiB file + 16 KiB multipart overhead
    client_body_timeout 120s;
    proxy_request_buffering off;
    proxy_http_version 1.1;
    proxy_pass http://127.0.0.1:8765;
    proxy_read_timeout 150s;
}
location ^~ /api/media/ {
    error_log /dev/null;
    client_max_body_size 64k;
    proxy_pass http://127.0.0.1:8765;
    proxy_buffering off;
    proxy_read_timeout 150s;
}
location = /api/send-image-2-13inch-touch-e-paper-hat-with-case/ {
    client_max_body_size 2m;
    proxy_request_buffering off;
    proxy_pass http://127.0.0.1:8765;
}
location /api/ {
    client_max_body_size 64k;
    proxy_request_buffering off;
    proxy_pass http://127.0.0.1:8765;
}
location /ws/ {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400;
}
```

Align proxy limits whenever changing app upload limits. Keep proxy buffering off to avoid unauthenticated spooling before app rejection. The application redacts `ticket` in Uvicorn access logs and never logs Authorization. Ensure load balancers, APM, HTTP clients and debugging logs follow the same policy; external signed URLs may also carry secrets. Do not expose the upstream port publicly.

## Separate Hub implementation

Hub must implement DEVICE/NONE handling and destination checks, interrupted-download retry and ticket refresh, exact size/SHA-256 verification, atomic persistent cache writes, durable message/media reference transaction, confirmation retry, terminal URL rewriting, delivery deduplication, and cache eviction based on terminal history references/capacity. An ACK is a trusted Hub assertion, not proof supplied by a JWT. Test terminal access after Mid cleanup with the actual Hub; Mid-only tests cannot establish that integration. Webpage/YouTube resolution, grabbing and general transcoding are out of scope.

Implementation references: [PyJWT verification API](https://pyjwt.readthedocs.io/en/latest/api.html) and [python-multipart streaming callbacks](https://multipart.fastapiexpert.com/api/).
