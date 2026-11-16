import pytest

from backend.diagnostics import diagnose, npsh_required, pump_curve_bep
from backend.models import default_document
from backend.physics import simulate_design


def test_default_network_has_no_findings_and_sane_metrics():
    out = diagnose(simulate_design(default_document()))
    assert out["findings"] == []
    m = out["metrics"]
    assert 0.05 < m["specific_energy_kwh_m3"] < 0.5
    assert 0.3 < m["wire_to_water_efficiency"] < 0.8


def test_undersized_pipes_report_pressure_velocity_and_supply_problems():
    doc = default_document()
    for link in doc.links:
        if link.kind == "pipe":
            link.diameter_m = 0.025
    out = diagnose(simulate_design(doc))
    categories = {f["category"] for f in out["findings"]}
    assert {"低压", "缺水", "流速", "泵工况"} <= categories
    severities = [f["severity"] for f in out["findings"]]
    assert severities == sorted(severities, key=["error", "warning", "info"].index)
    assert all(f["target"] for f in out["findings"])


def test_motor_overload_and_npsh_scaling():
    doc = default_document()
    pump = next(l for l in doc.links if l.id == "P1")
    pump.motor_rated_kw = 0.5
    out = diagnose(simulate_design(doc))
    assert any(f["category"] == "电机" and f["target"] == "P1" for f in out["findings"])
    q, h, eta = pump_curve_bep(pump)
    assert q == 15 and 0.6 < eta < 0.8
    assert npsh_required(pump, 10, 1.0) == pytest.approx(1.8)
    assert npsh_required(pump, 5, 0.5) == pytest.approx(1.8 * 0.25)


def test_tank_finding_keeps_lowest_level():
    from backend.models import DesignNode

    doc = default_document()
    doc.nodes.append(DesignNode(id="T1", kind="tank"))
    result = {
        "document": doc.model_dump(mode="json"),
        "points": [
            {"hour": i / 12, "nodes": {"T1": {"tank_level_m": level}}, "links": {}}
            for i, level in enumerate([0.04, 0.01, 0.03])
        ],
    }
    finding = diagnose(result)["findings"][0]
    assert finding["value"] == 0.01
    assert "0.01 m" in finding["message"]
    assert finding["count"] == 3 and finding["first_hour"] == 0
