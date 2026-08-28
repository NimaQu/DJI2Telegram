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
        telegram_user_id=42,
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
    assert row["telegram_user_id"] == 42


def test_file_database_and_parent_are_private(tmp_path):
    path = Path(tmp_path) / "data" / "gateway.sqlite3"
    database = Database(path)
    database.close()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
