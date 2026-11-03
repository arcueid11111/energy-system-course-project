import math

import pytest

from backend.models import DesignLink, DesignNode, default_document
from backend.physics import G, fluid, simulate_design


def test_pipe_headloss_matches_independent_colebrook_calculation():
    doc = default_document()
    run = simulate_design(doc)
    pipe = next(link for link in doc.links if link.id == "L3")
    state = run["points"][0]["links"]["L3"]
    velocity = state["flow_m3h"] / 3600 / (math.pi * pipe.diameter_m**2 / 4)
    _, nu, _ = fluid(doc.settings.temperature_c)
    reynolds = velocity * pipe.diameter_m / nu
    friction = 0.025
    for _ in range(30):
        friction = (
            -2
            * math.log10(
                pipe.roughness_m / (3.7 * pipe.diameter_m) + 2.51 / (reynolds * math.sqrt(friction))
            )
        ) ** -2
    expected = (
        (friction * pipe.length_m / pipe.diameter_m + pipe.minor_loss) * velocity**2 / (2 * G)
    )
    assert state["headloss_m"] == pytest.approx(expected, rel=0.025)


def test_stopped_pumps_do_not_force_supply_or_consume_pump_power():
    doc = default_document()
    for link in doc.links:
        if link.kind == "pump":
            link.enabled = False
    run = simulate_design(doc)
    assert run["summary"]["energy_kwh"] == 0
    assert run["summary"]["unmet_m3"] > 10


def test_operating_pump_keeps_shutoff_losses_and_interval_energy():
    doc = default_document()
    for link in doc.links:
        if link.id in ("L2", "L3", "L4"):
            link.enabled = False
    run = simulate_design(doc)
    point = run["points"][0]
    assert point["links"]["P1"]["flow_m3h"] < 0.001
    assert point["power_kw"] > 0.8
    assert run["summary"]["energy_kwh"] == pytest.approx(point["power_kw"], rel=1e-5)
    assert len(run["points"]) == 13


def test_tank_volume_change_matches_integrated_pipe_flow():
    doc = default_document()
    doc.nodes.append(
        DesignNode(
            id="T",
            kind="tank",
            x=40,
            y=25,
            elevation_m=5,
            tank_level_m=8,
            tank_min_m=3,
            tank_max_m=14,
            tank_diameter_m=3,
        )
    )
    doc.links.append(DesignLink(id="STORAGE", start="J2", end="T", length_m=40, diameter_m=0.04))
    run = simulate_design(doc)
    volume_change = (run["points"][-1]["nodes"]["T"]["tank_level_m"] - 8) * math.pi * 3**2 / 4
    integrated = sum(
        p["links"]["STORAGE"]["flow_m3h"] * doc.settings.step_seconds / 3600
        for p in run["points"][:-1]
    )
    assert integrated == pytest.approx(volume_change, abs=0.02)
