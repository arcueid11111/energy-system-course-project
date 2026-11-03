import os
from pathlib import Path

from backend import store
from backend.jobs import execute_design_task
from backend.dispatch import Dispatcher


class Controller:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._lock_stream = (self.folder / "desktop.lock").open("a+b")
        self._lock_stream.write(b"0")
        self._lock_stream.flush()
        self._lock_stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._lock_stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_stream.close()
            raise ValueError(
                "该工作目录已被另一个 GUI 实例使用，请切回现有窗口或用 --data-dir 指定其他目录"
            ) from exc
        self.database = self.folder / "desktop.sqlite3"

        def handler(kind, payload, progress):
            token = store.DATABASE_OVERRIDE.set(str(self.database))
            try:
                return execute_design_task(kind, payload, progress, store.save)
            finally:
                store.DATABASE_OVERRIDE.reset(token)

        self.manager = Dispatcher(self.folder / "desktop-dispatch.sqlite3", handler)
        self.manager.start()

    def submit(self, kind, document):
        return self.manager.submit(kind, document["name"], {"document": document})

    def calibrate(self, document, options):
        return self.manager.submit(
            "design_calibration", document["name"], {"document": document, **options}
        )

    def batch(self, documents):
        return self.manager.submit_many(
            [("design_simulation", d["name"], {"document": d}) for d in documents]
        )

    def snapshot(self):
        return self.manager.snapshot()

    def pause(self, paused):
        return self.manager.pause(paused)

    def cancel(self, identifier):
        return self.manager.cancel(identifier)

    def job(self, identifier):
        return self.manager.get(identifier)

    def _record(self, action):
        token = store.DATABASE_OVERRIDE.set(str(self.database))
        try:
            return action()
        finally:
            store.DATABASE_OVERRIDE.reset(token)

    def runs(self):
        return self._record(lambda: store.listing("design_run"))

    def result(self, identifier):
        return self._record(lambda: store.get(identifier, "design_run"))

    def optimization(self, identifier):
        return self._record(lambda: store.get(identifier, "design_optimization"))

    def calibration(self, identifier):
        return self._record(lambda: store.get(identifier, "design_calibration"))

    def health(self):
        return {"status": "ok", "mode": "embedded", "folder": str(self.folder)}

    def close(self):
        self.manager.stop()
        if self.manager.thread is None or not self.manager.thread.is_alive():
            self._lock_stream.close()
