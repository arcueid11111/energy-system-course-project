"""DXF centreline import with 3D endpoint topology; no solid-to-pipe inference."""

import hashlib
import math
from collections import Counter, defaultdict
from pathlib import Path

from .models import DesignLink, DesignNode, NetworkDocument

UNIT_SCALES = {
    1: 0.0254,
    2: 0.3048,
    4: 0.001,
    5: 0.01,
    6: 1.0,
    7: 1000.0,
    8: 2.54e-8,
    9: 2.54e-5,
    10: 0.9144,
    14: 0.1,
}
SUPPORTED = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "SPLINE", "ELLIPSE"}


def inspect_dxf(path):
    import ezdxf

    doc = ezdxf.readfile(path)
    entities = list(doc.modelspace())
    return {
        "units_code": int(doc.header.get("$INSUNITS", 0)),
        "suggested_scale": UNIT_SCALES.get(int(doc.header.get("$INSUNITS", 0))),
        "layers": sorted({e.dxf.layer for e in entities}),
        "entity_counts": dict(Counter(e.dxftype() for e in entities)),
        "notice": "请选择管道中心线图层；文字、尺寸、三维实体和外部参照不会自动识别为水力设备。",
    }


def import_dxf(
    path,
    *,
    scale=None,
    tolerance_m=0.01,
    curve_tolerance_m=0.01,
    layers=None,
    z_to_elevation=True,
    connect_endpoint_to_segment=True,
    diameter_m=0.05,
):
    import ezdxf
    from ezdxf import path as dxfpath

    path = Path(path)
    if path.suffix.lower() != ".dxf":
        raise ValueError("当前直接支持 DXF；DWG 请先在 CAD 软件中另存为 DXF")
    if path.stat().st_size > 100_000_000:
        raise ValueError("DXF 超过 100 MB，请仅导出管网中心线图层")
    if tolerance_m <= 0 or curve_tolerance_m <= 0:
        raise ValueError("连接容差和曲线离散容差必须大于零")
    doc = ezdxf.readfile(path)
    unitcode = int(doc.header.get("$INSUNITS", 0))
    if scale is None:
        scale = UNIT_SCALES.get(unitcode)
    if scale is None or not math.isfinite(scale) or scale <= 0:
        raise ValueError("CAD 未声明可识别单位，请明确选择单位或输入每图形单位对应米数")
    skipped = Counter()
    segments = []
    notes = []
    selected = None if layers is None else set(layers)

    def expand(entities, inherited_layer=None, depth=0):
        if depth > 12:
            raise ValueError("CAD 图块嵌套超过 12 层，请先在 CAD 中简化")
        for e in entities:
            layer = e.dxf.layer
            if layer == "0" and inherited_layer:
                layer = inherited_layer
            if e.dxftype() == "INSERT":
                if selected is not None and layer not in selected:
                    continue
                try:
                    yield from expand(e.virtual_entities(), layer, depth + 1)
                except (NotImplementedError, ValueError):
                    skipped["INSERT_UNSUPPORTED"] += 1
                continue
            if selected is None or layer in selected:
                yield e, layer

    for e, layer in expand(doc.modelspace()):
        kind = e.dxftype()
        if kind not in SUPPORTED:
            skipped[kind] += 1
            continue
        if kind == "POLYLINE" and (e.is_poly_face_mesh or e.is_polygon_mesh):
            skipped["MESH"] += 1
            continue
        try:
            if kind == "LINE":
                verts = [e.dxf.start, e.dxf.end]
            else:
                verts = list(
                    dxfpath.make_path(e).flattening(distance=curve_tolerance_m / scale, segments=4)
                )
            verts = [tuple(float(v) * scale for v in p) for p in verts]
        except (ValueError, TypeError, NotImplementedError) as exc:
            skipped[kind + "_UNSUPPORTED"] += 1
            notes.append(str(exc)[:100])
            continue
        for a, b in zip(verts, verts[1:]):
            if math.dist(a, b) > 1e-8:
                segments.append((a, b, layer))
        if len(segments) > 1900:
            raise ValueError("中心线离散超过 1900 段，请增大曲线离散容差或缩小图层范围")
    if not segments:
        raise ValueError("所选图层中没有可用中心线；请导出 LINE、2D/3D POLYLINE、ARC 或 SPLINE")
    # Only actual endpoints are welded. XY crossings at different Z never connect.
    nodes = []
    cells = defaultdict(list)

    def index_for(point):
        cell = tuple(math.floor(v / tolerance_m) for v in point)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for i in cells[(cell[0] + dx, cell[1] + dy, cell[2] + dz)]:
                        if math.dist(nodes[i], point) <= tolerance_m:
                            return i
        idx = len(nodes)
        nodes.append(point)
        cells[cell].append(idx)
        return idx

    raw = []
    for a, b, layer in segments:
        ia, ib = index_for(a), index_for(b)
        if ia != ib:
            raw.append((ia, ib, layer))
    if len(nodes) > 1200:
        raise ValueError("导入超过 1200 个节点，请简化中心线")
    split = []
    for a, b, layer in raw:
        pa, pb = nodes[a], nodes[b]
        vector = [y - x for x, y in zip(pa, pb)]
        length2 = sum(v * v for v in vector)
        cuts = [(0, a), (1, b)]
        if connect_endpoint_to_segment:
            for i, p in enumerate(nodes):
                if i in (a, b):
                    continue
                u = sum((p[j] - pa[j]) * vector[j] for j in range(3)) / length2
                if (
                    1e-8 < u < 1 - 1e-8
                    and math.dist(p, tuple(pa[j] + u * vector[j] for j in range(3))) <= tolerance_m
                ):
                    cuts.append((u, i))
        cuts.sort()
        for (_, i), (_, j) in zip(cuts, cuts[1:]):
            if i != j:
                split.append((i, j, layer))
    if len(split) > 2000:
        raise ValueError("分割后的管段超过 2000 条，请简化导入")
    unique = {}
    duplicates = 0
    for a, b, layer in split:
        key = (*sorted((a, b)), layer)
        if key in unique:
            duplicates += 1
        else:
            unique[key] = (a, b, layer)
    model_nodes = [
        DesignNode(
            id=f"N{i + 1}",
            x=p[0],
            y=p[1],
            z=p[2],
            elevation_m=p[2] if z_to_elevation else 0,
            note="CAD 几何节点；水力边界待确认",
        )
        for i, p in enumerate(nodes)
    ]
    links = [
        DesignLink(
            id=f"L{i + 1}",
            start=f"N{a + 1}",
            end=f"N{b + 1}",
            length_m=math.dist(nodes[a], nodes[b]),
            diameter_m=diameter_m,
            geometry=[nodes[a], nodes[b]],
            source="cad_geometry",
            note=f"DXF 图层 {layer}；管径和粗糙度为待确认默认值",
        )
        for i, (a, b, layer) in enumerate(unique.values())
    ]
    meta = {
        "file_name": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "insunits_code": unitcode,
        "metres_per_unit": scale,
        "endpoint_tolerance_m": tolerance_m,
        "curve_tolerance_m": curve_tolerance_m,
        "selected_layers": sorted(selected) if selected is not None else None,
        "z_to_elevation": z_to_elevation,
        "connect_endpoint_to_segment": connect_endpoint_to_segment,
        "skipped_entities": dict(skipped),
        "duplicate_segments_removed": duplicates,
        "notes": notes[:20],
        "limitations": "仅导入中心线几何。不会从文字、颜色或实体自动判定管径、水源、泵和阀门；交叉线内部交点不自动连通，端点落在线段上的三维 T 接可接通。",
    }
    return NetworkDocument(
        name=path.stem[:100] or "CAD 管网",
        nodes=model_nodes,
        links=links,
        cad_metadata=meta,
        note="CAD 中心线导入草稿：请明确水源、需水量、泵曲线、阀门与管径后再求解。",
    )
