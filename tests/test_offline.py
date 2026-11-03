import socket
import time

from backend.models import default_document
from desktop.controller import Controller


def test_computation_does_not_open_network_connections(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline computation attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    controller = Controller(tmp_path / "offline")
    try:
        job = controller.submit("design_simulation", default_document().model_dump(mode="json"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = controller.job(job["id"])
            if value["state"] in ("failed", "succeeded"):
                break
            time.sleep(0.02)
        assert value["state"] == "succeeded", value
        assert controller.result(value["result"]["design_run_id"])["meta"]["converged"]
    finally:
        controller.close()
