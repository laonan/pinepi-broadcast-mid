"""Single-process, filesystem-backed private media relay (no database)."""
import asyncio
import base64
import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import jwt
from fastapi import HTTPException
from PIL import Image
from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header

DEFAULTS = {
    "data_dir": "/var/lib/pinepi-broadcast-mid/media",
    "public_base_url": "https://mid.example.com",
    "max_image_pixels": 16000000,
    "max_upload_bytes": 100 * 1024 * 1024,
    "producer_capacity_bytes": 500 * 1024 * 1024,
    "total_capacity_bytes": 5 * 1024 * 1024 * 1024,
    "uploads_per_minute": 10,
    "retention_seconds": 86400,
    "ticket_seconds": 300,
    "grace_seconds": 60,
    "cleanup_interval_seconds": 30,
    "upload_timeout_seconds": 120,
    "temporary_retention_seconds": 600,
    "tombstone_seconds": 604800,
    "max_assets": 10000,
    "max_deliveries_per_asset": 200,
}
ID = re.compile(r"^[0-9a-f]{32}$")
ISSUER = "pinepi-broadcast-mid"
AUDIENCE = "pinepi-hub-media"


def fail(code, detail):
    raise HTTPException(code, detail)


def atomic_json(path, value):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(value, f, separators=(",", ":"), ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class MediaStore:
    def __init__(self, config):
        self.cfg = {**DEFAULTS, **config}
        for name, default in DEFAULTS.items():
            if isinstance(default, int) and (type(self.cfg[name]) is not int or self.cfg[name] <= 0):
                raise RuntimeError(f"media.{name} must be a positive integer")
        base = urlsplit(self.cfg["public_base_url"])
        if base.scheme != "https" or not base.netloc or base.username or base.query or base.fragment or base.path not in ("", "/"):
            raise RuntimeError("media.public_base_url must be an HTTPS origin")
        self.base = self.cfg["public_base_url"].rstrip("/")
        try:
            self.key = base64.b64decode(config["signing_key_base64"], validate=True)
        except (KeyError, ValueError) as exc:
            raise RuntimeError("media.signing_key_base64: provide an independent random base64 key") from exc
        if len(self.key) < 32 or len(set(self.key)) < 16:
            raise RuntimeError("media signing key must contain at least 32 random bytes")
        self.root = Path(self.cfg["data_dir"])
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lockfile = (self.root / ".lock").open("a+")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lockfile.close()
            raise RuntimeError("media directory already in use: run exactly one worker") from exc
        self.lock = threading.RLock()
        self.upload_lock = asyncio.Lock()
        self.assets = {}
        self.active_temps = set()
        self.downloads = {}
        self.rates = {}
        rate_path = self.root / "rates.json"
        if rate_path.exists():
            self.rates = json.loads(rate_path.read_text())
        # Fail closed on corrupt metadata. Never silently discard pending deliveries.
        for directory in self.root.iterdir():
            if directory.is_dir() and ID.fullmatch(directory.name):
                path = directory / "meta.json"
                if path.exists():
                    meta = json.loads(path.read_text())
                    if meta["media_id"] != directory.name:
                        raise RuntimeError("media metadata ID mismatch")
                    self.assets[directory.name] = meta
        self.cleanup()

    def close(self):
        self.lockfile.close()

    @staticmethod
    def owner(producer_id):
        # Ownership and persisted upload quotas are derived only from stable identity.
        return hashlib.sha256(producer_id.encode()).hexdigest()

    def disk_bytes(self):
        # Include metadata, tombstones and orphaned files in the global quota.
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def save(self, meta):
        encoded = json.dumps(meta, separators=(",", ":"), ensure_ascii=False).encode()
        old_path = self.root / meta["media_id"] / "meta.json"
        old_size = old_path.stat().st_size if old_path.exists() else 0
        if meta["state"] == "active" and self.disk_bytes() - old_size + len(encoded) > self.cfg["total_capacity_bytes"]:
            fail(507, "Service metadata capacity exceeded")
        atomic_json(self.root / meta["media_id"] / "meta.json", meta)

    def rate_limit(self, owner):
        with self.lock:
            now = time.time()
            self.rates = {k: v for k, v in self.rates.items() if now - v[0] < 60}
            entry = self.rates.setdefault(owner, [now, 0])
            if entry[1] >= self.cfg["uploads_per_minute"]:
                fail(429, "Producer upload rate exceeded")
            entry[1] += 1
            atomic_json(self.root / "rates.json", self.rates)

    def usage(self, owner):
        live = [m for m in self.assets.values() if (self.root / m["media_id"] / "content").exists()]
        return sum(m["size_bytes"] for m in live), sum(m["size_bytes"] for m in live if m["owner"] == owner)

    def capacity(self, owner, incoming):
        _, owned = self.usage(owner)
        if owned + incoming > self.cfg["producer_capacity_bytes"]:
            fail(429, "Producer media capacity exceeded")
        temporary_bytes = sum((self.root / name).stat().st_size for name in self.active_temps if (self.root / name).exists())
        if self.disk_bytes() - temporary_bytes + incoming > self.cfg["total_capacity_bytes"]:
            fail(507, "Service media capacity exceeded")
        if shutil.disk_usage(self.root).free < incoming + 16 * 1024 * 1024:
            fail(507, "Insufficient disk space")

    def get(self, media_id, *, owner=None, device=None, live=True):
        if not isinstance(media_id, str):
            fail(404, "Invalid media ID")
        meta = self.assets.get(media_id)
        if meta is None or (owner is not None and meta["owner"] != owner) or (device is not None and device not in meta["targets"]):
            fail(404, "Media not found or inaccessible")
        if live and (meta["state"] != "active" or time.time() >= meta["expires_at"]):
            fail(410, "Media expired" if time.time() >= meta["expires_at"] or meta["state"] == "expired" else "Media deleted")
        return meta

    def descriptor(self, meta, device):
        now = int(time.time())
        expiry = min(now + self.cfg["ticket_seconds"], int(meta["expires_at"]))
        ticket = jwt.encode({"sub": meta["media_id"], "device": device, "iat": now, "exp": expiry,
                             "purpose": "media.download", "iss": ISSUER, "aud": AUDIENCE}, self.key, algorithm="HS256")
        return {k: meta[k] for k in ("media_id", "mime_type", "size_bytes", "sha256", "expires_at")} | {
            "url": f'{self.base}/api/media/{meta["media_id"]}?ticket={ticket}',
            "auth_mode": "DEVICE", "ticket_expires_at": expiry}

    def verify_ticket(self, media_id, device, ticket):
        if not isinstance(ticket, str) or len(ticket) > 4096:
            fail(403, "Invalid media ticket")
        try:
            claims = jwt.decode(ticket, self.key, algorithms=["HS256"], issuer=ISSUER, audience=AUDIENCE,
                                options={"require": ["sub", "device", "exp", "iat", "purpose", "iss", "aud"]})
        except jwt.PyJWTError:
            fail(403, "Invalid or expired media ticket")
        if claims["sub"] != media_id or claims["device"] != device or claims["purpose"] != "media.download":
            fail(403, "Media ticket binding mismatch")

    def publish(self, ids, producer_id, targets, event):
        owner = self.owner(producer_id)
        with self.lock:
            metas = [copy.deepcopy(self.get(i, owner=owner)) for i in ids]
            if len(ids) != len(set(ids)) or not ids:
                fail(422, "media_ids must be nonempty and unique")
            for meta in metas:
                if not set(targets) <= set(meta["targets"]):
                    fail(403, "Publish targets exceed asset targets")
                if len(meta["deliveries"]) >= self.cfg["max_deliveries_per_asset"]:
                    fail(429, "Asset delivery limit exceeded")
            # One asset per durable delivery: every meta.json is its own atomic transaction.
            # Each carries the full original event; Hub deduplicates by publication_id.
            publication = secrets.token_hex(16)
            results = []
            for meta in metas:
                delivery = {"delivery_id": secrets.token_hex(16), "publication_id": publication,
                            "event": event, "created_at": time.time(),
                            "targets": {d: "pending" for d in targets}}
                meta["deliveries"].append(delivery)
                meta["completed_at"] = None
                self.save(meta)
                self.assets[meta["media_id"]] = meta
                results.append((meta, delivery))
            return results

    def envelope(self, meta, delivery, device):
        return {"type": "media.delivery", "delivery_id": delivery["delivery_id"],
                "publication_id": delivery["publication_id"], "event": delivery["event"],
                "downloads": [self.descriptor(meta, device)]}

    def pending(self, device):
        with self.lock:
            result = []
            for meta in self.assets.values():
                if meta["state"] != "active" or time.time() >= meta["expires_at"] or device not in meta["targets"]:
                    continue
                for delivery in meta["deliveries"]:
                    if delivery["targets"].get(device) == "pending":
                        result.append(self.envelope(meta, delivery, device))
            return result

    def acknowledge(self, media_id, delivery_id, digest, device):
        with self.lock:
            meta = copy.deepcopy(self.get(media_id, device=device, live=False))
            delivery = next((d for d in meta["deliveries"] if d["delivery_id"] == delivery_id), None)
            if delivery is None or device not in delivery["targets"]:
                fail(404, "Delivery not found")
            if digest != meta["sha256"]:
                fail(409, "SHA-256 mismatch")
            if delivery["targets"][device] == "cached":
                return {"status": "cached", "media_id": media_id, "delivery_id": delivery_id}
            self.get(media_id, device=device)
            delivery["targets"][device] = "cached"
            if all(s == "cached" for d in meta["deliveries"] for s in d["targets"].values()):
                meta["completed_at"] = time.time()
            self.save(meta)
            self.assets[media_id] = meta
            return {"status": "cached", "media_id": media_id, "delivery_id": delivery_id}

    def open_download(self, media_id, device, ticket):
        with self.lock:
            self.verify_ticket(media_id, device, ticket)
            meta = self.get(media_id, device=device)
            # Open before releasing lock: unlink never invalidates this POSIX file descriptor.
            try:
                file = (self.root / media_id / "content").open("rb")
                self.downloads[media_id] = self.downloads.get(media_id, 0) + 1
                return meta.copy(), file
            except FileNotFoundError:
                fail(410, "Media content unavailable")

    def release_download(self, media_id, file):
        with self.lock:
            file.close()
            self.downloads[media_id] -= 1
            if not self.downloads[media_id]:
                del self.downloads[media_id]
            self.cleanup()

    def cleanup(self):
        with self.lock:
            now = time.time()
            for media_id, meta in list(self.assets.items()):
                expired = now >= meta["expires_at"]
                completed = meta.get("completed_at")
                if meta["state"] == "active" and (expired or (completed and now >= completed + self.cfg["grace_seconds"])):
                    meta["state"] = "expired" if expired else "deleted"
                    meta["deleted_at"] = now
                    for delivery in meta["deliveries"]:
                        for device, state in delivery["targets"].items():
                            if state == "pending":
                                delivery["targets"][device] = "expired"
                    self.save(meta)
                if meta["state"] != "active" and not self.downloads.get(media_id):
                    (self.root / media_id / "content").unlink(missing_ok=True)
                    if now >= meta["deleted_at"] + self.cfg["tombstone_seconds"]:
                        shutil.rmtree(self.root / media_id)
                        del self.assets[media_id]
            for path in self.root.rglob("*.tmp"):
                if now - path.stat().st_mtime >= self.cfg["temporary_retention_seconds"]:
                    path.unlink()
            for path in self.root.iterdir():
                if path.name in self.active_temps or path.name.startswith(".") and not path.name.startswith(".upload-"):
                    continue
                orphan = path.is_dir() and path.name not in self.assets
                if (path.name.startswith(".upload-") or orphan) and now - path.stat().st_mtime >= self.cfg["temporary_retention_seconds"]:
                    shutil.rmtree(path) if path.is_dir() else path.unlink()

    async def upload(self, request, producer_id, resolve_targets):
        owner = self.owner(producer_id)
        self.rate_limit(owner)
        if self.upload_lock.locked():
            fail(429, "Another upload is active; retry later")
        async with self.upload_lock:
            temp = self.root / (".upload-" + secrets.token_hex(16))
            self.active_temps.add(temp.name)
            parser = None
            directory = None
            try:
                async with asyncio.timeout(self.cfg["upload_timeout_seconds"]):
                    parser = StreamingUpload(self, temp, owner, resolve_targets, request.headers.get("content-type", ""))
                    async for chunk in request.stream():
                        # Also bounds multipart overhead and chunked bodies.
                        parser.total += len(chunk)
                        if parser.total > self.cfg["max_upload_bytes"] + 16384:
                            fail(413, "Upload request too large")
                        parser.parser.write(chunk)
                    parser.parser.finalize()
                    if not parser.done or not parser.file_done or not parser.targets or parser.size == 0:
                        fail(422, "Incomplete upload")
                    parser.file.flush()
                    os.fsync(parser.file.fileno())
                    parser.file.close()
                    mime = await asyncio.to_thread(validate_media, temp, self.cfg["max_image_pixels"])
                    with self.lock:
                        self.capacity(owner, parser.size)
                        if len(self.assets) >= self.cfg["max_assets"]:
                            fail(507, "Asset count limit reached")
                        media_id = secrets.token_hex(16)
                        directory = self.root / media_id
                        directory.mkdir(mode=0o700)
                        root_fd = os.open(self.root, os.O_RDONLY)
                        try:
                            os.fsync(root_fd)
                        finally:
                            os.close(root_fd)
                        os.replace(temp, directory / "content")
                        now = time.time()
                        meta = {"media_id": media_id, "owner": owner, "producer_id": producer_id, "targets": parser.targets,
                                "mime_type": mime, "size_bytes": parser.size, "sha256": parser.digest.hexdigest(),
                                "created_at": now, "expires_at": now + self.cfg["retention_seconds"],
                                "state": "active", "completed_at": None, "deliveries": []}
                        self.save(meta)
                        self.assets[media_id] = meta
                        return {k: meta[k] for k in ("media_id", "mime_type", "size_bytes", "sha256", "expires_at")}
            except TimeoutError:
                fail(408, "Upload timeout")
            except MultipartParseError:
                fail(422, "Malformed multipart upload")
            except OSError as exc:
                if exc.errno in (28, 122):
                    fail(507, "Insufficient disk space")
                raise
            finally:
                if parser and parser.file:
                    parser.file.close()
                temp.unlink(missing_ok=True)
                self.active_temps.discard(temp.name)
                if directory is not None and directory.name not in self.assets:
                    shutil.rmtree(directory, ignore_errors=True)


class StreamingUpload:
    """Require a bounded targets field before file; never spool unauthorized bytes."""
    def __init__(self, store, temp, owner, resolve, content_type):
        self.store, self.temp, self.owner, self.resolve = store, temp, owner, resolve
        self.total = self.size = 0
        self.targets = None
        self.file = None
        self.file_done = self.done = False
        self.digest = hashlib.sha256()
        self.parts = 0
        mime, options = parse_options_header(content_type)
        boundary = options.get(b"boundary", b"")
        if mime != b"multipart/form-data" or not boundary or len(boundary) > 200:
            fail(422, "Expected multipart/form-data")
        callbacks = {"on_" + name: getattr(self, name) for name in (
            "part_begin", "header_field", "header_value", "header_end", "headers_finished", "part_data", "part_end", "end")}
        self.parser = MultipartParser(boundary, callbacks)

    def part_begin(self):
        self.parts += 1
        if self.parts > 2:
            fail(422, "Only targets and file are accepted")
        self.headers, self.hfield, self.hvalue, self.value = {}, bytearray(), bytearray(), bytearray()
        self.header_bytes = 0

    def header_field(self, data, start, end):
        self.header_bytes += end - start
        if self.header_bytes > 4096:
            fail(413, "Multipart headers too large")
        self.hfield.extend(data[start:end])

    def header_value(self, data, start, end):
        self.header_bytes += end - start
        if self.header_bytes > 4096:
            fail(413, "Multipart headers too large")
        self.hvalue.extend(data[start:end])

    def header_end(self):
        self.headers[bytes(self.hfield).lower()] = bytes(self.hvalue)
        self.hfield.clear()
        self.hvalue.clear()

    def headers_finished(self):
        _, opts = parse_options_header(self.headers.get(b"content-disposition", b""))
        self.name = opts.get(b"name")
        if self.name == b"targets" and self.parts == 1 and b"filename" not in opts:
            return
        if self.name != b"file" or self.parts != 2 or self.targets is None:
            fail(422, "Send targets before file")
        with self.store.lock:
            self.store.capacity(self.owner, 0)
        self.file = self.temp.open("xb")
        os.chmod(self.temp, 0o600)

    def part_data(self, data, start, end):
        chunk = data[start:end]
        if self.name == b"targets":
            if len(self.value) + len(chunk) > 4096:
                fail(413, "Targets too large")
            self.value.extend(chunk)
            return
        self.size += len(chunk)
        if self.size > self.store.cfg["max_upload_bytes"]:
            fail(413, "File exceeds upload limit")
        with self.store.lock:
            self.store.capacity(self.owner, self.size)
            self.file.write(chunk)
        self.digest.update(chunk)

    def part_end(self):
        if self.name == b"targets":
            try:
                text = self.value.decode()
                requested = json.loads(text) if text.startswith("[") else [s.strip() for s in text.split(",") if s.strip()]
            except (ValueError, UnicodeError):
                fail(422, "Invalid targets")
            self.targets = self.resolve(requested)
            if not self.targets:
                fail(422, "At least one target required")
        else:
            self.file_done = True

    def end(self):
        self.done = True


def validate_media(path, max_image_pixels=16000000):
    with path.open("rb") as f:
        magic = f.read(12)
    if magic.startswith(b"\xff\xd8\xff") or magic.startswith(b"\x89PNG\r\n\x1a\n"):
        try:
            with Image.open(path) as image:
                if image.format not in ("JPEG", "PNG"):
                    fail(415, "Only JPEG and PNG images supported")
                if image.width * image.height > max_image_pixels:
                    fail(415, "Image pixel count exceeds limit")
                fmt = image.format
                image.verify()
            with Image.open(path) as image:
                image.load()
            return "image/jpeg" if fmt == "JPEG" else "image/png"
        except HTTPException:
            raise
        except Exception:
            fail(415, "Invalid image")
    if magic.startswith(b"ID3") or (len(magic) >= 2 and magic[0] == 0xff and magic[1] & 0xe0 == 0xe0):
        result = probe_media(path)
        streams = result.get("streams", [])
        audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
        if result.get("format", {}).get("format_name") != "mp3" or len(audio) != 1 or audio[0].get("codec_name") != "mp3":
            fail(415, "Only MP3 audio is supported")
        for stream in streams:
            if stream.get("codec_type") == "audio":
                continue
            if not (stream.get("codec_type") == "video" and stream.get("codec_name") in ("mjpeg", "png")
                    and stream.get("disposition", {}).get("attached_pic") == 1):
                fail(415, "Unsupported MP3 stream")
        return "audio/mpeg"
    if magic[4:8] == b"ftyp":
        if magic[8:12] not in (b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash", b"M4V ", b"MSNV"):
            fail(415, "Unsupported MP4 container brand")
        result = probe_media(path)
        streams = result.get("streams", [])
        if "mp4" not in result.get("format", {}).get("format_name", "").split(","):
            fail(415, "Expected MP4 container")
        if not any(stream.get("codec_type") == "video" for stream in streams):
            fail(415, "MP4 requires a video stream")
        if any((stream.get("codec_type"), stream.get("codec_name")) not in (("video", "h264"), ("audio", "aac")) for stream in streams):
            fail(415, "MP4 supports H.264 video and optional AAC audio only")
        return "video/mp4"
    fail(415, "Only JPEG, PNG, MP3 and H.264/AAC MP4 supported")


def probe_media(path):
    try:
        probe = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-show_entries",
                                "format=format_name:stream=codec_type,codec_name:stream_disposition=attached_pic", "-of", "json", str(path)],
                               capture_output=True, timeout=15, check=True)
        if probe.stderr.strip():
            fail(415, "Invalid media stream")
        return json.loads(probe.stdout)
    except FileNotFoundError:
        fail(503, "Audio/video validation unavailable: install ffprobe")
    except (subprocess.SubprocessError, ValueError):
        fail(415, "Invalid or unsupported audio/video")
