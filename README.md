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

The installer creates a dedicated service user, installs dependencies and FFmpeg, and starts systemd. Configuration is `/etc/pinepi-broadcast-mid/config.json` (mode 0600); persistent state is `/var/lib/pinepi-broadcast-mid/` (mode 0700). Configure HTTPS/WSS at the reverse proxy. Run **one Uvicorn worker**, without reload; a filesystem lock rejects a second media process using the same directory. Local/manual startup:

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
