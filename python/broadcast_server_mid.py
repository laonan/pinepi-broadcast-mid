import asyncio
import io
import os
import time
import json
import logging
import contextlib
import hmac
import re
from urllib.parse import urlsplit
from fastapi.responses import StreamingResponse, JSONResponse
from media_store import MediaStore, fail
from identities import Identities, unique_object
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, UploadFile, File, Form, Request
from collections import deque
from PIL import Image
ImageDecompressionBombError = Image.DecompressionBombError
from typing import Dict
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

class RedactTickets(logging.Filter):
    def filter(self, record):
        def redact(value):
            return re.sub(r"(?i)(ticket=)[^ &\\\"]+", r"\1[REDACTED]", value) if isinstance(value, str) else value
        record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(v) for v in record.args)
        return True

logging.getLogger("uvicorn.access").addFilter(RedactTickets())

@contextlib.asynccontextmanager
async def lifespan(app):
    global MEDIA
    MEDIA = MediaStore(_config.get("media", {})) if _config.get("media") else None
    if MEDIA and any(MEDIA.key == token.encode() or _config["media"]["signing_key_base64"] == token
                     for token in IDENTITIES.tokens):
        MEDIA.close()
        raise RuntimeError("Media signing key must be independent from Producer/Device credentials")
    async def maintenance():
        while True:
            await asyncio.sleep(MEDIA.cfg["cleanup_interval_seconds"])
            try:
                MEDIA.cleanup()
            except Exception:
                logger.exception("Media cleanup failed; retrying next interval")
    task = asyncio.create_task(maintenance()) if MEDIA else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if MEDIA:
            MEDIA.close()

app = FastAPI(lifespan=lifespan)
MEDIA = None

# Configure Pillow limits once at startup to prevent decompression bombs
# Keep the legacy adapter limit; generic uploads enforce a separate configurable pixel cap.
Image.MAX_IMAGE_PIXELS = 8000 * 8000

# ---------------- Config ----------------

_CONFIG_PATH = os.environ.get(
    "PINEPI_BROADCAST_CONFIG",
    "/etc/pinepi-broadcast-mid/config.json"
)

def _load_config() -> dict:
    path = os.path.abspath(_CONFIG_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f, object_pairs_hook=unique_object)
    except FileNotFoundError:
        raise RuntimeError(f"config.json not found at {path}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"config.json is invalid JSON: {e}")

_config = _load_config()

# Public identity names and credential lookup are kept separate. No token-keyed
# producer configuration or per-entry legacy `id` fallback is accepted.
IDENTITIES = Identities(_config)
logger.info("Loaded Producers %s; devices %s", list(IDENTITIES.producers), list(IDENTITIES.devices))

# ---------------- Client tracking ----------------

HEARTBEAT_INTERVAL = 30   # seconds between server pings
HEARTBEAT_TIMEOUT  = 10   # seconds to wait for pong
OFFLINE_BUFFER_MAX = 200  # max messages kept per client while offline
MAX_PAYLOAD_SIZE = 64 * 1024  # 64 KB max JSON payload
MAX_OFFLINE_BYTES = 2 * 1024 * 1024  # 2 MB max buffered per device
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 100  # max requests per window per producer

@dataclass
class RateLimitEntry:
    count: int
    window_start: float

# Rate limiting per stable Producer ID
PRODUCER_RATE_LIMITS: Dict[str, RateLimitEntry] = {}

class ClientState:
    """Tracks one connected (or recently disconnected) WebSocket client."""
    _next_id = 0

    def __init__(self, ws: WebSocket, device_name: str):
        ClientState._next_id += 1
        self.id = ClientState._next_id
        self.ws = ws
        self.device_name = device_name
        self.connected = True
        self.last_pong = time.monotonic()
        self.offline_buffer: deque[str] = deque(maxlen=OFFLINE_BUFFER_MAX)

CLIENTS: dict[int, ClientState] = {}            # conn id -> ClientState (connected)
CONNECTED_BY_DEVICE: dict[str, ClientState] = {} # device_name -> ClientState (connected)
OFFLINE_CLIENTS: dict[str, ClientState] = {}    # device_name -> ClientState (offline buffer)

# ---------------- Helpers ----------------

async def safe_send(client: ClientState, text: str) -> bool:
    """Send text to a client; return False if the send fails."""
    try:
        await client.ws.send_text(text)
        return True
    except Exception:
        return False

async def safe_send_bytes(client: ClientState, data: bytes) -> bool:
    """Send a binary frame to a client; return False if the send fails."""
    try:
        await client.ws.send_bytes(data)
        return True
    except Exception:
        return False

async def flush_offline_buffer(client: ClientState):
    """Send all buffered messages to a freshly reconnected client."""
    while client.offline_buffer:
        msg = client.offline_buffer.popleft()
        ok = await safe_send(client, msg)
        if not ok:
            client.offline_buffer.appendleft(msg)
            break
    if not client.offline_buffer:
        logger.info("Client %s (%s): offline buffer flushed", client.id, client.device_name)


def producer_identity(token: str):
    producer = IDENTITIES.producer_for_token(token)
    if producer is None:
        fail(401, "Invalid Producer Token")
    return producer


def _resolve_targets(producer_token: str, requested: list[str]) -> list[str]:
    """Return the list of device names to deliver to, enforcing producer ACL.

    Raises HTTPException 401 if the token is unknown, 403 if a requested
    device is outside the producer's allowed targets.
    """
    if not isinstance(requested, list) or not requested or any(not isinstance(d, str) or not d for d in requested):
        fail(422, "targets must be a nonempty list of device names")
    producer = producer_identity(producer_token)
    allowed = producer.targets
    all_devices = list(IDENTITIES.devices)
    if allowed == "*":
        allowed_set = set(all_devices)
    else:
        allowed_set = set(allowed)
    if "*" in requested:
        resolved = [d for d in all_devices if d in allowed_set]
        if not resolved:
            fail(403, "Producer has no permitted target devices")
        return resolved
    for d in requested:
        if d not in allowed_set:
            raise HTTPException(status_code=403, detail=f"Producer not allowed to target device '{d}'")
    if any(d not in IDENTITIES.devices for d in requested):
        fail(422, "Unknown target device")
    return list(dict.fromkeys(requested))


def _ensure_offline_buffer(device_name: str) -> ClientState:
    """Return or create a placeholder offline ClientState for a device."""
    if device_name not in OFFLINE_CLIENTS:
        placeholder = object.__new__(ClientState)
        placeholder.id = 0
        placeholder.ws = None
        placeholder.device_name = device_name
        placeholder.connected = False
        placeholder.last_pong = 0
        placeholder.offline_buffer = deque(maxlen=OFFLINE_BUFFER_MAX)
        OFFLINE_CLIENTS[device_name] = placeholder
    return OFFLINE_CLIENTS[device_name]

# ---------------- Heartbeat ----------------

async def heartbeat_loop(client: ClientState):
    """Periodically ping the client; disconnect if no pong arrives."""
    try:
        while client.connected:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if not client.connected:
                break
            try:
                await client.ws.send_json({"type": "ping", "ts": time.time()})
            except Exception:
                break
            # Wait for pong (the receive loop handles the actual pong message
            # and updates last_pong; here we just check the timestamp)
            await asyncio.sleep(HEARTBEAT_TIMEOUT)
            if time.monotonic() - client.last_pong > HEARTBEAT_INTERVAL + HEARTBEAT_TIMEOUT:
                logger.warning("Client %s: heartbeat timeout, disconnecting", client.id)
                client.connected = False
                try:
                    await client.ws.close()
                except Exception:
                    pass
                break
    except asyncio.CancelledError:
        pass

# ---------------- WebSocket endpoint ----------------

@app.websocket("/ws/")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()

    # --- Auth: expect JSON {"token": "...", "device": "..."} ---
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=5)
        auth = json.loads(raw)
        device_name = auth.get("device", "")
        token = auth.get("token", "")
    except asyncio.TimeoutError:
        try:
            await ws.send_json({"error": "Authentication timeout", "code": 4002})
        except Exception:
            pass
        await ws.close(code=4002, reason="Authentication timeout")
        return
    except WebSocketDisconnect:
        await ws.close()
        return
    except (json.JSONDecodeError, AttributeError):
        await ws.close(code=4001, reason="invalid auth message")
        return

    device = IDENTITIES.devices.get(device_name) if isinstance(device_name, str) else None
    if not device or not isinstance(token, str) or not hmac.compare_digest(token.encode(), device.token.encode()):
        await ws.close(code=4001, reason="invalid token or unknown device")
        return

    # --- Setup client ---
    # Terminate any existing connection for this device
    existing = CONNECTED_BY_DEVICE.get(device_name)
    if existing and existing.connected:
        logger.info("Terminating existing connection for device %s (client %s)", device_name, existing.id)
        existing.connected = False
        try:
            await existing.ws.close()
        except Exception:
            pass
        CLIENTS.pop(existing.id, None)
        CONNECTED_BY_DEVICE.pop(device_name, None)

    client = ClientState(ws, device_name)
    CLIENTS[client.id] = client
    CONNECTED_BY_DEVICE[device_name] = client
    logger.info("Client %s (%s) connected from %s", client.id, device_name, ws.client)

    # Flush any offline buffer from a previous session
    if device_name in OFFLINE_CLIENTS:
        old = OFFLINE_CLIENTS.pop(device_name)
        client.offline_buffer = old.offline_buffer
        logger.info("Client %s (%s): restoring %d buffered messages",
                    client.id, device_name, len(client.offline_buffer))
        await flush_offline_buffer(client)

    if MEDIA:
        for envelope in MEDIA.pending(device_name):
            if not await safe_send(client, json.dumps(envelope, ensure_ascii=False)):
                break

    # Start heartbeat
    hb_task = asyncio.create_task(heartbeat_loop(client))

    # --- Receive loop ---
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "pong":
                    client.last_pong = time.monotonic()
                elif msg.get("type") == "media.cached" and MEDIA:
                    try:
                        result = MEDIA.acknowledge(msg.get("media_id"), msg.get("delivery_id"), msg.get("sha256"), device_name)
                        await ws.send_json({"type": "media.cached.result", **result})
                    except HTTPException as exc:
                        await ws.send_json({"type": "media.cached.result", "status": "error", "code": exc.status_code, "detail": exc.detail})
            except (json.JSONDecodeError, AttributeError):
                pass
    except WebSocketDisconnect:
        logger.info("Client %s (%s) disconnected", client.id, device_name)
    except Exception as e:
        logger.warning("Client %s (%s) error: %s", client.id, device_name, e)
    finally:
        client.connected = False
        hb_task.cancel()
        CLIENTS.pop(client.id, None)
        # Only remove from CONNECTED_BY_DEVICE if this is still the current client
        if CONNECTED_BY_DEVICE.get(device_name) is client:
            CONNECTED_BY_DEVICE.pop(device_name, None)
        OFFLINE_CLIENTS[device_name] = client
        logger.info("Client %s (%s) moved to offline (buffer: %d msgs)",
                    client.id, device_name, len(client.offline_buffer))

# ---------------- HTTP broadcast endpoint ----------------

def _check_rate_limit(producer_token: str) -> None:
    """Rotating a credential must not create a new Producer quota identity."""
    producer_id = producer_identity(producer_token).id
    now = time.monotonic()
    entry = PRODUCER_RATE_LIMITS.get(producer_id)
    if entry is None or now - entry.window_start > RATE_LIMIT_WINDOW:
        PRODUCER_RATE_LIMITS[producer_id] = RateLimitEntry(count=1, window_start=now)
        return
    if entry.count >= RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    entry.count += 1

def _get_buffer_size_bytes(client: ClientState) -> int:
    """Calculate total bytes in offline buffer."""
    return sum(len(msg.encode('utf-8')) for msg in client.offline_buffer)

@app.post("/api/send")
async def send_message(request: Request, body: dict, authorization: str = Header(None)):
    producer_token = (authorization or "").removeprefix("Bearer ")
    
    # Validate token first
    targets = _resolve_targets(producer_token, body.get("targets", ["*"]))
    
    # Rate limiting (after token validation)
    _check_rate_limit(producer_token)
    if ("media_id" in body and "media_ids" in body) or ("media_urls" in body and ("media_id" in body or "media_ids" in body)):
        fail(422, "Use exactly one media reference form")
    ids = body.get("media_ids", [body["media_id"]] if "media_id" in body else [])
    if "downloads" in body or "auth_mode" in body:
        fail(422, "Download descriptors are generated by Mid")
    if not isinstance(ids, list) or len(ids) > 1 or any(not isinstance(i, str) for i in ids):
        fail(422, "media_ids must be an array of at most one ID")
    if ids:
        store = media_store()
        # Persist before sending: WebSocket success never marks cached.
        event = body.get("event", {k: v for k, v in body.items() if k not in ("media_id", "media_ids", "targets")})
        deliveries = store.publish(ids, producer_identity(producer_token).id, targets, event)
        logger.info("Media published producer=%s media=%s targets=%s", producer_identity(producer_token).id, ids, targets)
        delivered = 0
        for meta, delivery in deliveries:
            for device in targets:
                client = CONNECTED_BY_DEVICE.get(device)
                if client and await safe_send(client, json.dumps(store.envelope(meta, delivery, device), ensure_ascii=False)):
                    delivered += 1
        return {"status": "ok", "delivered": delivered, "buffered": len(deliveries) * len(targets) - delivered,
                "delivery_ids": [d["delivery_id"] for _, d in deliveries], "publication_id": deliveries[0][1]["publication_id"]}
    if "media_urls" in body:
        urls = body["media_urls"]
        if not isinstance(urls, list) or not 1 <= len(urls) <= 16:
            fail(422, "media_urls must contain 1–16 HTTPS URLs")
        for url in urls:
            validate_external_url(url)
        # The Hub performs DNS/IP and redirect checks before every external request.
        body = {"event": body.get("event", {}), "downloads": [
            {"url": url, "auth_mode": "NONE"} for url in urls]}
    # Broadcast full message format with event wrapper for uniformity
    if "event" in body:
        # New format: keep the event wrapper
        msg = json.dumps(body, ensure_ascii=False)
    else:
        # Backward compatibility: wrap old format in event
        msg = json.dumps({"event": body}, ensure_ascii=False)
    
    # Check message size (additional safety check)
    msg_bytes = len(msg.encode('utf-8'))
    if msg_bytes > MAX_PAYLOAD_SIZE:
        raise HTTPException(status_code=413, detail="Message too large")
    
    logger.info("Send producer=%s targets=%s payload_size=%d bytes", producer_identity(producer_token).id, targets, msg_bytes)

    delivered = 0
    buffered = 0
    for device_name in targets:
        client = CONNECTED_BY_DEVICE.get(device_name)
        if client:
            ok = await safe_send(client, msg)
            if ok:
                delivered += 1
            else:
                client.connected = False
                CLIENTS.pop(client.id, None)
                # Only remove from CONNECTED_BY_DEVICE if this is still the current client
                if CONNECTED_BY_DEVICE.get(device_name) is client:
                    CONNECTED_BY_DEVICE.pop(device_name, None)
                buf = _ensure_offline_buffer(device_name)
                # Check buffer size before adding
                if _get_buffer_size_bytes(buf) + msg_bytes <= MAX_OFFLINE_BYTES:
                    buf.offline_buffer.append(msg)
                    buffered += 1
                else:
                    logger.warning("Device %s: buffer full, dropping message", device_name)
                logger.warning("Device %s: send failed, moved to offline", device_name)
        else:
            buf = _ensure_offline_buffer(device_name)
            if _get_buffer_size_bytes(buf) + msg_bytes <= MAX_OFFLINE_BYTES:
                buf.offline_buffer.append(msg)
                buffered += 1
                logger.info("Device %s offline, message buffered (%d total, %d bytes)",
                            device_name, len(buf.offline_buffer), _get_buffer_size_bytes(buf))
            else:
                logger.warning("Device %s: buffer full, dropping message", device_name)

    return {"status": "ok", "delivered": delivered, "buffered": buffered}


# ---------------- E-paper image conversion ----------------

EPAPER_W = 250
EPAPER_H = 122
EPAPER_BYTES = 4000   # ceil(250/8) * 122 = 32 * 122
EPAPER_ROW_BYTES = 32  # ceil(250/8)
IMAGE_MAX_BYTES = 1 * 1024 * 1024  # 1 MB


def convert_to_epaper_binary(image_bytes: bytes) -> bytes:
    """Convert any image to the 4000-byte 1-bit e-paper format.

    Layout: row-major, MSB first, 32 bytes per row * 122 rows.
    White pixel = 1, black pixel = 0.
    Unused bits in the last byte of each row are padded with 1 (white).
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert("L")                      # grayscale
        img = img.resize((EPAPER_W, EPAPER_H), Image.LANCZOS)
        img = img.convert("1", dither=Image.Dither.FLOYDSTEINBERG)

        buf = bytearray(EPAPER_BYTES)
        pixels = img.load()
        for row in range(EPAPER_H):
            for col in range(EPAPER_W):
                # PIL mode "1": 255 = white, 0 = black
                bit = 1 if pixels[col, row] else 0
                byte_index = row * EPAPER_ROW_BYTES + col // 8
                bit_pos = 7 - (col % 8)              # MSB first
                buf[byte_index] |= (bit << bit_pos)
        return bytes(buf)
    except ImageDecompressionBombError as e:
        raise HTTPException(status_code=422, detail=f"Image decompression bomb detected: {e}")


# ---------------- Image broadcast endpoint ----------------

@app.post("/api/send-image-2-13inch-touch-e-paper-hat-with-case/")
async def send_epaper_image(
    request: Request,
    targets: str = Form(...),
    image: UploadFile = File(...),
    authorization: str = Header(None),
):
    producer_token = (authorization or "").removeprefix("Bearer ")
    
    # Validate token first
    target_list = [t.strip() for t in targets.split(",") if t.strip()]
    resolved = _resolve_targets(producer_token, target_list or ["*"])
    
    # Rate limiting (after token validation)
    _check_rate_limit(producer_token)

    # Read image with size limit (UploadFile doesn't have stream(), use read())
    raw = await image.read()
    if len(raw) > IMAGE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds 1 MB limit")

    try:
        payload = convert_to_epaper_binary(raw)
    except ImageDecompressionBombError as e:
        raise HTTPException(status_code=422, detail=f"Image decompression bomb detected: {e}")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Image conversion failed: {e}")

    if len(payload) != EPAPER_BYTES:
        raise HTTPException(status_code=500, detail="Conversion produced unexpected byte count")

    logger.info("E-paper producer=%s targets=%s size=%d bytes", producer_identity(producer_token).id, resolved, len(payload))

    delivered = 0
    for device_name in resolved:
        client = CONNECTED_BY_DEVICE.get(device_name)
        if client:
            ok = await safe_send_bytes(client, payload)
            if ok:
                delivered += 1
            else:
                client.connected = False
                CLIENTS.pop(client.id, None)
                # Only remove from CONNECTED_BY_DEVICE if this is still the current client
                if CONNECTED_BY_DEVICE.get(device_name) is client:
                    CONNECTED_BY_DEVICE.pop(device_name, None)
                logger.warning("Device %s: binary send failed, moved to offline", device_name)
        else:
            logger.info("Device %s offline, image not buffered (binary frames are not buffered)", device_name)

    return {"status": "ok", "delivered": delivered, "size_bytes": len(payload)}


# ---------------- Media HTTP protocol and bounded ingress ----------------

def media_store():
    if MEDIA is None:
        fail(503, "Media is disabled: configure media signing key and storage")
    return MEDIA


def bearer(value):
    if not value or not value.startswith("Bearer ") or not value[7:]:
        fail(401, "Bearer credential required")
    return value[7:]


def device_identity(authorization):
    token = bearer(authorization)
    device = IDENTITIES.device_for_token(token)
    if device is None:
        fail(401, "Invalid Device Token")
    return device.id


def validate_external_url(url):
    import ipaddress
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password or
                parsed.fragment or parsed.port not in (None, 443) or
                parsed.path.startswith("/api/media") or
                host.lower() in ("localhost", "localhost.localdomain") or
                host.endswith((".local", ".localhost", ".internal")) or "." not in host):
            fail(422, "External media requires a public HTTPS URL")
        try:
            if not ipaddress.ip_address(host).is_global:
                fail(422, "External media address is not public")
        except ValueError:
            pass
        if MEDIA and parsed.netloc == urlsplit(MEDIA.base).netloc:
            fail(422, "Mid URLs cannot use NONE authentication")
    except (TypeError, ValueError):
        fail(422, "Invalid media URL")


@app.post("/api/media", status_code=201)
async def upload_media(request: Request):
    token = bearer(request.headers.get("authorization"))
    _resolve_targets(token, ["*"])
    store = media_store()
    result = await store.upload(request, producer_identity(token).id, lambda targets: _resolve_targets(token, targets))
    logger.info("Media uploaded producer=%s media=%s size=%d", producer_identity(token).id, result["media_id"], result["size_bytes"])
    return result


@app.post("/api/media/{media_id}/ticket")
async def refresh_media_ticket(media_id: str, request: Request):
    device = device_identity(request.headers.get("authorization"))
    store = media_store()
    with store.lock:
        return store.descriptor(store.get(media_id, device=device), device)


@app.post("/api/media/{media_id}/cached")
async def media_cached(media_id: str, request: Request, body: dict):
    device = device_identity(request.headers.get("authorization"))
    return media_store().acknowledge(media_id, body.get("delivery_id"), body.get("sha256"), device)


@app.get("/api/media/{media_id}")
async def download_media(media_id: str, request: Request):
    device = device_identity(request.headers.get("authorization"))
    store = media_store()
    meta, file = store.open_download(media_id, device, request.query_params.get("ticket", ""))
    size = meta["size_bytes"]
    start, end, status = 0, size - 1, 200
    # Single byte ranges support interrupted MP4 downloads. Multiple ranges are rejected.
    try:
        value = request.headers.get("range")
        if value and request.headers.get("if-range", '"' + meta["sha256"] + '"') == '"' + meta["sha256"] + '"':
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
            if not match or not any(match.groups()):
                fail(416, "Invalid byte range")
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), end) if last else end
            else:
                suffix = int(last)
                if suffix == 0:
                    fail(416, "Invalid suffix range")
                start = max(0, size - suffix)
            if start >= size or start > end:
                fail(416, "Unsatisfiable byte range")
            status = 206
    except (HTTPException, ValueError):
        store.release_download(media_id, file)
        return JSONResponse({"detail": "Unsatisfiable byte range"}, status_code=416, headers={"Content-Range": f"bytes */{size}"})
    async def chunks():
        try:
            file.seek(start)
            remaining = end - start + 1
            while remaining:
                data = file.read(min(256 * 1024, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data
        finally:
            store.release_download(media_id, file)
    headers = {"Content-Length": str(end - start + 1), "Accept-Ranges": "bytes", "ETag": '"' + meta["sha256"] + '"',
               "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(chunks(), status_code=status, media_type=meta["mime_type"], headers=headers)


class BoundedIngress:
    """Authenticate before any body read; limit actual ASGI bytes, even without Content-Length."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        path, method = scope["path"], scope["method"]
        try:
            auth = headers.get(b"authorization", b"").decode("latin1")
            upload = path == "/api/media" and method == "POST"
            epaper = path.startswith("/api/send-image-")
            if upload or epaper or path == "/api/send":
                _resolve_targets(bearer(auth), ["*"])
            elif path.startswith("/api/media/"):
                device_identity(auth)
            limit = media_store().cfg["max_upload_bytes"] + 16384 if upload else (IMAGE_MAX_BYTES + 16384 if epaper else MAX_PAYLOAD_SIZE)
            length = headers.get(b"content-length")
            try:
                declared = int(length) if length is not None else 0
            except ValueError:
                fail(400, "Invalid Content-Length")
            if declared < 0 or declared > limit:
                fail(413, "Payload too large")
            received = 0
            async def bounded_receive():
                nonlocal received
                message = await receive()
                received += len(message.get("body", b""))
                if received > limit:
                    fail(413, "Payload too large")
                return message
            await self.app(scope, bounded_receive, send)
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)


app.add_middleware(BoundedIngress)
