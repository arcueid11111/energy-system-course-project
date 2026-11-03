import sys
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.models import default_document
from backend.physics import simulate_design
import ezdxf

folder = ROOT / "examples"
folder.mkdir(exist_ok=True)
(folder / "editable_network.json").write_text(
    default_document().model_dump_json(indent=2), encoding="utf-8"
)
doc = ezdxf.new("R2010")
doc.header["$INSUNITS"] = 4
doc.layers.new("PIPE_CENTERLINE")
doc.layers.new("ANNOTATION")
m = doc.modelspace()
for a, b in [
    ((0, 0, 0), (10000, 0, 0)),
    ((10000, 0, 0), (20000, 0, 0)),
    ((20000, 0, 0), (30000, 10000, 5000)),
    ((20000, 0, 0), (30000, -10000, 8000)),
    ((30000, 10000, 5000), (30000, -10000, 8000)),
]:
    m.add_line(a, b, dxfattribs={"layer": "PIPE_CENTERLINE"})
m.add_text(
    "3D pipe centreline demo; unit mm; hydraulic boundaries not assigned",
    dxfattribs={"layer": "ANNOTATION"},
)
doc.saveas(folder / "cad_3d_centerline_demo.dxf")
truth = default_document()
for point in truth.links[1].curve:
    point.head_m *= 0.9
    point.shaft_kw *= 0.9
result = simulate_design(truth)
with (folder / "custom_network_observations.csv").open("w", encoding="utf-8", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["timestamp", "pressure_m", "flow_m3h", "power_kw"])
    for p in result["points"]:
        writer.writerow(
            [
                (
                    datetime(2026, 11, 3, tzinfo=timezone.utc) + timedelta(seconds=p["seconds"])
                ).isoformat(),
                p["nodes"]["J3"]["pressure_m"],
                p["links"]["P1"]["flow_m3h"],
                p["power_kw"],
            ]
        )
(folder / "custom_network_observations.meta.json").write_text(
    json.dumps(
        {
            "source": "synthetic",
            "purpose": "同求解器无噪声单泵曲线倍率恢复，不能作为实测验证",
            "measurement_node": "J3",
            "measurement_link": "P1",
            "calibration_pump": "P1",
            "truth_factor": 0.9,
            "baseline_project": "editable_network.json",
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
print("已生成可编辑管网 JSON 和毫米单位的三维 DXF 中心线样例。")
