import numpy as np
import pytest

from backend.models import DesignLink, DesignNode, NetworkDocument, default_document
from backend.physics import export_design_inp, simulate_design, validate_topology


def test_arbitrary_topology_and_snapshots():
    doc = default_document()
    doc.nodes.append(DesignNode(id="NEW", x=78, y=10, z=4, elevation_m=4, demand_m3h=0.5))
    doc.links.append(DesignLink(id="EXT", start="J4", end="NEW", length_m=40))
    r = simulate_design(doc)
    assert "NEW" in r["points"][0]["nodes"]
    assert "EXT" in r["points"][0]["links"]
    assert r["summary"]["max_mass_residual_m3h"] < 0.001
    assert r["document"]["nodes"][-1]["z"] == 4
    pump = r["points"][0]["links"]["P1"]
    expected = np.interp(
        pump["flow_m3h"],
        [p.flow_m3h for p in doc.links[1].curve],
        [p.head_m for p in doc.links[1].curve],
    )
    assert pump["head_m"] == pytest.approx(expected, abs=0.015)
    assert "D-W" in export_design_inp(doc)


def test_hydraulic_elevation_is_explicit_not_view_coordinate():
    doc = NetworkDocument(
        nodes=[
            DesignNode(id="R", kind="reservoir", source_head_m=30),
            DesignNode(id="J", x=50, z=100, elevation_m=5, demand_m3h=2),
        ],
        links=[DesignLink(id="L", start="R", end="J", length_m=100)],
    )
    r = simulate_design(doc)
    assert r["points"][0]["nodes"]["J"]["pressure_m"] > 20
    doc.nodes[1].elevation_m = 100
    r2 = simulate_design(doc)
    assert r2["summary"]["unmet_m3"] > 1.9


def test_draft_topology_validation():
    doc = NetworkDocument(
        nodes=[DesignNode(id="A"), DesignNode(id="B")],
        links=[DesignLink(id="L", start="A", end="B")],
    )
    assert any("缺少水源" in e for e in validate_topology(doc))
    doc.nodes[0].kind = "reservoir"
    doc.nodes[0].source_head_m = 25
    doc.nodes.append(DesignNode(id="ISOLATED"))
    assert any("ISOLATED" in e for e in validate_topology(doc))
    with pytest.raises(ValueError):
        simulate_design(doc)


def test_design_events_preserve_speed_and_enable_state():
    doc = default_document()
    doc.links[1].speed_ratio = 0.8
    from backend.models import DesignEvent

    doc.settings.events = [
        DesignEvent(hour=0.25, target="P1", kind="enabled", value=0),
        DesignEvent(hour=0.5, target="P1", kind="enabled", value=1),
        DesignEvent(hour=0.25, target="P2", kind="speed", value=0.7),
    ]
    r = simulate_design(doc)
    assert r["points"][3]["links"]["P1"]["active"] is False
    assert r["points"][6]["links"]["P1"]["rpm"] == pytest.approx(2320)
    assert all(not p["links"]["P2"]["active"] for p in r["points"])


def test_geometry_connectivity_and_duplicate_ids():
    doc = default_document()
    doc.nodes.append(doc.nodes[0].model_copy())
    assert any("重复" in e for e in validate_topology(doc))
