"""Read solver-produced VTK fields; no reconstructed or randomly generated flow field."""

import json
import math
from pathlib import Path

import numpy as np

from backend.models import G


def read_vtk(path):
    from vtkmodules.vtkIOLegacy import vtkGenericDataObjectReader
    from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader, vtkXMLPolyDataReader

    path = Path(path)
    if path.suffix == ".vtu":
        reader = vtkXMLUnstructuredGridReader()
    elif path.suffix == ".vtp":
        reader = vtkXMLPolyDataReader()
    else:
        reader = vtkGenericDataObjectReader()
        reader.ReadAllFieldsOn()
        reader.ReadAllScalarsOn()
        reader.ReadAllVectorsOn()
    reader.SetFileName(str(path))
    reader.Update()
    output = reader.GetOutputDataObject(0)
    if output is None or output.GetNumberOfPoints() == 0:
        raise ValueError(f"没有可读取的 CFD 网格/场：{path}")
    if output.GetNumberOfPoints() > 5_000_000:
        raise ValueError("当前桌面读取最多500万个网格点；请使用切片导出或 ParaView")
    return output


def point_data(dataset):
    from vtkmodules.vtkFiltersCore import vtkCellDataToPointData

    filt = vtkCellDataToPointData()
    filt.SetInputData(dataset)
    filt.PassCellDataOn()
    filt.Update()
    output = filt.GetOutput()
    if output.GetPointData().GetArray("U") is not None:
        output.GetPointData().SetActiveVectors("U")
    return output


def discover_frames(case):
    """foamToVTK writes the actual time in the VTK title, independent of file index."""
    case = Path(case)
    import re

    mapping = {}
    log = case / "log.foamToVTK"
    current = None
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"Time:\s*([-+\d.eE]+)s?\s*$", line.strip())
            if match:
                current = float(match[1])
            match = re.search(r'Internal\s*:\s*"([^"\n]+)"', line)
            if match and current is not None:
                mapping[Path(match[1]).name] = current
    frames = []
    for path in sorted((case / "VTK").rglob("*.vtk")):
        relative = path.relative_to(case / "VTK")
        if len(relative.parts) != 1:
            continue
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.readline()
            title = stream.readline().strip()
        match = re.search(r"(?:time|Time)\s*[=:]?\s*([-+\d.eE]+)", title)
        if path.name in mapping:
            t = mapping[path.name]
        elif match:
            t = float(match[1])
        else:
            data = read_vtk(path)
            time_array = data.GetFieldData().GetArray("TIME") or data.GetFieldData().GetArray(
                "TimeValue"
            )
            if time_array is None:
                raise ValueError(f"VTK 文件缺少真实时刻，不能用文件编号代替：{path}")
            t = float(time_array.GetTuple1(0))
        frames.append({"seconds": t, "path": str(path.relative_to(case))})
    return sorted(frames, key=lambda f: f["seconds"])


def arrays(dataset, manifest):
    from vtkmodules.util.numpy_support import vtk_to_numpy

    data = point_data(dataset)
    points = vtk_to_numpy(data.GetPoints().GetData()).astype(float)
    values = {}
    pd = data.GetPointData()
    u = pd.GetArray("U")
    if u is not None:
        velocity = vtk_to_numpy(u).astype(float)
        values["speed_ms"] = np.linalg.norm(velocity, axis=1)
    else:
        velocity = np.zeros_like(points)
    p = pd.GetArray("p")
    if p is not None:
        pressure = vtk_to_numpy(p).astype(float)
        if not manifest["compressible"]:
            pressure = (
                pressure * manifest["project"]["rho_kgm3"] + manifest["pressure_reference_pa"]
            )
        values["pressure_pa"] = pressure
    alpha = pd.GetArray("alpha.vapour")
    if alpha is not None:
        values["vapour_fraction"] = vtk_to_numpy(alpha).astype(float)
    if not all(np.all(np.isfinite(v)) for v in [points, velocity, *values.values()]):
        raise ValueError("CFD 场中出现非有限值，不能作为有效物理结果显示")
    return data, points, velocity, values


def display_frame(
    case,
    frame,
    metric="speed_ms",
    plane_axis=None,
    plane_fraction=0.5,
    seeds=None,
    max_points=6000,
    streamlines=False,
):
    from vtkmodules.vtkFiltersGeometry import vtkDataSetSurfaceFilter
    from vtkmodules.vtkFiltersCore import vtkTriangleFilter, vtkCutter
    from vtkmodules.vtkCommonDataModel import vtkPlane
    from vtkmodules.util.numpy_support import vtk_to_numpy

    case = Path(case)
    manifest = json.loads((case / "manifest.json").read_text(encoding="utf-8"))
    data = read_vtk(case / frame["path"])
    data, points, velocity, values = arrays(data, manifest)
    if metric not in values:
        raise ValueError("该计算结果没有此物理场：" + metric)
    surface = vtkDataSetSurfaceFilter()
    surface.SetInputData(data)
    surface.Update()
    tri = vtkTriangleFilter()
    tri.SetInputConnection(surface.GetOutputPort())
    tri.Update()
    poly = tri.GetOutput()
    shell_points = vtk_to_numpy(poly.GetPoints().GetData()).astype(float)
    faces = []
    stride = max(1, math.ceil(poly.GetNumberOfCells() / 8000))
    for i in range(0, poly.GetNumberOfCells(), stride):
        cell = poly.GetCell(i)
        if cell.GetNumberOfPoints() == 3:
            faces.append([cell.GetPointId(j) for j in range(3)])
    if plane_axis is not None:
        axis = int(plane_axis)
        center = (points.min(axis=0) + points.max(axis=0)) / 2
        center[axis] = points[:, axis].min() + float(plane_fraction) * np.ptp(points[:, axis])
        normal = [0, 0, 0]
        normal[axis] = 1
        plane = vtkPlane()
        plane.SetOrigin(*center)
        plane.SetNormal(*normal)
        cut = vtkCutter()
        cut.SetCutFunction(plane)
        cut.SetInputData(data)
        cut.Update()
        if cut.GetOutput().GetNumberOfPoints():
            _, points, velocity, values = arrays(cut.GetOutput(), manifest)
    indexes = np.linspace(0, len(points) - 1, min(len(points), max_points), dtype=int)
    paths = trace_streamlines(data, seeds) if streamlines and seeds else []
    return {
        "seconds": frame["seconds"],
        "points": points[indexes],
        "velocity": velocity[indexes],
        "values": values[metric][indexes],
        "metric": metric,
        "surface_points": shell_points,
        "surface_triangles": np.asarray(faces, dtype=int),
        "streamlines": paths,
        "minimum": float(values[metric].min()),
        "maximum": float(values[metric].max()),
        "cell_count": data.GetNumberOfCells(),
        "point_count": data.GetNumberOfPoints(),
        "display_sampled": len(indexes) < len(points),
    }


def trace_streamlines(data, seeds):
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkPolyData
    from vtkmodules.vtkFiltersFlowPaths import vtkStreamTracer
    from vtkmodules.util.numpy_support import vtk_to_numpy

    seed_points = vtkPoints()
    for seed in seeds[:100]:
        seed_points.InsertNextPoint(*seed)
    seed = vtkPolyData()
    seed.SetPoints(seed_points)
    tracer = vtkStreamTracer()
    tracer.SetInputData(data)
    tracer.SetSourceData(seed)
    tracer.SetIntegratorTypeToRungeKutta45()
    tracer.SetIntegrationDirectionToBoth()
    bounds = data.GetBounds()
    size = math.sqrt(sum((bounds[2 * i + 1] - bounds[2 * i]) ** 2 for i in range(3)))
    tracer.SetMaximumPropagation(size * 8)
    tracer.SetInitialIntegrationStep(0.1)
    tracer.SetMaximumNumberOfSteps(3000)
    tracer.SetTerminalSpeed(1e-8)
    tracer.Update()
    out = tracer.GetOutput()
    if not out.GetNumberOfPoints():
        return []
    points = vtk_to_numpy(out.GetPoints().GetData())
    lines = []
    for i in range(out.GetNumberOfCells()):
        cell = out.GetCell(i)
        ids = [cell.GetPointId(j) for j in range(cell.GetNumberOfPoints())]
        lines.append(points[ids].copy())
    return lines


def patch_file(case, frame, patch):
    path = Path(case) / frame["path"]
    suffix = path.stem.rsplit("_", 1)[-1]
    candidates = list((Path(case) / "VTK" / patch).glob(f"*_{suffix}.vtk"))
    if len(candidates) != 1:
        raise ValueError(f"无法唯一找到 {patch} 在 {frame['seconds']} s 的边界场")
    return candidates[0]


def port_state(path, manifest):
    from vtkmodules.vtkFiltersCore import vtkTriangleFilter
    from vtkmodules.util.numpy_support import vtk_to_numpy

    ds = read_vtk(path)
    tri = vtkTriangleFilter()
    tri.SetInputData(ds)
    tri.Update()
    data = tri.GetOutput()
    _, xyz, velocity, values = arrays(data, manifest)
    if "pressure_pa" not in values:
        raise ValueError("边界场缺少压力，无法计算扬程")
    cell_u = data.GetCellData().GetArray("U")
    cell_p = data.GetCellData().GetArray("p")
    cell_u = vtk_to_numpy(cell_u) if cell_u is not None else None
    cell_p = vtk_to_numpy(cell_p) if cell_p is not None else None
    fluxes, heads, pressures, zs, kinetic = [], [], [], [], []
    rho = manifest["project"]["rho_kgm3"]
    gravity = np.asarray(manifest["project"].get("gravity_ms2", [0, 0, 0]))
    for i in range(data.GetNumberOfCells()):
        cell = data.GetCell(i)
        if cell.GetNumberOfPoints() != 3:
            continue
        ids = [cell.GetPointId(j) for j in range(3)]
        a, b, c = xyz[ids]
        area_vector = np.cross(b - a, c - a) / 2
        u = cell_u[i] if cell_u is not None else velocity[ids].mean(axis=0)
        q = float(np.dot(u, area_vector))
        if cell_p is not None:
            pressure = float(cell_p[i])
            if not manifest["compressible"]:
                pressure = pressure * rho + manifest["pressure_reference_pa"]
        else:
            pressure = float(values["pressure_pa"][ids].mean())
        center = xyz[ids].mean(axis=0)
        potential = -float(np.dot(gravity, center)) / G
        speed2 = float(np.dot(u, u))
        fluxes.append(q)
        heads.append(pressure / (rho * G) + speed2 / (2 * G) + potential)
        pressures.append(pressure)
        zs.append(float(center[2]))
        kinetic.append(speed2 / (2 * G))
    q = sum(fluxes)
    if abs(q) < 1e-12:
        raise ValueError("端口净流量接近零，不能定义流量加权扬程")
    weights = np.asarray(fluxes) / q
    return {
        "outward_flow_m3s": q,
        "total_head_m": float(np.dot(weights, heads)),
        "pressure_pa": float(np.dot(weights, pressures)),
        "elevation_m": float(np.dot(weights, zs)),
        "velocity_head_m": float(np.dot(weights, kinetic)),
    }


def performance_series(case, frames):
    case = Path(case)
    manifest = json.loads((case / "manifest.json").read_text(encoding="utf-8"))
    native = native_performance(case, manifest)
    if native:
        return native
    rows = []
    for frame in frames:
        if frame["seconds"] == 0:
            continue
        try:
            inlet = port_state(patch_file(case, frame, "inlet"), manifest)
            outlet = port_state(patch_file(case, frame, "outlet"), manifest)
        except ValueError:
            continue
        rho = manifest["project"]["rho_kgm3"]
        pv = manifest["project"]["vapour_pressure_pa"]
        datum = (
            -float(
                np.dot(
                    manifest["project"].get("gravity_ms2", [0, 0, 0]),
                    manifest["project"]["rotor_origin"],
                )
            )
            / G
        )
        rows.append(
            {
                "seconds": frame["seconds"],
                "flow_m3h": abs(outlet["outward_flow_m3s"])
                * 3600
                * 360
                / manifest["sector_angle_deg"],
                "head_m": outlet["total_head_m"] - inlet["total_head_m"],
                "npsha_m": inlet["total_head_m"] - pv / (rho * G) - datum,
                "inlet": inlet,
                "outlet": outlet,
            }
        )
    return rows


def native_performance(case, manifest):
    from backend.loads import numeric_rows

    data = {}
    for name in ("portAverage_inlet", "portAverage_outlet", "inletFlowRate", "outletFlowRate"):
        rows = {}
        for path in sorted((Path(case) / "postProcessing" / name).glob("*/surfaceFieldValue.dat")):
            for row in numeric_rows(path):
                rows[row[0]] = row[1:]
        data[name] = rows
    shared = set.intersection(*(set(v) for v in data.values()))
    rho = manifest["project"]["rho_kgm3"]
    pv = manifest["project"]["vapour_pressure_pa"]
    datum = (
        -float(
            np.dot(
                manifest["project"].get("gravity_ms2", [0, 0, 0]),
                manifest["project"]["rotor_origin"],
            )
        )
        / G
    )
    result = []
    for t in sorted(shared):
        if t <= 0:
            continue
        states = {}
        for port in ("inlet", "outlet"):
            row = data["portAverage_" + port][t]
            q = data[port + "FlowRate"][t][0]
            if manifest["compressible"]:
                if len(row) != 4:
                    raise ValueError("端口平均字段数量不匹配")
                pressure, modified, speed2, alpha = row
                head = modified / (rho * G) + speed2 / (2 * G)
            else:
                if len(row) != 2:
                    raise ValueError("端口平均字段数量不匹配")
                modified, speed2 = row
                alpha = 0
                pressure = modified * rho + manifest["pressure_reference_pa"]
                head = pressure / (rho * G) + speed2 / (2 * G)
            states[port] = {
                "outward_flow_m3s": q,
                "total_head_m": head,
                "pressure_pa": pressure,
                "velocity_head_m": speed2 / (2 * G),
                "vapour_fraction": alpha,
            }
        if abs(states["outlet"]["outward_flow_m3s"]) < 1e-12:
            continue
        result.append(
            {
                "seconds": t,
                "flow_m3h": states["outlet"]["outward_flow_m3s"]
                * 3600
                * 360
                / manifest["sector_angle_deg"],
                "head_m": states["outlet"]["total_head_m"] - states["inlet"]["total_head_m"],
                "npsha_m": states["inlet"]["total_head_m"] - pv / (rho * G) - datum,
                **states,
            }
        )
    return result
