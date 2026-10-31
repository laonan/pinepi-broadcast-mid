# PinePi Broadcast Mid

[简体中文](README.zh-CN.md) · [Media protocol](docs/media.md) · [媒体协议](docs/media.zh-CN.md)

FastAPI relay for authenticated Producers and named Hubs/devices. Text broadcasts and the independent e-paper conversion endpoint remain available. Producers can also upload private JPEG/PNG images, MP3 audio and H.264/AAC MP4 videos for delivery to Hubs. Mid temporarily stores media; Hubs retain their own caches for terminal history.

## Install

Requires Linux, Python 3.11–3.13, and FFmpeg (`ffprobe`).

```sh
git clone https://github.com/laonan/pinepi-broadcast-mid.git
cd pinepi-broadcast-mid
cp config.json.example config.json
# Edit tokens, media.public_base_url and media.signing_key_base64 first.
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' # run separately for each credential
python3 -c 'import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())' # signing key
sudo bash install.sh
```

Use the **same installer for every upgrade or repair**:

```sh
cd /home/ubuntu/pinepi-broadcast-mid # your source checkout
git pull --ff-only
sudo bash install.sh
```

The installer always preserves an existing `/etc/pinepi-broadcast-mid/config.json`, even if the checkout contains another `config.json`. On the first installation it uses the checkout's config; if neither exists, it creates a protected template and exits until you fill it in and rerun. It never offers to continue with placeholder credentials. Existing credentials are validated, never automatically rotated or merged. A legacy config without `media` stays media-disabled; add the media section explicitly to enable uploads.

Each run copies all application modules and web assets into a new permanent `/opt/pinepi-broadcast-mid/releases/<release>/` directory and creates a **fresh virtualenv there**, so upgrades also repair missing/broken dependencies in the old environment. Pip installation, `pip check`, dependency imports and application/configuration startup validation run before stopping the old service. The installer selects Python 3.11–3.13, installs FFmpeg and the matching venv package when needed, and validates configuration as the dedicated service user. If no supported Python exists, install one using your distribution's packages and run `sudo bash install.sh --python /usr/bin/python3.12` (choose the version actually installed).

After preflight, it switches `/opt/pinepi-broadcast-mid/current`, installs/reloads/enables the systemd unit, restarts it, and waits for local unauthenticated `POST /api/send` to return **401**. Startup/readiness failure restores the previous unit, release link and web files; a previously active service is restarted. An already broken old service cannot be made healthy by rollback. Failure before switching leaves the old runtime in place. Prepared and previous releases are retained for inspection; only remove inactive releases when you no longer need them. A deployment lock prevents simultaneous installers. Restart still drops ordinary in-memory text buffers; durable media deliveries recover from the separate data directory.

The web demo is deployed automatically to `/var/www/pinepi-broadcast-mid`, a symlink to the active release's web-only directory. A previous directory/link is retained beside it as `.before-<release>`. To update an existing **dedicated** Nginx document root, pass `sudo bash install.sh --web-dir /your/existing/site/root` on every upgrade. Do not choose a root shared with unrelated sites. The installer does not change your domain, TLS certificates or Nginx configuration; point your existing root/alias at that directory, or add this to the TLS server:

```nginx
location /demo/ {
    alias /var/www/pinepi-broadcast-mid/;
    index index.html;
}
```

Retain the API/WebSocket/media proxy rules in [the deployment reference](docs/media.md#systemd-and-nginx), then run `sudo nginx -t` and `sudo systemctl reload nginx` after proxy changes. This serves the demo at `https://YOUR_MID_HOST/demo/`; use `https://YOUR_MID_HOST/api/send` and a Producer Token in its connection settings.

Configuration stays at `/etc/pinepi-broadcast-mid/config.json` (0600); persistent media stays at `/var/lib/pinepi-broadcast-mid/` (0700) and is never replaced during deployment. Existing flat `/opt/pinepi-broadcast-mid` installations migrate through the same installer; their old files/venv remain available. The unit runs **one Uvicorn worker** through `current/venv/bin/python -m uvicorn`; a filesystem lock rejects concurrent media processes. Production code stays under `/opt` because the service's `ProtectHome=true` prevents running from the source checkout in `/home`.

For local development, start manually:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PINEPI_BROADCAST_CONFIG="$PWD/config.json" .venv/bin/uvicorn broadcast_server_mid:app \
  --app-dir python --host 127.0.0.1 --port 8765 --workers 1 --ws-max-size 65536
```

For local development choose a writable `media.data_dir`; download URLs still use the configured HTTPS origin. Removing the `media` section disables media endpoints and preserves text/e-paper service. An invalid media configuration fails startup.

## Identities

Both sections are keyed by stable, meaningful IDs. A Producer represents a sending application/integration; a Device represents a receiving Hub or adapter. Identity configuration example (keep your separate `media` section):

```json
{
  "producers": {
    "web-console": {
      "description": "Manual messages from the web console",
      "token": "REPLACE_WITH_PRODUCER_WEB_TOKEN",
      "targets": "*"
    },
    "morning-news": {
      "description": "Scheduled morning news",
      "token": "REPLACE_WITH_PRODUCER_NEWS_TOKEN",
      "targets": ["speaker"]
    }
  },
  "devices": {
    "speaker": {
      "description": "Living-room Hub",
      "token": "REPLACE_WITH_DEVICE_SPEAKER_TOKEN"
    },
    "epaper": {
      "description": "Kitchen e-paper display",
      "token": "REPLACE_WITH_DEVICE_EPAPER_TOKEN"
    }
  }
}
```

- Mapping keys (`web-console`, `speaker`) are stable identity IDs, at most 128 characters without whitespace/control characters. Producer IDs determine asset ownership and quota identity; device IDs are used in `targets`, WebSocket `device` and ticket binding. Keep them stable.
- `token` is required: a nonempty printable ASCII credential without whitespace. Every Producer Token and Device Token must be unique, including across roles. IDs are not credentials; send the token value, never the mapping key, as the HTTP Bearer credential.
- `description` is optional explanatory text (maximum 500 characters; defaults to empty). It can change without affecting identity, permissions or media. It is not sent to terminals or used for authentication.
- Producer `targets` is required: `"*"` for all configured devices, or an array of exact device IDs. An empty array grants no targets. Devices have no upload/publish permissions.

Token-keyed Producer configuration and per-entry legacy `id` overrides are **not supported**. Unknown identity fields, duplicate JSON keys, duplicate credentials and unknown ACL targets fail startup. Before upgrading, restructure `producers` in `/etc/pinepi-broadcast-mid/config.json` using the new example; preserve device IDs and distinct token values if keeping existing clients. The installer preserves production config and never converts or overwrites it. It validates the identity schema before dependency downloads, then performs full application startup validation before switching services.

Rotating `producers.<id>.token` under the same key preserves ownership, content-capacity accounting and durable upload-rate state. Normal JSON rate counters also use the Producer ID, but remain in memory and reset on process restart. Changing a Producer ID creates a different identity. New metadata records `producer_id` and an `owner` hash derived from that ID; no ownership fallback to credential hashes is performed. Old assets whose owner was derived from a token are not automatically reassigned. If the old configuration already used an explicit stable `id`, using that value as the new mapping key preserves its owner hash.

HTTP uses `Authorization: Bearer …`. Producers upload and publish; Device Tokens connect, download, refresh tickets and confirm caching. A separate random signing key of at least 32 bytes signs HS256 download tickets, stays on Mid, and is never a credential for uploads. Configuration changes require restart. The [media protocol](docs/media.md) specifies permissions, quotas, lifecycle and Hub responsibilities.

## Existing interfaces

Publish a JSON event (64 KiB maximum request, 100 requests/minute per Producer):

```sh
curl https://mid.example.com/api/send -H "Authorization: Bearer $PRODUCER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"targets":["speaker"],"event":{"title":"Hello","content":"World"}}'
```

`targets:["*"]` selects the Producer's permitted configured devices. Legacy requests without `event` are wrapped as `{"event": body}`. Ordinary text messages have an in-memory offline buffer (200 messages / 2 MiB per device); media deliveries have durable pending state.

The [web demo](examples/web/index.html) has four message types: TEXT, IMAGE, AUDIO and VIDEO. **Severity is independent**: NORMAL / CRITICAL applies to every type (Hub/terminal 0.8.4+); it is not a fifth message type. Switching examples preserves the selected severity. Text has PRIVATE / TTS modes. TTS is synthesized exclusively on the Hub: this form sends text without `speech`, performs no media upload for text and offers no narration URL input. Mid only relays the request.

For IMAGE/AUDIO/VIDEO choose a local JPEG/PNG (up to 8 MiB), MP3 (16 MiB) or H.264/AAC MP4 (100 MiB). Choose Upload file to create an asset first, or Upload & send to upload then publish in one flow. The form sends targets before file bytes, takes MIME/hash/size/asset ID from the validated server response, and publishes using `media_id`; there are no fake media URLs. Uploads have an indeterminate progress indicator and cancellation. Changing the file, type, targets or connection requires a fresh upload; severity/title changes reuse the asset. A failed publication can reuse the completed upload while it remains available. Browser history stores asset metadata, never file bytes, tickets or Device Tokens; unavailable media must be uploaded again.

The companion Hub must include its private Mid media cache and use its existing `--media-directory` / `--media-base-url` options. It downloads with its own Device Token, checks size/hash, atomically caches, persists the message with a LAN URL, and only then sends `media.cached`. Mid cleanup does not delete that Hub copy. The managed upload cache defaults to 512 MiB (`--media-cache-bytes`); it refuses new downloads when full and does not evict history-referenced files. Audio/video playback remains manual. The e-paper crop/send tool remains separate. Retries preserve IDs and are deduplicated; editing a message or choosing New ID creates a new message. Previously entered device IDs remain clickable below both target fields.


Host the static demo on the same origin as Mid (for example via Nginx), or configure explicit trusted-origin CORS in your proxy. A separately hosted page requires CORS for Bearer-authenticated requests. Browser settings and recent messages remain in localStorage. Frontend protocol checks: `node --test tests/test_web_protocol.cjs` (Node 18+).

Connect to `wss://mid.example.com/ws/` and send `{"device":"speaker","token":"DEVICE_TOKEN"}` within five seconds. Reply to `{"type":"ping"}` with `{"type":"pong"}`. A second connection replaces the first. See [Python subscriber](examples/subscribe_demo.py) and [web demo](examples/web/index.html).

The independent device adapter still accepts common Pillow-supported image formats, up to 1 MiB:

```sh
curl https://mid.example.com/api/send-image-2-13inch-touch-e-paper-hat-with-case/ \
  -H "Authorization: Bearer $PRODUCER_TOKEN" -F 'targets=epaper' -F 'image=@photo.png'
```

It sends a raw 4000-byte binary WebSocket frame (250×122 monochrome), only to connected devices; it does not create media assets or offline binary deliveries.

## Media and deployment

See [the full protocol and configuration defaults](docs/media.md) for upload, publish, ticket refresh, Range download, completion confirmation, Nginx setup and failure recovery. No SQLite, Redis, external URL fetching, webpage extraction or transcoding is used. The private media cache and terminal URL rewriting live in the companion Hub project; deploy its matching version as well as Mid.

```sh
sudo systemctl restart pinepi-broadcast-mid
journalctl -u pinepi-broadcast-mid -f
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests require `ffmpeg` and `ffprobe` and cover security, delivery persistence, retries, Range requests, cleanup and compatibility. MIT License.

To run the two-repository upload/cache/cleanup integration checks, set `PINEPI_HUB_SOURCE=/path/to/pinepi-thinksmart-view/hub/src` when running `pytest tests/test_media.py`. The checks use local ASGI transports and fake credentials, with no external provider calls.
