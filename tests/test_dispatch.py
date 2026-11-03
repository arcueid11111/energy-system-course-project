import threading
import time

import pytest

from backend.dispatch import Dispatcher


def until(check, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("等待任务状态超时")


def test_pause_cancel_queued_and_resume_persistence(tmp_path):
    executed = []

    def handler(kind, payload, progress):
        executed.append(payload["n"])
        progress(0.5, "执行中")
        return {"n": payload["n"]}

    manager = Dispatcher(tmp_path / "queue.sqlite", handler)
    try:
        manager.pause(True)
        first, second = manager.submit_many(
            [("simulation", "one", {"n": 1}), ("simulation", "two", {"n": 2})]
        )
        assert manager.snapshot()["counts"]["queued"] == 2
        manager.cancel(first["id"])
        manager.pause(False)
        until(lambda: manager.get(second["id"])["state"] == "succeeded")
        assert executed == [2]
        assert manager.get(first["id"])["state"] == "cancelled"
        assert manager.get(second["id"])["result"] == {"n": 2}
    finally:
        manager.stop()


def test_running_cancel_waits_for_cooperative_boundary(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def handler(kind, payload, progress):
        entered.set()
        release.wait(timeout=2)
        progress(0.8, "原生调用已返回")
        return {"done": True}

    manager = Dispatcher(tmp_path / "queue.sqlite", handler)
    try:
        job = manager.submit("simulation", "test", {})
        assert entered.wait(timeout=2)
        assert manager.cancel(job["id"])["state"] == "cancelling"
        release.set()
        until(lambda: manager.get(job["id"])["state"] == "cancelled")
        assert manager.get(job["id"])["result"] is None
    finally:
        release.set()
        manager.stop()


def test_batch_capacity_is_atomic_and_restart_marks_interrupted(tmp_path):
    path = tmp_path / "queue.sqlite"
    manager = Dispatcher(path, lambda *args: {})
    manager.pause(True)
    try:
        jobs = manager.submit_many([("simulation", str(n), {}) for n in range(50)])
        with pytest.raises(ValueError):
            manager.submit_many([("simulation", "overflow", {}), ("simulation", "overflow2", {})])
        assert manager.snapshot()["counts"]["queued"] == 50
    finally:
        manager.stop()
    with manager.db() as con:
        con.execute("UPDATE jobs SET state='running' WHERE id=?", (jobs[0]["id"],))
    recovered = Dispatcher(path, lambda *args: {})
    try:
        assert recovered.snapshot()["paused"] is True
        assert recovered.get(jobs[0]["id"])["state"] == "failed"
        assert "服务中断" in recovered.get(jobs[0]["id"])["error"]
    finally:
        recovered.stop()
