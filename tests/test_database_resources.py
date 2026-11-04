"""Database transactions release file handles without waiting for garbage collection."""

import sqlite3

import pytest

from backend import store
from backend.dispatch import Dispatcher


@pytest.mark.parametrize("database", ["dispatcher", "results"])
def test_transactions_close_connections_and_preserve_commit_rollback(tmp_path, database):
    manager = Dispatcher(tmp_path / "queue.sqlite", lambda *args: {})
    token = store.DATABASE_OVERRIDE.set(str(tmp_path / "results.sqlite"))
    transaction = manager.db if database == "dispatcher" else store.db
    try:
        with transaction() as committed:
            committed.execute("CREATE TABLE probe (value INTEGER)")
            committed.execute("INSERT INTO probe VALUES (1)")
        # Retaining the connection reference must not retain the Windows file lock.
        with pytest.raises(sqlite3.ProgrammingError):
            committed.execute("SELECT 1")
        with pytest.raises(RuntimeError, match="rollback"):
            with transaction() as rolled_back:
                rolled_back.execute("INSERT INTO probe VALUES (2)")
                raise RuntimeError("rollback")
        with pytest.raises(sqlite3.ProgrammingError):
            rolled_back.execute("SELECT 1")
        with transaction() as check:
            assert check.execute("SELECT value FROM probe").fetchall()[0][0] == 1
            assert check.execute("SELECT count(*) FROM probe").fetchone()[0] == 1
    finally:
        manager.stop()
        store.DATABASE_OVERRIDE.reset(token)
