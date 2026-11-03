import json
import sqlite3
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

DATABASE_OVERRIDE = ContextVar("pump_twin_database", default=None)


def db():
    path = Path(DATABASE_OVERRIDE.get() or Path.home() / ".flowstate" / "results.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute(
        "CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL)"
    )
    return con


def save(kind, name, payload):
    identifier = uuid.uuid4().hex
    value = {**payload, "id": identifier, "created_at": datetime.now(timezone.utc).isoformat()}
    with db() as con:
        con.execute(
            "INSERT INTO records VALUES(?,?,?,?,?)",
            (
                identifier,
                kind,
                name,
                value["created_at"],
                json.dumps(value, ensure_ascii=False, allow_nan=False),
            ),
        )
    return value


def get(identifier, kind):
    with db() as con:
        row = con.execute(
            "SELECT payload FROM records WHERE id=? AND kind=?", (identifier, kind)
        ).fetchone()
    if not row:
        raise KeyError("记录不存在")
    return json.loads(row[0])


def listing(kind):
    with db() as con:
        rows = con.execute(
            "SELECT id,name,created_at,payload FROM records WHERE kind=? ORDER BY created_at DESC LIMIT 100",
            (kind,),
        ).fetchall()
    result = []
    for identifier, name, created, payload in rows:
        value = json.loads(payload)
        result.append(
            {"id": identifier, "name": name, "created_at": created, "summary": value.get("summary")}
        )
    return result
