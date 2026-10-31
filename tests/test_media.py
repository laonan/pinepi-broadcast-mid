import base64
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

import jwt
import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
# Import-time legacy config, intentionally separate Producer and Device credentials.
config_path = Path('/tmp') / ('pinepi-test-' + secrets.token_hex(8) + '.json')
config_path.write_text(json.dumps({'producers': {'web-console': {'description': 'Web console', 'token': 'producer', 'targets': '*'}, 'limited-sender': {'token': 'limited', 'targets': ['a']}, 'other-sender': {'token': 'other', 'targets': '*'}},
                                   'devices': {'a': {'description': 'First Hub', 'token': 'device-a'}, 'b': {'description': 'Second Hub', 'token': 'device-b'}}}))
os.environ['PINEPI_BROADCAST_CONFIG'] = str(config_path)
import broadcast_server_mid as server
config_path.unlink()
from media_store import MediaStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setitem(server._config, 'media', {'data_dir': str(tmp_path / 'media'),
        'signing_key_base64': base64.b64encode(secrets.token_bytes(32)).decode(),
        'public_base_url': 'https://mid.example.com', 'grace_seconds': 1})
    server.CLIENTS.clear()
    server.CONNECTED_BY_DEVICE.clear()
    server.OFFLINE_CLIENTS.clear()
    server.PRODUCER_RATE_LIMITS.clear()
    with TestClient(server.app) as c:
        yield c


def auth(token='producer'):
    return {'Authorization': 'Bearer ' + token}


def png():
    out = io.BytesIO()
    Image.new('RGB', (10, 10), 'blue').save(out, format='PNG')
    return out.getvalue()


def upload(c, targets='a,b', token='producer', data=None):
    return c.post('/api/media', headers=auth(token), data={'targets': targets}, files={'file': ('../../evil.mp4', data or png(), 'text/plain')})


def publish(c, media, targets=None, token='producer'):
    return c.post('/api/send', headers=auth(token), json={'targets': targets or ['a', 'b'], 'media_id': media['media_id'],
        'event': {'v': 2, 'media': {'url': 'mid://' + media['media_id']}}})


def ticket(c, media, device='a'):
    return c.post('/api/media/' + media['media_id'] + '/ticket', headers=auth('device-' + device))


def ack(c, media, delivery, device='a', digest=None):
    return c.post('/api/media/' + media['media_id'] + '/cached', headers=auth('device-' + device),
        json={'delivery_id': delivery, 'sha256': digest or media['sha256'], 'device': 'spoof'})


def test_auth_acl_format_and_filename(client):
    root = server.MEDIA.root
    assert upload(client, token='invalid').status_code == 401
    assert upload(client, token='device-a').status_code == 401
    assert upload(client, token='limited').status_code == 403
    assert not list(root.glob('.upload-*')) and not server.MEDIA.assets
    assert upload(client, data=b'fake PNG').status_code == 415
    response = upload(client)
    assert response.status_code == 201, response.text
    m = response.json()
    assert m['mime_type'] == 'image/png' and m['sha256'] == hashlib.sha256(png()).hexdigest()
    assert m['size_bytes'] == len(png())
    assert (root / m['media_id'] / 'content').read_bytes() == png()
    text = (root / m['media_id'] / 'meta.json').read_text()
    assert 'Bearer' not in text and 'producer"' not in text and 'evil' not in text
    assert publish(client, m, token='other').status_code == 404
    assert publish(client, m, token='limited').status_code == 403
    a_only = upload(client, targets='a').json()
    assert publish(client, a_only).status_code == 403
    assert client.post('/api/send', headers=auth(), json={'targets': []}).status_code == 422


def test_requires_targets_before_file(client):
    response = client.post('/api/media', headers=auth(), files=[('file', ('photo.png', png())), ('targets', (None, 'a'))])
    assert response.status_code == 422
    assert not list(server.MEDIA.root.glob('.upload-*'))


def test_download_ticket_binding_ranges_and_expiry(client):
    m = upload(client).json()
    d = ticket(client, m).json()
    url = d['url']
    assert d['auth_mode'] == 'DEVICE' and d['ticket_expires_at'] <= m['expires_at']
    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth('device-b')).status_code == 403
    assert client.get('/api/media/' + m['media_id'], headers=auth('device-a')).status_code == 403
    assert client.get(url + 'x', headers=auth('device-a')).status_code == 403
    for _ in range(2):
        assert client.get(url, headers=auth('device-a')).content == png()
    for range_value, content in [('bytes=0-9', png()[:10]), ('bytes=10-', png()[10:]), ('bytes=-5', png()[-5:])]:
        response = client.get(url, headers={**auth('device-a'), 'Range': range_value})
        assert response.status_code == 206 and response.content == content
    for r in ['bytes=9999-', 'bytes=10-1', 'bytes=-0', 'bytes=0-1,4-5', 'nonsense']:
        assert client.get(url, headers={**auth('device-a'), 'Range': r}).status_code == 416
    signed = url.split('ticket=')[1]
    claims = jwt.decode(signed, server.MEDIA.key, algorithms=['HS256'], audience='pinepi-hub-media')
    for field, value in [('exp', 1), ('purpose', 'other'), ('aud', 'other'), ('iss', 'other'), ('sub', 'wrong')]:
        forged = jwt.encode({**claims, field: value}, server.MEDIA.key, algorithm='HS256')
        assert client.get('/api/media/' + m['media_id'] + '?ticket=' + forged, headers=auth('device-a')).status_code == 403
    wrong_alg = jwt.encode(claims, server.MEDIA.key, algorithm='HS384')
    assert client.get('/api/media/' + m['media_id'] + '?ticket=' + wrong_alg, headers=auth('device-a')).status_code == 403
    private = upload(client, targets='a').json()
    assert ticket(client, private, 'b').status_code == 404
    server.MEDIA.assets[m['media_id']]['expires_at'] = time.time() - 1
    assert ticket(client, m).status_code == 410
    assert client.get(url, headers=auth('device-a')).status_code == 410


def test_multiple_publications_devices_idempotency_and_cleanup(client):
    m = upload(client).json()
    first = publish(client, m).json()['delivery_ids'][0]
    second = publish(client, m, ['a']).json()['delivery_ids'][0]
    url = ticket(client, m).json()['url']
    assert ack(client, m, first, digest='wrong').status_code == 409
    assert ack(client, m, 'unknown').status_code == 404
    assert ack(client, m, first).status_code == 200
    assert ack(client, m, first).status_code == 200
    assert ack(client, m, second).status_code == 200
    server.MEDIA.cleanup()
    assert client.get(url, headers=auth('device-a')).status_code == 200
    assert len(server.MEDIA.pending('b')) == 1
    assert ack(client, m, first, 'b').status_code == 200
    meta = server.MEDIA.assets[m['media_id']]
    meta['completed_at'] -= 2
    server.MEDIA.cleanup()
    assert client.get(url, headers=auth('device-a')).status_code == 410
    assert not (server.MEDIA.root / m['media_id'] / 'content').exists()
    assert ack(client, m, first).status_code == 200
    assert meta['state'] == 'deleted'


def test_recovery_offline_reconnect_and_websocket_ack(client):
    m = upload(client).json()
    delivery = publish(client, m).json()['delivery_ids'][0]
    assert ack(client, m, delivery).status_code == 200
    store = server.MEDIA
    cfg = dict(store.cfg)
    store.close()
    server.MEDIA = MediaStore(cfg)
    assert not server.MEDIA.pending('a')
    assert len(server.MEDIA.pending('b')) == 1
    with client.websocket_connect('/ws/') as ws:
        ws.send_json({'device': 'b', 'token': 'device-b'})
        envelope = ws.receive_json()
        assert envelope['type'] == 'media.delivery' and envelope['delivery_id'] == delivery
        assert envelope['event']['media'].keys() == {'url'}
        desc = envelope['downloads'][0]
        assert desc['auth_mode'] == 'DEVICE'
        assert client.get(desc['url'], headers=auth('device-b')).content == png()
        ws.send_json({'type': 'media.cached', 'media_id': m['media_id'], 'delivery_id': delivery, 'sha256': m['sha256'], 'device': 'a'})
        assert ws.receive_json()['status'] == 'cached'
    assert not server.MEDIA.pending('b')


def test_expiry_abandoned_temp_and_active_download(client):
    m = upload(client).json()
    delivery = publish(client, m).json()['delivery_ids'][0]
    meta = server.MEDIA.assets[m['media_id']]
    desc = ticket(client, m).json()
    _, file = server.MEDIA.open_download(m['media_id'], 'a', desc['url'].split('ticket=')[1])
    meta['expires_at'] = time.time() - 1
    temp = server.MEDIA.root / '.upload-orphan'
    temp.write_bytes(b'partial')
    os.utime(temp, (0, 0))
    server.MEDIA.cleanup()
    assert meta['state'] == 'expired' and meta['deliveries'][0]['targets'] == {'a': 'expired', 'b': 'expired'}
    assert ack(client, m, delivery).status_code == 410
    assert not temp.exists()
    assert (server.MEDIA.root / m['media_id'] / 'content').exists()
    assert file.read() == png()
    server.MEDIA.release_download(m['media_id'], file)
    assert not (server.MEDIA.root / m['media_id'] / 'content').exists()
    assert server.MEDIA.pending('b') == []


def test_quotas_and_actual_read_size(client):
    store = server.MEDIA
    store.cfg['max_upload_bytes'] = 10
    assert upload(client).status_code == 413
    assert not list(store.root.glob('.upload-*'))
    store.cfg['max_upload_bytes'] = 100000
    store.cfg['producer_capacity_bytes'] = len(png()) - 1
    assert upload(client).status_code == 429
    store.cfg['producer_capacity_bytes'] = 100000
    store.cfg['total_capacity_bytes'] = len(png()) - 1
    assert upload(client).status_code == 507
    store.cfg['total_capacity_bytes'] = 100000
    store.cfg['uploads_per_minute'] = 1
    assert upload(client).status_code == 429
    assert client.post('/api/send', headers=auth(), content=b'x' * 70000).status_code == 413
    async def raw_test():
        count = 0
        responses = []
        async def receive():
            nonlocal count
            count += 1
            return {'type': 'http.request', 'body': b'not read', 'more_body': False}
        async def send(msg):
            responses.append(msg)
        await server.app({'type': 'http', 'path': '/api/media', 'method': 'POST', 'headers': []}, receive, send)
        assert count == 0 and responses[0]['status'] == 401
    import asyncio
    asyncio.run(raw_test())


def test_none_descriptors_legacy_and_epaper(client):
    body = {'targets': ['a'], 'event': {'hello': 'world'}, 'media_urls': ['https://cdn.example.org/file.mp4?token=external']}
    with client.websocket_connect('/ws/') as ws:
        ws.send_json({'device': 'a', 'token': 'device-a'})
        assert client.post('/api/send', headers=auth(), json=body).status_code == 200
        data = ws.receive_json()
        assert data['downloads'] == [{'url': body['media_urls'][0], 'auth_mode': 'NONE'}]
        assert 'device-a' not in json.dumps(data)
        assert client.post('/api/send', headers=auth(), json={'targets': ['a'], 'event': {'text': 'old'}}).status_code == 200
        assert ws.receive_json()['event'] == {'text': 'old'}
        response = client.post('/api/send-image-2-13inch-touch-e-paper-hat-with-case/', headers=auth(), data={'targets': 'a'}, files={'image': ('x.png', png())})
        assert response.status_code == 200 and len(ws.receive_bytes()) == 4000
    for url in ['https://mid.example.com/api/media/id', 'http://cdn.example.com/a', 'https://127.0.0.1/a', 'https://foo.local/a', 'https://cdn.example.com/api/media/id']:
        body['media_urls'] = [url]
        assert client.post('/api/send', headers=auth(), json=body).status_code == 422
    assert client.post('/api/send', headers=auth(), json={'downloads': [{'auth_mode': 'NONE'}]}).status_code == 422


def test_mp4_validation(client, tmp_path):
    path = tmp_path / 'test.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=32x32:d=0.2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)], check=True)
    response = upload(client, data=path.read_bytes())
    assert response.status_code == 201 and response.json()['mime_type'] == 'video/mp4'
    bad = tmp_path / 'unsupported.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=32x32:d=0.2', '-c:v', 'mpeg4', str(bad)], check=True)
    assert upload(client, data=bad.read_bytes()).status_code == 415


def test_single_worker_lock_and_ticket_log_redaction(client):
    with pytest.raises(RuntimeError, match='one worker'):
        MediaStore(server.MEDIA.cfg)
    record = logging.LogRecord('uvicorn.access', 20, '', 0, '%s %s', ('GET', '/api/media/id?ticket=secret&x=1'), None)
    server.RedactTickets().filter(record)
    assert 'secret' not in record.getMessage()


def test_chunked_limits_malformed_and_incomplete_uploads(client):
    boundary = b'pinepi-boundary'
    head = b'--' + boundary + b'\r\nContent-Disposition: form-data; name="targets"\r\n\r\na\r\n--' + boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="x.png"\r\n\r\n'
    tail = b'\r\n--' + boundary + b'--\r\n'
    headers = {**auth(), 'Content-Type': 'multipart/form-data; boundary=pinepi-boundary'}
    def pieces(body):
        for start in range(0, len(body), 7):
            yield body[start:start + 7]
    # No Content-Length: parser must count actual streamed file bytes.
    server.MEDIA.cfg['max_upload_bytes'] = len(png()) - 1
    assert client.post('/api/media', headers=headers, content=pieces(head + png() + tail)).status_code == 413
    assert not list(server.MEDIA.root.glob('.upload-*'))
    server.MEDIA.cfg['max_upload_bytes'] = 100000
    assert client.post('/api/media', headers=headers, content=pieces(head + png())).status_code == 422
    assert client.post('/api/media', headers=headers, content=b'--wrong\r\n').status_code == 422
    assert not list(server.MEDIA.root.glob('.upload-*'))
    assert client.post('/api/send', headers={**auth(), 'Content-Type': 'application/json'}, content=pieces(b'{"text":"' + b'x' * 70000 + b'"}')).status_code == 413


def test_target_revocation_and_metadata_limits(client):
    m = upload(client).json()
    url = ticket(client, m).json()['url']
    server.MEDIA.assets[m['media_id']]['targets'].remove('a')
    assert client.get(url, headers=auth('device-a')).status_code == 404
    assert ticket(client, m).status_code == 404
    server.MEDIA.cfg['max_deliveries_per_asset'] = 1
    response = publish(client, m, targets=['b'])
    assert response.status_code == 200
    assert publish(client, m, targets=['b']).status_code == 429
    for body in [{'media_id': m['media_id'], 'media_urls': ['https://cdn.example.org/a']},
                 {'media_id': m['media_id'], 'media_ids': [m['media_id']]}, {'media_ids': ['one', 'two']}]:
        assert client.post('/api/send', headers=auth(), json=body).status_code == 422
    server.MEDIA.cfg['max_assets'] = 1
    assert upload(client).status_code == 507


def test_republish_during_grace_and_unpublished_expiry(client):
    m = upload(client, targets='a').json()
    first = publish(client, m, ['a']).json()['delivery_ids'][0]
    assert ack(client, m, first).status_code == 200
    assert server.MEDIA.assets[m['media_id']]['completed_at'] is not None
    second = publish(client, m, ['a']).json()['delivery_ids'][0]
    assert server.MEDIA.assets[m['media_id']]['completed_at'] is None
    assert len(server.MEDIA.pending('a')) == 1
    assert ack(client, m, second).status_code == 200
    abandoned = upload(client).json()
    meta = server.MEDIA.assets[abandoned['media_id']]
    meta['expires_at'] = time.time() - 1
    server.MEDIA.cleanup()
    assert meta['state'] == 'expired' and meta['deliveries'] == []
    tmp = server.MEDIA.root / m['media_id'] / 'meta.tmp'
    tmp.write_text('orphan')
    os.utime(tmp, (0, 0))
    server.MEDIA.cleanup()
    assert not tmp.exists()


def test_partial_reader_retry_and_if_range(client):
    m = upload(client).json()
    descriptor = ticket(client, m).json()
    url = descriptor['url']
    _, file = server.MEDIA.open_download(m['media_id'], 'a', url.split('ticket=')[1])
    partial = file.read(10)
    server.MEDIA.release_download(m['media_id'], file)
    response = client.get(url, headers={**auth('device-a'), 'Range': 'bytes=10-'})
    assert response.status_code == 206
    assert hashlib.sha256(partial + response.content).hexdigest() == m['sha256']
    full = client.get(url, headers={**auth('device-a'), 'Range': 'bytes=10-', 'If-Range': 'old-etag'})
    assert full.status_code == 200 and full.content == png()
    assert server.MEDIA.assets[m['media_id']]['state'] == 'active'
    assert server.MEDIA.downloads == {}


def test_upload_timeout_pixel_limit_and_restart_rate(client):
    import asyncio
    from starlette.requests import Request
    server.MEDIA.cfg['max_image_pixels'] = 1
    assert upload(client).status_code == 415
    server.MEDIA.cfg['max_image_pixels'] = 16000000
    server.MEDIA.cfg['uploads_per_minute'] = 1
    cfg = dict(server.MEDIA.cfg)
    server.MEDIA.close()
    server.MEDIA = MediaStore(cfg)
    assert upload(client).status_code == 429
    server.MEDIA.cfg['uploads_per_minute'] = 10
    server.MEDIA.cfg['upload_timeout_seconds'] = 0.01
    async def check():
        async def slow_receive():
            await asyncio.sleep(1)
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        request = Request({'type': 'http', 'headers': [(b'content-type', b'multipart/form-data; boundary=test')]}, slow_receive)
        with pytest.raises(server.HTTPException) as error:
            await server.MEDIA.upload(request, 'timeout-owner', lambda targets: targets)
        assert error.value.status_code == 408
    asyncio.run(check())
    assert not list(server.MEDIA.root.glob('.upload-*'))


def test_producer_token_rotation_preserves_asset_permissions_and_quotas(client, monkeypatch):
    import copy
    from identities import Identities
    m = upload(client, targets='a').json()
    meta = server.MEDIA.assets[m['media_id']]
    assert meta['producer_id'] == 'web-console'
    assert meta['owner'] == server.MEDIA.owner('web-console')
    assert meta['owner'] != hashlib.sha256(b'producer').hexdigest()
    assert publish(client, m, ['a']).status_code == 200
    assert server.PRODUCER_RATE_LIMITS['web-console'].count == 1
    before_rate = dict(server.MEDIA.rates)
    cfg = copy.deepcopy(server._config)
    cfg['producers']['web-console']['token'] = 'rotated-producer'
    cfg['producers']['web-console']['description'] = 'A clearer console description'
    cfg['devices']['a']['description'] = 'Renamed description, same device ID'
    monkeypatch.setattr(server, 'IDENTITIES', Identities(cfg))
    assert publish(client, m, ['a'], token='producer').status_code == 401
    assert publish(client, m, ['a'], token='web-console').status_code == 401
    assert publish(client, m, ['a'], token='rotated-producer').status_code == 200
    assert server.PRODUCER_RATE_LIMITS['web-console'].count == 2
    assert 'rotated-producer' not in server.PRODUCER_RATE_LIMITS
    assert publish(client, m, ['a'], token='other').status_code == 404
    assert ticket(client, m).status_code == 200
    store_cfg = dict(server.MEDIA.cfg)
    store_cfg['uploads_per_minute'] = 1
    server.MEDIA.close()
    server.MEDIA = MediaStore(store_cfg)
    assert server.MEDIA.rates == before_rate
    assert upload(client, targets='a', token='rotated-producer').status_code == 429


def test_mid_forwards_hub_tts_submission_without_injecting_speech(client):
    event = {'type': 'home.message', 'version': 2, 'id': 'hub-tts-submission', 'source': 'Web console',
             'created_at': '2026-10-31T14:02:44.412Z', 'kind': 'TEXT', 'text_mode': 'TTS', 'text': '早上好。'}
    with client.websocket_connect('/ws/') as ws:
        ws.send_json({'device': 'a', 'token': 'device-a'})
        response = client.post('/api/send', headers=auth(), json={'targets': ['a'], 'event': event})
        assert response.status_code == 200
        received = ws.receive_json()
        assert received['event'] == event
        assert 'speech' not in received['event']


def test_mp3_upload_download_and_format_validation(client, tmp_path):
    path = tmp_path / 'sample.mp3'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.2', '-c:a', 'libmp3lame', str(path)], check=True)
    response = upload(client, targets='a', data=path.read_bytes())
    assert response.status_code == 201, response.text
    media = response.json()
    assert media['mime_type'] == 'audio/mpeg'
    assert media['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert client.get(ticket(client, media).json()['url'], headers=auth('device-a')).content == path.read_bytes()
    assert upload(client, targets='a', data=b'ID3' + b'fake audio data' * 30).status_code == 415
    aac = tmp_path / 'other.aac'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.2', '-c:a', 'aac', str(aac)], check=True)
    assert upload(client, targets='a', data=aac.read_bytes()).status_code == 415


@pytest.mark.parametrize('kind', ['IMAGE', 'AUDIO', 'VIDEO'])
def test_uploaded_media_hub_cache_ack_and_mid_cleanup(client, tmp_path, monkeypatch, kind):
    """Optional two-repository integration; no remote sockets or real credentials."""
    source = os.environ.get('PINEPI_HUB_SOURCE')
    if not source:
        pytest.skip('Set PINEPI_HUB_SOURCE to the companion Hub src directory')
    monkeypatch.syspath_prepend(source)
    from email.message import Message
    from unittest.mock import AsyncMock
    from urllib.error import HTTPError
    from pinepi_hub.app import create_app
    from pinepi_hub.broadcast_mid import BroadcastMid
    from pinepi_hub.link_test import LinkTest
    from pinepi_hub.mid_media import MidMediaCache
    from pinepi_hub.notifications import NotificationStore
    import asyncio
    if kind == 'IMAGE':
        content = png()
    else:
        path = tmp_path / ('file.mp3' if kind == 'AUDIO' else 'file.mp4')
        generation = ['-f','lavfi','-i','sine=frequency=440:duration=0.2','-c:a','libmp3lame'] if kind == 'AUDIO' else ['-f','lavfi','-i','color=c=blue:s=32x32:d=0.2','-c:v','libx264','-pix_fmt','yuv420p']
        subprocess.run(['ffmpeg','-v','error',*generation,str(path)], check=True)
        content = path.read_bytes()
    response = upload(client, targets='a', data=content)
    assert response.status_code == 201, response.text
    media = response.json()
    protocol = str(Path(__file__).resolve().parents[1] / 'examples/web/message-protocol.js')
    js = f"const P=require({json.dumps(protocol)}); const kind=process.argv[2]; process.stdout.write(JSON.stringify(P.buildRequest({{...P.PRESETS[kind.toLowerCase()],severity:'CRITICAL',uploadedMedia:JSON.parse(process.argv[1])}},{{id:'integration-'+kind,createdAt:'2026-10-31T14:00:00Z'}},['a'])));"
    published = client.post('/api/send',headers=auth(),json=json.loads(subprocess.run(['node','-e',js,json.dumps(media),kind],text=True,capture_output=True,check=True).stdout))
    assert published.status_code == 200, published.text
    envelope = server.MEDIA.pending('a')[0]
    hub_config = tmp_path/'mid-config.json'
    hub_config.write_text(json.dumps({'url':'wss://mid.example.com/ws/','device':'a','token':'device-a'}))
    link = LinkTest('d'*40,'c'*40,trusted_lan=True)
    link.notifications = NotificationStore(tmp_path/'pending.json')
    directory = tmp_path/'hub-media';directory.mkdir()
    cache = MidMediaCache(hub_config,directory,'http://192.168.1.2:8766/media/',link)
    class Response(io.BytesIO):
        def __init__(self, body, code, content_type):
            super().__init__(body);self.status=code;self.headers=Message();self.headers['Content-Type']=content_type
    def open_(request,timeout):
        response=client.request(request.get_method(),request.full_url,headers=dict(request.header_items()),content=request.data)
        if response.status_code>=400:
            raise HTTPError(request.full_url,response.status_code,'request failed',{},io.BytesIO(response.content))
        return Response(response.content,response.status_code,response.headers['content-type'])
    cache.opener.open=open_
    ws=AsyncMock()
    async def acknowledge(raw):
        confirmation=json.loads(raw)
        assert confirmation['type']=='media.cached'
        assert len(NotificationStore(link.notifications.path).messages)==1
        assert next(directory.glob('mid-*')).read_bytes()==content
        confirmed=client.post('/api/media/'+media['media_id']+'/cached',headers=auth('device-a'),json=confirmation)
        assert confirmed.status_code==200
    ws.send.side_effect=acknowledge
    receiver=BroadcastMid('unused',link.publish_mid_notification,media_cache=cache)
    asyncio.run(receiver.handle(ws,json.dumps(envelope)))
    assert receiver.received==1
    saved=link.notifications.messages[0]
    assert saved['severity']=='CRITICAL' and saved['media']['mime_type']==media['mime_type']
    assert set(saved['media'])=={'url','mime_type'}
    server.MEDIA.assets[media['media_id']]['completed_at']-=2
    server.MEDIA.cleanup()
    assert not (server.MEDIA.root/media['media_id']/'content').exists()
    with TestClient(create_app(link_test=link,media_directory=directory)) as hub:
        local='/media/'+saved['media']['url'].rsplit('/',1)[1]
        assert hub.get(local).content==content
        assert hub.get(local,headers={'Range':'bytes=0-9'}).status_code==206
