import asyncio
import io
import os
import time
import json
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, UploadFile, File, Form, Request, Response
from collections import deque
from PIL import Image
try:
    from PIL import ImageDecompressionBombError
except ImportError:
    # Fallback for older Pillow versions
    ImageDecompressionBombError = Exception
from typing import Dict
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

# Configure Pillow limits once at startup to prevent decompression bombs
# Allow up to 8MP (roughly 4K resolution) which is reasonable for most images
Image.MAX_IMAGE_PIXELS = 8000 * 8000

# ---------------- Middleware ----------------

@app.middleware("http")
async def payload_size_middleware(request: Request, call_next):
    """Validate Content-Length before FastAPI parses the body."""
    if request.url.path.startswith("/api/"):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                size = int(content_length)
                if size > MAX_PAYLOAD_SIZE:
                    return Response(
                        content='{"detail": "Payload too large"}',
                        status_code=413,
                        media_type="application/json"
                    )
            except ValueError:
                pass  # Invalid Content-Length header, let FastAPI handle it
    
    response = await call_next(request)
    return response

# ---------------- Config ----------------

_CONFIG_PATH = os.environ.get(
    "PINEPI_BROADCAST_CONFIG",
    "/etc/pinepi-broadcast-mid/config.json"
)

def _load_config() -> dict:
    path = os.path.abspath(_CONFIG_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"config.json not found at {path}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"config.json is invalid JSON: {e}")

_config = _load_config()

# producers: {token: {"targets": "*" | ["device", ...]}}
PRODUCER_TOKENS: dict[str, dict] = _config.get("producers", {})
# devices: {device_name: {"token": "..."}}
DEVICE_TOKENS: dict[str, str] = {
    name: cfg["token"]
    for name, cfg in _config.get("devices", {}).items()
}

if not PRODUCER_TOKENS:
    raise RuntimeError("config.json: no producers defined")
if not DEVICE_TOKENS:
    raise RuntimeError("config.json: no devices defined")

logger.info("Loaded %d producer token(s), %d device(s): %s",
            len(PRODUCER_TOKENS), len(DEVICE_TOKENS), list(DEVICE_TOKENS.keys()))

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

# Rate limiting per producer token
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


def _resolve_targets(producer_token: str, requested: list[str]) -> list[str]:
    """Return the list of device names to deliver to, enforcing producer ACL.

    Raises HTTPException 401 if the token is unknown, 403 if a requested
    device is outside the producer's allowed targets.
    """
    cfg = PRODUCER_TOKENS.get(producer_token)
    if cfg is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    allowed = cfg.get("targets", [])
    all_devices = list(DEVICE_TOKENS.keys())
    if allowed == "*" or allowed == ["*"]:
        allowed_set = set(all_devices)
    else:
        allowed_set = set(allowed)
    if "*" in requested:
        return [d for d in all_devices if d in allowed_set]
    for d in requested:
        if d not in allowed_set:
            raise HTTPException(status_code=403, detail=f"Producer not allowed to target device '{d}'")
    return requested


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

    expected_token = DEVICE_TOKENS.get(device_name)
    if not expected_token or token != expected_token:
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
    """Check and update rate limits for a producer token."""
    now = time.monotonic()
    entry = PRODUCER_RATE_LIMITS.get(producer_token)
    if entry is None or now - entry.window_start > RATE_LIMIT_WINDOW:
        PRODUCER_RATE_LIMITS[producer_token] = RateLimitEntry(count=1, window_start=now)
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
    
    logger.info("Send to %s: payload_size=%d bytes", targets, msg_bytes)

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

    logger.info("E-paper image send: targets=%s size=%d bytes", resolved, len(payload))

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
