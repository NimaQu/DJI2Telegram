import asyncio
import json
import time
import uuid
from dataclasses import replace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from qdc507_gateway.apns import APNsService, notification_payload
from qdc507_gateway.api.app import create_app
from qdc507_gateway.config import ConfigurationError, Settings
from qdc507_gateway.events import EventBus
from qdc507_gateway.security import hash_token
from qdc507_gateway.storage.database import Database


@pytest.fixture
def settings(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / 'key.p8'
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    return Settings(apns_enabled=True, apns_key_path=path, apns_key_id='KEY123',
                    apns_team_id='TEAM123', apns_bundle_id='app.test')


def setup_service(settings, handler=None):
    database = Database(':memory:')
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler)) if handler else None
    service = APNsService(settings, database, client=client)
    installation = str(uuid.uuid4())
    service.register(installation, 'ab' * 32)
    return database, service, installation


def sms(database, sms_id='sms-test', body='测试验证码 😀'):
    message = {'id': sms_id, 'sender': '+123456789', 'body': body,
               'timestamp': '2026-09-05T12:00:00+00:00'}
    database.save_sms(message, inbound=True)
    return message


def test_config_paths_overrides_and_missing_key(settings, tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text('''[server]
public_base_url="https://djihub.fubuki.app"
[apns]
enabled=true
sandbox=true
key_path="key.p8"
key_id="KEY123"
team_id="TEAM123"
bundle_id="app.test"
''')
    loaded = Settings.load(config, environ={'QDC507_APNS_SANDBOX': 'false'})
    assert loaded.apns_key_path == settings.apns_key_path
    assert not loaded.apns_sandbox
    assert loaded.public_base_url == 'https://djihub.fubuki.app'
    with pytest.raises(ConfigurationError):
        Settings(apns_enabled=True)
    settings.apns_key_path.write_text('invalid-private-key')
    with pytest.raises(ConfigurationError, match='ES256'):
        replace(settings)
    replace(settings, apns_enabled=False)
    with pytest.raises(ConfigurationError):
        Settings(public_base_url='https://user:pass@example.com')


def test_api_registration_validation_auth_and_sms(settings):
    database, service, installation = setup_service(replace(settings, apns_enabled=False))
    database.replace_token(hash_token('test-auth'), 'now')
    client = TestClient(create_app(database, EventBus(), {'apns': service}))
    path = '/api/v1/push/devices/' + installation
    headers = {'Authorization': 'Bearer test-auth'}
    assert client.put(path, json={'device_token': 'AA'}).status_code == 401
    response = client.put(path, headers=headers, json={'device_token': 'AA' * 40})
    assert response.status_code == 200 and response.json()['enabled'] is False
    assert database.connection.execute('SELECT device_token FROM push_devices').fetchone()[0] == 'aa' * 40
    for token in ['a', 'xx', '', 'aa' * 257, 123]:
        assert client.put(path, headers=headers, json={'device_token': token}).status_code == 422
    assert client.put('/api/v1/push/devices/not-uuid', headers=headers,
                      json={'device_token': 'aa'}).status_code == 422
    sms(database)
    assert service.status()['queued'] == 0
    assert client.get('/api/v1/sms/sms-test').status_code == 401
    assert client.get('/api/v1/sms/sms-test', headers=headers).json()['body'] == '测试验证码 😀'
    assert client.get('/api/v1/sms/missing', headers=headers).status_code == 404
    assert client.delete(path).status_code == 401
    for _ in range(2):
        assert client.delete(path, headers=headers).status_code == 204


def test_registration_versions_and_token_uniqueness(settings):
    database, service, installation = setup_service(settings)
    sms(database)
    old = database.next_push_job('sandbox', 'app.test', time.time())
    service.register(installation, 'ab' * 32)
    assert database.next_push_job('sandbox', 'app.test', time.time())['version'] == old['version']
    service.register(installation, 'cd' * 32)
    assert service.status()['queued'] == 0
    database.invalidate_push_device(old, time.time() + 1)
    assert service.status()['active_devices'] == 1
    service.register(str(uuid.uuid4()), 'cd' * 32)
    assert service.status()['active_devices'] == 1
    assert service.status()['queued'] == 0  # no history replay


def test_outbox_persistence_scope_and_atomic_rollback(settings, tmp_path):
    path = tmp_path / 'db.sqlite3'
    database = Database(path)
    service = APNsService(settings, database)
    service.register(str(uuid.uuid4()), 'ab')
    sms(database)
    database.close()
    database = Database(path)
    assert APNsService(settings, database).status()['queued'] == 1
    database.connection.execute("CREATE TRIGGER fail_push BEFORE INSERT ON push_jobs BEGIN SELECT RAISE(ABORT, 'fail'); END")
    with pytest.raises(Exception, match='fail'):
        sms(database, 'must-rollback')
    assert database.get_sms('must-rollback') is None
    assert database.next_push_job('production', 'app.test', time.time()) is None
    assert database.push_counts('sandbox', 'app.test')['queued'] == 0


def test_payload_utf8_limit():
    job = {'id': 'job-test', 'installation_id': 'installation-test', 'sms_id': 'sms-' + 'a'*64, 'sender': '+1234', 'timestamp': 'now', 'body': '汉字😀' * 4000}
    encoded = notification_payload(job)
    assert len(encoded) <= 4096
    decoded = json.loads(encoded)
    assert decoded['body_truncated'] is True
    assert decoded['aps']['alert']['body'].endswith('…')
    assert decoded['sms_id'] == job['sms_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('sandbox', [True, False])
async def test_request_jwt_and_success(settings, sandbox):
    settings = replace(settings, apns_sandbox=sandbox)
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200)
    database, service, _ = setup_service(settings, handler)
    sms(database, 'sms-' + 'a'*64)
    assert await service.send_one()
    request = captured[0]
    assert request.url.host == ('api.sandbox.push.apple.com' if sandbox else 'api.push.apple.com')
    assert request.headers['apns-topic'] == 'app.test'
    assert request.headers['apns-push-type'] == 'alert'
    assert request.headers['apns-priority'] == '10'
    assert len(request.headers['apns-collapse-id'].encode()) <= 64
    token = request.headers['authorization'].split()[1]
    key = serialization.load_pem_private_key(settings.apns_key_path.read_bytes(), password=None)
    assert jwt.decode(token, key.public_key(), algorithms=['ES256'])['iss'] == 'TEAM123'
    assert jwt.get_unverified_header(token)['kid'] == 'KEY123'
    assert service.provider_token() == token
    service._jwt_at -= 3100
    service.clock = lambda: time.time() + 1
    assert service.provider_token() != token
    assert service.status()['queued'] == 1
    payload = json.loads(request.content)
    assert payload['notification_id'] == request.headers['apns-id']
    database.acknowledge_sms(payload['sms_id'])
    assert service.status()['queued'] == 0
    await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('code,reason', [(429, 'TooManyRequests'), (500, 'InternalServerError')])
async def test_retry_after_and_expiry(settings, code, reason):
    database, service, _ = setup_service(settings, lambda r: httpx.Response(
        code, json={'reason': reason}, headers={'retry-after': '120'}))
    sms(database)
    now = time.time()
    await service.send_one()
    row = database.connection.execute('SELECT * FROM push_jobs').fetchone()
    assert row['attempts'] == 1 and row['next_attempt'] >= now + 120
    assert database.next_push_job('sandbox', 'app.test', now + 86401) is None
    await service.stop()


@pytest.mark.asyncio
async def test_transport_error_and_credentials_pause(settings):
    def timeout(request):
        raise httpx.ReadTimeout('sensitive URL must not be logged', request=request)
    database, service, _ = setup_service(settings, timeout)
    sms(database)
    await service.send_one()
    assert service.last_error == 'transport_error'
    database.connection.execute('UPDATE push_jobs SET next_attempt=0')
    await service.client.aclose()
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={'reason': 'InvalidProviderToken'})))
    await service.send_one()
    assert service.paused and service.status()['active_devices'] == 1
    assert service.status()['queued'] == 1
    await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('fresh', [False, True])
async def test_410_respects_registration_time(settings, fresh):
    database, service, installation = setup_service(settings)
    sms(database)
    row = database.next_push_job('sandbox', 'app.test', time.time())
    cutoff = row['registered_at'] + (1 if not fresh else -1)
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(410, json={'reason': 'Unregistered', 'timestamp': cutoff * 1000})))
    await service.send_one()
    assert service.status()['active_devices'] == int(fresh)
    await service.stop()


@pytest.mark.asyncio
async def test_slow_delivery_does_not_block_ingress_and_delete_race(settings):
    started, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        started.set()
        await release.wait()
        return httpx.Response(400, json={'reason': 'BadDeviceToken'})
    database, service, installation = setup_service(settings, handler)
    sms(database)
    task = asyncio.create_task(service.send_one())
    await started.wait()
    sms(database, 'second')
    assert service.status()['queued'] == 2
    database.delete_push_device(installation)
    service.register(installation, 'ef')
    release.set()
    await task
    assert service.status()['queued'] == 0 and service.status()['active_devices'] == 1
    await service.stop()


def test_complete_sms_only_queues_once(settings, monkeypatch):
    from qdc507_gateway.modem import sms as module
    database, service, _ = setup_service(settings)
    parts = {
        '01': module.SMSPart('+123', 'hello ', None, '01', 7, 2, 1),
        '02': module.SMSPart('+123', 'world', None, '02', 7, 2, 2),
    }
    monkeypatch.setattr(module, 'decode_deliver', lambda pdu: parts[pdu])
    ingress = module.SMSIngress(database)
    assert ingress.ingest('02') is None
    assert service.status()['queued'] == 0
    assert ingress.ingest('01')['body'] == 'hello world'
    assert service.status()['queued'] == 1
    assert ingress.ingest('01') is None
    assert ingress.ingest('02') is None
    assert service.status()['queued'] == 1
    database.save_sms({'id': 'outbound', 'sender': '+123', 'body': 'sent', 'timestamp': 'now'})
    assert service.status()['queued'] == 1


def test_changing_scope_requires_reregistration(settings):
    database, service, installation = setup_service(settings)
    sms(database)
    APNsService(replace(settings, apns_sandbox=False), database)
    restored = APNsService(settings, database)
    assert restored.status()['active_devices'] == 0
    assert restored.status()['queued'] == 0
    restored.register(installation, 'ab' * 32)
    assert restored.status()['active_devices'] == 1


@pytest.mark.asyncio
async def test_worker_lifecycle_recovers_persisted_jobs(settings):
    database, service, _ = setup_service(settings, lambda r: httpx.Response(200))
    sms(database)
    await service.start()
    for _ in range(100):
        row = database.connection.execute('SELECT * FROM push_jobs').fetchone()
        if row['attempts'] == 1:
            break
        await asyncio.sleep(.01)
    assert row['attempts'] == 1 and service.status()['queued'] == 1
    database.acknowledge_sms(row['sms_id'])
    assert service.status()['queued'] == 0
    await service.stop()
    assert service.task is None and service.client.is_closed




def test_mutable_content_is_inside_aps_and_included_in_size_budget():
    payload = json.loads(notification_payload({'id': 'job-test', 'installation_id': 'installation-test', 'sms_id': 'sms-test', 'sender': '+123',
        'timestamp': '2026-01-01T06:30:00+00:00', 'body': 'test'}))
    assert payload['aps']['mutable-content'] == 1
    assert 'mutable-content' not in {k: v for k, v in payload.items() if k != 'aps'}
    assert payload['timestamp'] == '2026-01-01T06:30:00+00:00'
    payload_bytes = notification_payload({'id': 'job-test', 'installation_id': 'installation-test', 'sms_id': 'sms-test', 'sender': '+123',
        'timestamp': '2026-01-01T06:30:00+00:00', 'body': '中文😀' * 4000})
    assert len(payload_bytes) <= 4096
    assert json.loads(payload_bytes)['aps']['mutable-content'] == 1


def test_old_sms_utc_migration_is_idempotent_and_preserves_queue(tmp_path):
    import sqlite3
    path = tmp_path / 'old.sqlite3'
    # Minimal pre-migration database; no schema_migrations marker exists yet.
    c = sqlite3.connect(path)
    c.execute('CREATE TABLE sms_messages(id TEXT PRIMARY KEY,sender TEXT,body TEXT,timestamp TEXT,is_read INTEGER,raw_pdus TEXT)')
    pdu = '00040D91683108108300F000086210101003000A046D4B8BD5'
    c.execute('INSERT INTO sms_messages VALUES (?,?,?,?,?,?)',
              ('old', '+123', '测试', '2026-01-01T01:30:00+00:00', 0, json.dumps([pdu])))
    c.execute('INSERT INTO sms_messages VALUES (?,?,?,?,?,?)',
              ('fallback', '+123', 'test', '2026-01-01T01:30:00+00:00', 1, 'invalid-json'))
    c.commit()
    c.close()
    db = Database(path)
    assert db.get_sms('old')['timestamp'] == '2026-01-01T06:30:00+00:00'
    assert db.get_sms('fallback')['timestamp'] == '2026-01-01T01:30:00+00:00'
    assert db.get_sms('fallback')['is_read'] == 1
    assert db.connection.execute('SELECT COUNT(*) FROM push_jobs').fetchone()[0] == 0
    db.close()
    db = Database(path)
    assert db.get_sms('old')['timestamp'] == '2026-01-01T06:30:00+00:00'
    assert db.connection.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] == 1
    db.close()


@pytest.mark.asyncio
async def test_ack_timeout_resends_stable_id_and_stops_after_ack(settings):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200)
    database, service, installation = setup_service(settings, handler)
    sms(database)
    now = time.time()
    service.clock = lambda: now
    await service.send_one()
    row = database.connection.execute('SELECT * FROM push_jobs').fetchone()
    assert now + 60 <= row['next_attempt'] <= now + 61
    assert not await service.send_one()
    now = row['next_attempt']
    await service.send_one()
    row = database.connection.execute('SELECT * FROM push_jobs').fetchone()
    assert now + 120 <= row['next_attempt'] <= now + 121
    assert requests[0].content == requests[1].content
    assert requests[0].headers['apns-id'] == requests[1].headers['apns-id']
    database.acknowledge_sms(row['sms_id'])
    now += 3600
    assert not await service.send_one()
    assert database.get_sms('sms-test')['is_read'] == 0
    await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', [200, 500, 'timeout'])
async def test_ack_during_inflight_request_cannot_resurrect_job(settings, outcome):
    started, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        started.set()
        await release.wait()
        if outcome == 'timeout':
            raise httpx.ReadTimeout('timeout', request=request)
        return httpx.Response(outcome)
    database, service, installation = setup_service(settings, handler)
    sms(database)
    task = asyncio.create_task(service.send_one())
    await started.wait()
    row = database.connection.execute('SELECT * FROM push_jobs').fetchone()
    database.acknowledge_sms(row['sms_id'])
    release.set()
    await task
    assert service.status()['queued'] == 0
    await service.stop()


def test_sms_ack_api_stops_all_devices_only_for_requested_sms(settings, tmp_path):
    path = tmp_path / 'global-ack.sqlite3'
    db = Database(path)
    service = APNsService(settings, db)
    service.register(str(uuid.uuid4()), 'aa')
    service.register(str(uuid.uuid4()), 'bb')
    sms(db, 'sms-one')
    sms(db, 'sms-two')
    assert service.status()['queued'] == 4
    db.replace_token(hash_token('global-test'), 'now')
    client = TestClient(create_app(db, EventBus(), {'apns': service}))
    old_path = f'/api/v1/push/devices/{uuid.uuid4()}/notifications/{uuid.uuid4()}/ack'
    assert client.post(old_path).status_code == 404
    assert '/api/v1/push/devices/{installation_id}/notifications/{notification_id}/ack' not in client.get('/openapi.json').json()['paths']
    endpoint = '/api/v1/sms/sms-one/ack'
    assert client.post(endpoint).status_code == 401
    assert service.status()['queued'] == 4
    headers = {'Authorization': 'Bearer global-test'}
    for url in (endpoint, endpoint, '/api/v1/sms/missing/ack'):
        assert client.post(url, headers=headers).status_code == 204
    assert service.status()['queued'] == 2
    assert db.get_sms('sms-one')['is_read'] == 0
    db.close()
    db = Database(path)
    assert db.connection.execute("SELECT COUNT(*) FROM push_jobs WHERE sms_id='sms-one'").fetchone()[0] == 0
    assert db.connection.execute("SELECT COUNT(*) FROM push_jobs WHERE sms_id='sms-two'").fetchone()[0] == 2
    db.close()
