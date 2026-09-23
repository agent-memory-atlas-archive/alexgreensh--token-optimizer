"""The compression-health probe must see live WAL writes, not just the checkpoint.

WHY THIS EXISTS
---------------
Hermes keeps state.db in WAL mode, so a live session's compression-health
columns sit in the -wal file until a checkpoint. The reader's default
``immutable=1`` view ignores the WAL entirely — fine for collecting ended
sessions, blind for the in-session health probe, which would read a stale
(or absent) row and never see the failure it's watching for. The probe uses a
plain ``mode=ro`` connection so committed WAL writes are visible.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import hermes_state  # noqa: E402


def test_live_read_sees_wal_only_writes(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    writer = sqlite3.connect(str(db))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, compression_ineffective_count INTEGER)"
    )
    writer.execute("INSERT INTO sessions VALUES ('s-live', 2)")
    # Committed to the -wal file only; holding the writer open blocks the
    # checkpoint that would flush it into the main database file.
    writer.commit()

    monkeypatch.setattr(hermes_state, "state_db_path", lambda: db)
    try:
        # The immutable snapshot view cannot see rows still living in the WAL.
        assert hermes_state.get_session("s-live") is None
        # The health probe's WAL-visible read can.
        row = hermes_state.get_session("s-live", live=True)
        assert row is not None
        assert row["compression_ineffective_count"] == 2
    finally:
        writer.close()
