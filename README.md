# pinepi-broadcast-mid — Multi-Device WebSocket Broadcast Service

A lightweight FastAPI + WebSocket server that routes messages from producers to multiple named devices. Each device authenticates with its own token. Producers authenticate separately and can target one or more devices per message.

---

## Architecture

```
                     +------------------+
                     | Broadcast Server |
                     +------------------+
                              ▲
                              │
                    POST /api/send
                              │
                          Producer

         ┌────────────────────┼────────────────────┐
         ▼                    ▼                    ▼

   device:speaker      device:epaper      device:other
      WebSocket          WebSocket          WebSocket
```

---

## Installation

### Manual Installation

```bash
# 1. get source
git clone https://github.com/laonan/pinepi-broadcast-mid.git
cd pinepi-broadcast-mid

# 2. Fill in your tokens
cp config.json.example config.json
nano config.json

# 3. Install (requires root, Debian/Ubuntu)
sudo bash install.sh
```


### Quick Install Script

```bash
curl -fsSL https://raw.githubusercontent.com/laonan/pinepi-broadcast-mid/main/install.sh | sudo bash

# After installation:
cp /etc/pinepi-broadcast-mid/config.json.example /etc/pinepi-broadcast-mid/config.json
nano /etc/pinepi-broadcast-mid/config.json
sudo systemctl restart pinepi-broadcast-mid
```

The script installs to `/opt/pinepi-broadcast-mid`, creates a Python venv, installs dependencies, and registers a systemd service (`pinepi-broadcast-mid`).

---

## Configuration

Edit `config.json` :

```json
{
  "producers": {
    "<producer_token>": { "targets": "*" },
    "<producer_speaker_token>": { "targets": ["speaker"] },
    "<producer_epaper_token>": { "targets": ["epaper"] }
  },
  "devices": {
    "speaker": { "token": "<producer_speaker_token>" },
    "epaper":  { "token": "<producer_epaper_token>" }
  }
}
```

- **`producers`** — map of producer tokens to their allowed targets (`"*"` = all devices, or an array like `["speaker"]`)
- **`devices`** — map of device names to their WebSocket auth tokens

`config.json` is installed to `/etc/pinepi-broadcast-mid/config.json` with `chmod 600`.

---

## WebSocket Auth (Device Side)

After connecting, the device must send a JSON auth message within 5 seconds:

```json
{ "token": "<device_token>", "device": "speaker" }
```

The server validates the token against the device name in `/etc/pinepi-broadcast-mid/config.json`. On success the connection is kept alive with heartbeat pings.

---

## HTTP API (Producer Side)

### `POST /api/send`

**Header:** `Authorization: Bearer <producer_token>`

**Body:**
```json
{
  "targets": ["speaker", "epaper"],
  "event": { "type": "...", "data": "..." }
}
```

- `targets` — array of device names, or `["*"]` to broadcast to all
- `event` — arbitrary JSON payload forwarded to each target device

Messages are buffered per device if the target is currently offline and delivered on reconnect.

---

## Examples

### Send a message via `curl`

Send to a single device:
```bash
curl -X POST https://your-domain/api/send \
  -H "Authorization: Bearer <producer_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "targets": ["speaker"],
    "event": { "title": "Hello", "content": "World", "play_now": 1 }
  }'
```

Broadcast to all devices:
```bash
curl -X POST https://your-domain/api/send \
  -H "Authorization: Bearer <producer_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "targets": ["*"],
    "event": { "title": "Announcement", "content": "Reboot in 5 min" }
  }'
```

Fan-out to multiple specific devices:
```bash
curl -X POST https://your-domain/api/send \
  -H "Authorization: Bearer <producer_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "targets": ["speaker", "epaper"],
    "event": { "title": "Alert", "content": "Sensor triggered" }
  }'
```

Send an image to the Waveshare 2.13" e-paper screen:
```bash
curl -X POST https://your-domain/api/send-image-2-13inch-touch-e-paper-hat-with-case/ \
  -H "Authorization: Bearer <producer_token>" \
  -F "targets=epaper" \
  -F "image=@/path/to/photo.jpg"
```

Fan-out image to multiple devices:
```bash
curl -X POST https://your-domain/api/send-image-2-13inch-touch-e-paper-hat-with-case/ \
  -H "Authorization: Bearer <producer_token>" \
  -F "targets=epaper,other_epaper" \
  -F "image=@/path/to/photo.png"
```

- Any common image format is accepted (JPEG, PNG, BMP, etc.)
- The image is automatically resized to 122×250 and converted to 1-bit monochrome
- Original file must be ≤ 1 MB; larger files are rejected with HTTP 413
- The device receives a raw 4000-byte binary WebSocket frame (no JSON wrapper)

Image endpoint success response:
```json
{ "status": "ok", "delivered": 1, "size_bytes": 4000 }
```

---

Example `/api/send` success response:
```json
{ "status": "ok", "delivered": 2, "buffered": 0 }
```

---

### Subscribe over WebSocket (device side)

**Python example (text messages):**
```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("wss://your-domain/ws/") as ws:
        # Step 1: authenticate
        await ws.send(json.dumps({"token": "<device_token>", "device": "speaker"}))

        # Step 2: receive messages
        async for message in ws:
            # Handle binary frames (e-paper images) and text frames (JSON)
            if isinstance(message, bytes):
                print("Received binary image data:", len(message), "bytes")
                # Process binary data for e-paper display
                continue
            
            # Handle text frames (JSON)
            data = json.loads(message)
            if data.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            else:
                # New format: extract event data
                event = data.get("event", data)
                print("Received event:", event)

asyncio.run(main())
```

**Python example (e-paper image display):**
```python
import asyncio, json, websockets
import epaper_driver  # Your e-paper display driver

async def main():
    async with websockets.connect("wss://your-domain/ws/") as ws:
        # Step 1: authenticate
        await ws.send(json.dumps({"token": "<device_token>", "device": "epaper"}))

        # Step 2: receive messages
        async for message in ws:
            # Handle binary frames (images) and text frames (ping/pong)
            if isinstance(message, bytes):
                # Binary frame: 4000 bytes for 250x122 e-paper display
                if len(message) == 4000:
                    print("Displaying image on e-paper")
                    epaper_driver.display_image(message)  # Send to e-paper driver
                else:
                    print(f"Unexpected binary data: {len(message)} bytes")
            else:
                # Text frame: JSON messages (ping/pong)
                data = json.loads(message)
                if data.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                else:
                    # New format: extract event data
                    event = data.get("event", data)
                    print("Received event:", event)

asyncio.run(main())
```

**JavaScript (browser) example:**
```js
const ws = new WebSocket("wss://your-domain/ws/");

ws.onopen = () => {
  ws.send(JSON.stringify({ token: "<device_token>", device: "epaper" }));
};

ws.onmessage = (e) => {
  // Handle binary frames (e-paper images) and text frames (JSON)
  if (e.data instanceof Blob) {
    console.log("Received binary image data");
    // Process binary data for e-paper display
    return;
  }
  
  const data = JSON.parse(e.data);
  if (data.type === "ping") {
    ws.send(JSON.stringify({ type: "pong" }));
  } else {
    // New format: extract event data
    const event = data.event || data;
    console.log("Received event:", event);
  }
};
```

### Web Demo (Optional)

The `examples/web/` directory contains a lightweight static web console for testing and demonstration.

- No backend required
- Runs directly in browser
- Stores credentials in browser localStorage
- Useful for quick prototyping and debugging device messaging

---

## Service Management

```bash
# View logs
journalctl -u pinepi-broadcast-mid -f

# Restart
sudo systemctl restart pinepi-broadcast-mid

# Status
sudo systemctl status pinepi-broadcast-mid
```

---

## Nginx Proxy Config

```nginx
http {
  limit_req_zone $binary_remote_addr zone=ws_limit:10m rate=2r/s;
  # ...
}


# WebSocket endpoint
location /ws/ {
    limit_req zone=ws_limit burst=10 nodelay;
    limit_req_status 429;

    proxy_pass http://127.0.0.1:8765/ws/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "Upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 86400;
}

# API endpoints
location /api/ {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    
    # Security headers
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;
    add_header X-XSS-Protection "1; mode=block";
    
    # Rate limiting (optional, adjust as needed)
    # limit_req zone=api burst=20 nodelay;
}

# Rate limiting zone (add to http block)
# limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;
```

## License

MIT License