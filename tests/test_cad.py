import math

import ezdxf
import pytest

from backend.cad import import_dxf, inspect_dxf
from backend.physics import validate_topology


def test_3d_units_t_junction_and_overpass(tmp_path):
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    m = doc.modelspace()
    m.add_line((0, 0, 1000), (10000, 0, 1000), dxfattribs={"layer": "PIPE"})
    m.add_line((5000, 0, 1000), (5000, 5000, 3000), dxfattribs={"layer": "PIPE"})
    m.add_line((5000, -5000, 4000), (5000, 5000, 4000), dxfattribs={"layer": "PIPE"})
    m.add_line((0, 0, 1000), (10000, 0, 1000), dxfattribs={"layer": "PIPE"})
    m.add_text("not a pipe", dxfattribs={"layer": "TEXT"})
    path = tmp_path / "3d.dxf"
    doc.saveas(path)
    assert inspect_dxf(path)["suggested_scale"] == 0.001
    imported = import_dxf(path, layers=["PIPE"], tolerance_m=0.001)
    assert max(n.z for n in imported.nodes) == 4
    assert all(n.elevation_m == n.z for n in imported.nodes)
    assert len(imported.links) == 4  # main split at T + T branch + overpass
    assert imported.cad_metadata["duplicate_segments_removed"] > 0
    at_tee = next(n.id for n in imported.nodes if n.x == 5 and n.y == 0 and n.z == 1)
    assert sum(at_tee in (l.start, l.end) for l in imported.links) == 3
    # No artificial junction is created at XY crossing with Z=4.
    assert not any(n.x == 5 and n.y == 0 and n.z == 4 for n in imported.nodes)
    assert any("缺少水源" in e for e in validate_topology(imported))


def test_nested_block_transform_and_polyline_height(tmp_path):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 6
    block = doc.blocks.new("PIPEBLOCK")
    block.add_line((0, 0, 0), (2, 0, 0))
    doc.modelspace().add_blockref(
        "PIPEBLOCK", (10, 20, 3), dxfattribs={"layer": "PIPE", "rotation": 90}
    )
    doc.modelspace().add_lwpolyline(
        [(0, 0), (2, 0), (2, 2)], dxfattribs={"layer": "PIPE", "elevation": 5}
    )
    path = tmp_path / "block.dxf"
    doc.saveas(path)
    imported = import_dxf(path, layers=["PIPE"])
    positions = [(round(n.x, 6), round(n.y, 6), round(n.z, 6)) for n in imported.nodes]
    assert (10, 20, 3) in positions and (10, 22, 3) in positions
    assert (2, 2, 5) in positions


def test_unknown_units_are_not_silently_inferred(tmp_path):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 0
    doc.modelspace().add_line((0, 0, 0), (1000, 0, 0))
    path = tmp_path / "unknown.dxf"
    doc.saveas(path)
    with pytest.raises(ValueError, match="单位"):
        import_dxf(path)
    imported = import_dxf(path, scale=0.001, z_to_elevation=False)
    assert imported.links[0].length_m == pytest.approx(1)


def test_arc_is_approximated_and_unsupported_entities_reported(tmp_path):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 6
    m = doc.modelspace()
    m.add_arc((0, 0, 2), 5, 0, 90, dxfattribs={"layer": "PIPE"})
    m.add_text("DN50")
    path = tmp_path / "arc.dxf"
    doc.saveas(path)
    imported = import_dxf(path, curve_tolerance_m=0.005)
    assert sum(l.length_m for l in imported.links) == pytest.approx(math.pi * 5 / 2, rel=0.003)
    assert imported.cad_metadata["skipped_entities"]["TEXT"] == 1
    assert all(n.z == pytest.approx(2) for n in imported.nodes)
