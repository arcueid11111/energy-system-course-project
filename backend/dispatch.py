"""Persistent single-worker dispatch. A cancelled native solve returns before stop completes."""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Cancelled(Exception):
    pass


class Dispatcher:
    def __init__(self, path: Path, handler):
        self.path = path
        self.handler = handler
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.closed = threading.Event()
        self.thread = None
        with self.db() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL, progress REAL NOT NULL, message TEXT NOT NULL, payload TEXT NOT NULL, result TEXT, error TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS scheduler(id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL)"
            )
            con.execute("INSERT OR IGNORE INTO scheduler VALUES(1,0)")
            con.execute(
                "UPDATE jobs SET state='failed',error='服务中断：运行中的任务未自动重试，请检查后手动重新提交',finished_at=? WHERE state IN ('running','cancelling')",
                (now(),),
            )

    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def start(self):
        with self.lock:
            if self.thread is None:
                self.thread = threading.Thread(
                    target=self.loop, name="pump-twin-dispatch", daemon=True
                )
                self.thread.start()

    def stop(self):
        self.closed.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=20)

    def submit(self, kind, name, payload):
        return self.submit_many([(kind, name, payload)])[0]

    def submit_many(self, items):
        identifiers = []
        with self.lock, self.db() as con:
            pending = con.execute(
                "SELECT count(*) FROM jobs WHERE state IN ('queued','running','cancelling')"
            ).fetchone()[0]
            if pending + len(items) > 50:
                raise ValueError("队列最多保留 50 个待处理任务，本批次未提交")
            for kind, name, payload in items:
                identifier = uuid.uuid4().hex
                identifiers.append(identifier)
                con.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        kind,
                        name,
                        "queued",
                        0,
                        "等待调度",
                        json.dumps(payload, ensure_ascii=False),
                        None,
                        None,
                        now(),
                        None,
                        None,
                    ),
                )
        self.start()
        self.wake.set()
        return [self.get(identifier) for identifier in identifiers]

    def get(self, identifier):
        with self.db() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise KeyError("任务不存在")
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        value["result"] = json.loads(value["result"]) if value["result"] else None
        return value

    def snapshot(self):
        with self.db() as con:
            paused = bool(con.execute("SELECT paused FROM scheduler WHERE id=1").fetchone()[0])
            rows = con.execute(
                "SELECT id,kind,name,state,progress,message,error,created_at,started_at,finished_at,result FROM jobs ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
            counts = {
                r["state"]: r["n"]
                for r in con.execute("SELECT state,count(*) AS n FROM jobs GROUP BY state")
            }
        jobs = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item["result"]) if item["result"] else None
            jobs.append(item)
        return {
            "paused": paused,
            "jobs": jobs,
            "counts": counts,
            "worker_count": 1,
            "notice": "暂停仅阻止启动排队任务；正在执行的任务继续。取消原生水力求解需等待当前调用返回。",
        }

    def pause(self, paused):
        with self.lock, self.db() as con:
            con.execute("UPDATE scheduler SET paused=? WHERE id=1", (int(paused),))
        self.wake.set()
        return self.snapshot()

    def cancel(self, identifier):
        with self.lock, self.db() as con:
            row = con.execute("SELECT state FROM jobs WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise KeyError("任务不存在")
            if row["state"] == "queued":
                con.execute(
                    "UPDATE jobs SET state='cancelled',message='排队任务已取消',finished_at=? WHERE id=?",
                    (now(), identifier),
                )
            elif row["state"] == "running":
                con.execute(
                    "UPDATE jobs SET state='cancelling',message='已请求取消，等待当前计算返回' WHERE id=?",
                    (identifier,),
                )
        self.wake.set()
        return self.get(identifier)

    def progress(self, identifier, fraction, message):
        with self.lock, self.db() as con:
            row = con.execute("SELECT state FROM jobs WHERE id=?", (identifier,)).fetchone()
            if row["state"] == "cancelling" or self.closed.is_set():
                raise Cancelled()
            con.execute(
                "UPDATE jobs SET progress=?,message=? WHERE id=?",
                (min(0.99, max(0, float(fraction))), message, identifier),
            )

    def loop(self):
        while not self.closed.is_set():
            with self.lock, self.db() as con:
                paused = bool(con.execute("SELECT paused FROM scheduler WHERE id=1").fetchone()[0])
                row = (
                    None
                    if paused
                    else con.execute(
                        "SELECT * FROM jobs WHERE state='queued' ORDER BY created_at LIMIT 1"
                    ).fetchone()
                )
                if row is not None:
                    con.execute(
                        "UPDATE jobs SET state='running',progress=.02,message='正在准备模型',started_at=? WHERE id=?",
                        (now(), row["id"]),
                    )
            if row is None:
                self.wake.wait(0.75)
                self.wake.clear()
                continue
            identifier = row["id"]
            try:
                result = self.handler(
                    row["kind"],
                    json.loads(row["payload"]),
                    lambda p, m: self.progress(identifier, p, m),
                )
                with self.lock, self.db() as con:
                    state = con.execute(
                        "SELECT state FROM jobs WHERE id=?", (identifier,)
                    ).fetchone()[0]
                    message = (
                        "计算完成，结果已保存"
                        if state != "cancelling"
                        else "结果已保存，取消请求晚于完成"
                    )
                    con.execute(
                        "UPDATE jobs SET state='succeeded',progress=1,message=?,result=?,finished_at=? WHERE id=?",
                        (message, json.dumps(result, ensure_ascii=False), now(), identifier),
                    )
            except Cancelled:
                with self.db() as con:
                    con.execute(
                        "UPDATE jobs SET state='cancelled',message='计算已取消',finished_at=? WHERE id=?",
                        (now(), identifier),
                    )
            except Exception as exc:
                with self.db() as con:
                    con.execute(
                        "UPDATE jobs SET state='failed',error=?,message='任务失败',finished_at=? WHERE id=?",
                        (str(exc)[:1500], now(), identifier),
                    )
