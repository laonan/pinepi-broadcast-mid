# PinePi Broadcast Mid

[简体中文](README.zh-CN.md) · [Media protocol](docs/media.md) · [媒体协议](docs/media.zh-CN.md)

FastAPI relay for authenticated Producers and named Hubs/devices. Text broadcasts and the independent e-paper conversion endpoint remain available. Producers can also upload private JPEG/PNG images and H.264/AAC MP4 videos for delivery to Hubs. Mid temporarily stores media; Hubs retain their own caches for terminal history.

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

`producers` maps Producer Tokens to `{"targets":"*"}` or `{"targets":["speaker"]}`. `devices` maps device names to `{"token":"DEVICE_TOKEN"}`. **Device Tokens must be unique and separate from Producer Tokens**; configurations that reused a token must be migrated. Optional producer `id` is a stable, unique identity for ownership across token rotation. Otherwise ownership is the SHA-256 of the Producer Token. Never give different producers the same `id`.

HTTP uses `Authorization: Bearer …`. Producers upload and publish; Device Tokens connect, download, refresh tickets and confirm caching. A separate random signing key of at least 32 bytes signs HS256 download tickets, stays on Mid, and is never a credential for uploads. Configuration changes require restart. The [media protocol](docs/media.md) specifies permissions, quotas, lifecycle and Hub responsibilities.

## Existing interfaces

Publish a JSON event (64 KiB maximum request, 100 requests/minute per Producer):

```sh
curl https://mid.example.com/api/send -H "Authorization: Bearer $PRODUCER_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"targets":["speaker"],"event":{"title":"Hello","content":"World"}}'
```

`targets:["*"]` selects the Producer's permitted configured devices. Legacy requests without `event` are wrapped as `{"event": body}`. Ordinary text messages have an in-memory offline buffer (200 messages / 2 MiB per device); media deliveries have durable pending state.

The [web demo](examples/web/index.html) includes complete PinePi Home v2 examples for TEXT/PRIVATE, TEXT/TTS, IMAGE, AUDIO and VIDEO. Choose NORMAL or CRITICAL (Hub/terminal 0.8.4+), or omit severity for older v2 implementations. The form shows only fields belonging to the selected type, validates the payload, and offers a JSON preview/copy. TTS requires a pre-generated narration MP3 URL; this demo does not call a speech service. Audio/video playback remains manual. Replace example Hub addresses and filenames with reachable, persistent media URLs; examples do not upload media or include sample media files. Retries preserve IDs, and editing the message generates a new ID. Recent messages restore the original complete event for retry; old text history restores as PRIVATE. Previously entered target device names appear as clickable shortcuts below both target fields, with up to 20 unique names retained in this browser. The e-paper crop/send tool remains separate.

Host the static demo on the same origin as Mid (for example via Nginx), or configure explicit trusted-origin CORS in your proxy. A separately hosted page requires CORS for Bearer-authenticated requests. Browser settings and recent messages remain in localStorage. Frontend protocol checks: `node --test tests/test_web_protocol.cjs` (Node 18+).

Connect to `wss://mid.example.com/ws/` and send `{"device":"speaker","token":"DEVICE_TOKEN"}` within five seconds. Reply to `{"type":"ping"}` with `{"type":"pong"}`. A second connection replaces the first. See [Python subscriber](examples/subscribe_demo.py) and [web demo](examples/web/index.html).

The independent device adapter still accepts common Pillow-supported image formats, up to 1 MiB:

```sh
curl https://mid.example.com/api/send-image-2-13inch-touch-e-paper-hat-with-case/ \
  -H "Authorization: Bearer $PRODUCER_TOKEN" -F 'targets=epaper' -F 'image=@photo.png'
```

It sends a raw 4000-byte binary WebSocket frame (250×122 monochrome), only to connected devices; it does not create media assets or offline binary deliveries.

## Media and deployment

See [the full protocol and configuration defaults](docs/media.md) for upload, publish, ticket refresh, Range download, completion confirmation, Nginx setup and failure recovery. No SQLite, Redis, external URL fetching, webpage extraction or transcoding is used. Hub caching and terminal URL rewriting are implemented in the separate Hub project.

```sh
sudo systemctl restart pinepi-broadcast-mid
journalctl -u pinepi-broadcast-mid -f
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests require `ffmpeg` and `ffprobe` and cover security, delivery persistence, retries, Range requests, cleanup and compatibility. MIT License.
