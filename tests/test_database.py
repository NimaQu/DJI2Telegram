from datetime import datetime, timezone
from pathlib import Path

from qdc507_gateway.models import CallDirection, CallRecord, CallState
from qdc507_gateway.storage.database import Database


def test_call_records_are_persisted_with_enum_and_datetime_values():
    database = Database(":memory:")
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    record = CallRecord(
        id="call-1",
        direction=CallDirection.outbound_cellular,
        state=CallState.active,
        cellular_number="+12045550100",
        started_at=started,
        connected_at=started,
    )

    database.save_call(record)
    row = database.connection.execute(
        "SELECT * FROM call_records WHERE id = ?", (record.id,)
    ).fetchone()

    assert row["direction"] == "outbound_cellular"
    assert row["state"] == "active"
    assert row["started_at"] == started.isoformat()


def test_file_database_and_parent_are_private(tmp_path):
    path = Path(tmp_path) / "data" / "gateway.sqlite3"
    database = Database(path)
    database.close()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_sms_retains_latest_100_and_cancels_only_evicted_jobs():
    database = Database(':memory:')
    database.push_scope = ('sandbox', 'app.test')
    database.register_push_device('device', 'aa', 'sandbox', 'app.test')
    database.record_sms_pdu('old-hash', 'now')
    for i in range(101):
        database.save_sms({'id': str(i), 'sender': 'self' if i % 2 else '+123',
            'body': 'test', 'timestamp': f'2026-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00'}, inbound=True)
    assert len(database.list_sms(500)) == 100
    assert database.get_sms('0') is None
    assert database.get_sms('1') is not None
    assert database.connection.execute('SELECT COUNT(*) FROM push_jobs').fetchone()[0] == 100
    assert database.connection.execute("SELECT COUNT(*) FROM push_jobs WHERE sms_id='0'").fetchone()[0] == 0
    assert not database.record_sms_pdu('old-hash', 'later')
    # A delayed old SMS cannot evict a newer one or leave an orphan push task.
    database.save_sms({'id': 'delayed', 'sender': '+123', 'body': 'old',
                       'timestamp': '2025-01-01T00:00:00+00:00'}, inbound=True)
    assert database.get_sms('delayed') is None
    assert database.connection.execute('SELECT COUNT(*) FROM push_jobs').fetchone()[0] == 100


def test_sms_retention_cleans_existing_database_and_same_timestamp_ties(tmp_path):
    database = Database(tmp_path / 'retention.sqlite3')
    with database.connection:
        database.connection.executemany(
            'INSERT INTO sms_messages(id,sender,body,timestamp) VALUES (?,?,?,?)',
            [(str(i), '+123', 'body', '2026-01-01T00:00:00+00:00') for i in range(105)],
        )
        database.connection.execute("INSERT INTO push_jobs VALUES ('old-job','0','device','version',0,0,0)")
    database.close()
    database = Database(tmp_path / 'retention.sqlite3')
    assert len(database.list_sms(500)) == 100
    assert database.list_sms(1)[0]['id'] == '104'
    assert database.get_sms('4') is None
    assert database.get_sms('5') is not None
    assert database.connection.execute('SELECT COUNT(*) FROM push_jobs').fetchone()[0] == 0
    database.close()
