"""3-D cell-centred linear acoustic finite volumes on a polygonal circular pipe.

This is a low-Mach, single-phase limit, not Navier-Stokes/VoF or structural FSI.
All 3 velocity components and face pressure fluxes are advanced on a 3-D mesh.
Riemann fluxes give a dissipative, independent water-hammer verification solver.
"""

import hashlib
import json
import math
import uuid
from pathlib import Path
import numpy as np
from .cases import wave_properties


def circular_mesh(length, diameter, nx, nr, nt=12):
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkUnstructuredGrid

    points = []
    per = 1 + nr * nt
    for x in np.linspace(0, length, nx + 1):
        points.append((x, 0, 0))
        for r in np.linspace(diameter / (2 * nr), diameter / 2, nr):
            for theta in np.arange(nt) * 2 * math.pi / nt:
                points.append((x, r * math.cos(theta), r * math.sin(theta)))

    def node(i, r, k):
        return i * per if r == 0 else i * per + 1 + (r - 1) * nt + k % nt

    cells = []
    types = []
    for i in range(nx):
        for r in range(nr):
            for k in range(nt):
                face = (
                    [node(i, r, k), node(i, r + 1, k), node(i, r + 1, k + 1)]
                    if r == 0
                    else [
                        node(i, r, k),
                        node(i, r + 1, k),
                        node(i, r + 1, k + 1),
                        node(i, r, k + 1),
                    ]
                )
                cells.append(face + [v + per for v in face])
                types.append(13 if r == 0 else 12)
    points = np.asarray(points)
    centers = np.array([points[ids].mean(axis=0) for ids in cells])
    face_map = {}
    for ci, (ids, typ) in enumerate(zip(cells, types)):
        n = len(ids) // 2
        faces = [ids[:n], ids[n:]] + [
            [ids[j], ids[(j + 1) % n], ids[(j + 1) % n + n], ids[j + n]] for j in range(n)
        ]
        for face in faces:
            key = tuple(sorted(face))
            if key in face_map:
                face_map[key][1] = ci
            else:
                face_map[key] = [ci, -1, face]
    owner = []
    neighbor = []
    normals = []
    areas = []
    fc = []
    for ci, cj, ids in face_map.values():
        poly = points[ids]
        center = poly.mean(axis=0)
        area_vector = np.cross(poly, np.roll(poly, -1, axis=0)).sum(axis=0) / 2
        if np.dot(area_vector, center - centers[ci]) < 0:
            area_vector = -area_vector
        area = np.linalg.norm(area_vector)
        owner.append(ci)
        neighbor.append(cj)
        normals.append(area_vector / area)
        areas.append(area)
        fc.append(center)
    owner = np.array(owner)
    neighbor = np.array(neighbor)
    normals = np.asarray(normals)
    areas = np.asarray(areas)
    fc = np.asarray(fc)
    volumes = np.zeros(len(cells))
    contrib = np.einsum("ij,ij->i", fc, normals) * areas / 3
    np.add.at(volumes, owner, contrib)
    inside = neighbor >= 0
    np.add.at(volumes, neighbor[inside], -contrib[inside])
    if (volumes <= 0).any():
        raise ValueError("声学网格包含非正体积")
    vtkpoints = vtkPoints()
    for row in points:
        vtkpoints.InsertNextPoint(*row)
    grid = vtkUnstructuredGrid()
    grid.SetPoints(vtkpoints)
    for ids, typ in zip(cells, types):
        grid.InsertNextCell(typ, len(ids), ids)
    return grid, centers, volumes, owner, neighbor, normals, areas, fc


def execute_acoustics(project, root, progress=None):
    from vtkmodules.util.numpy_support import numpy_to_vtk
    from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridWriter

    p = project
    if p.kind != "water_hammer" or not p.wave_frictionless_benchmark or any(p.gravity_ms2):
        raise ValueError(
            "线性声学有限体积模式限无摩阻、无重力直管基准；黏性/汽蚀请使用OpenFOAM模式"
        )
    wave = wave_properties(p)
    c = wave["wave_speed_ms"]
    rho = p.rho_kgm3
    bulk = wave["effective_bulk_pa"]
    if p.inlet_velocity_ms / c > 0.01 or wave["joukowsky_pa"] / bulk > 0.01:
        raise ValueError("线性声学近似仅适用于小马赫数/小密度扰动")
    case = Path(root).resolve() / uuid.uuid4().hex
    case.mkdir(parents=True)
    nx = max(10, round(p.pipe_cells * p.mesh_scale))
    nr = max(2, round(p.radial_cells * p.mesh_scale))
    grid, centers, vol, owner, neighbor, norm, area, fc = circular_mesh(
        p.pipe_length_m, p.pipe_diameter_m, nx, nr
    )
    inside = neighbor >= 0
    boundary = ~inside
    inlet = boundary & (fc[:, 0] < 1e-8)
    outlet = boundary & (fc[:, 0] > p.pipe_length_m - 1e-8)
    walls = boundary & ~(inlet | outlet)
    sum_area = np.zeros(len(vol))
    np.add.at(sum_area, owner, area)
    np.add.at(sum_area, neighbor[inside], area[inside])
    dt = min(p.delta_t_s, float(np.min(vol / (c * sum_area))) * 0.8)
    count = math.ceil(p.end_time_s / dt)
    dt = p.end_time_s / count
    if count > 500000 or count * len(vol) > 300_000_000:
        raise ValueError("三维声学基准规模过大，请缩短时窗或降低网格")
    pressure = np.zeros(len(vol))
    velocity = np.zeros((len(vol), 3))
    velocity[:, 0] = p.inlet_velocity_ms
    probes = [
        int(np.argmin(np.linalg.norm(centers - np.array((p.pipe_length_m * x, 0, 0)), axis=1)))
        for x in (0.25, 0.5, 0.75, 0.995)
    ]
    frames = []
    loads = []
    rows = []
    next_write = 0
    reference = p.inlet_absolute_pa
    manifest = {
        "schema_version": "flowstate-cfd-case-1",
        "solver": "FLOWSTATE linear acoustic finite volume",
        "project": p.model_dump(mode="json"),
        "compressible": True,
        "two_phase": False,
        "pressure_unit": "Pa absolute",
        "pressure_reference_pa": reference,
        "sector_angle_deg": 360,
        "wave_reference": wave,
        "assumptions": [
            "三维线性化可压缩Euler有限体积；忽略对流、黏性、汽蚀和结构位移。",
            "圆形截面用正多边形逼近；管壁柔度通过等效体积模量表示。",
            "仅用于小马赫数直管水锤及独立基准，不可代替任意管网三维Navier–Stokes。",
        ],
        "input_hashes": {},
        "probe_points_m": [centers[index].tolist() for index in probes],
        "validated_on_real_asset": False,
    }
    project_text = p.model_dump_json(indent=2)
    (case / "project.json").write_text(project_text, encoding="utf-8")
    manifest["input_hashes"] = {
        "project.json": hashlib.sha256((case / "project.json").read_bytes()).hexdigest()
    }
    (case / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for step in range(count + 1):
        t = step * dt
        rows.append({"seconds": t, "pressure_pa": (reference + pressure[probes]).tolist()})
        load = float(np.sum(pressure[owner[outlet]] * area[outlet]))
        loads.append(
            {
                "seconds": t,
                "force_n": [load, 0, 0],
                "moment_nm": [0, 0, 0],
                "pressure_force_n": [load, 0, 0],
                "viscous_force_n": [0, 0, 0],
            }
        )
        if t >= next_write - dt * 0.01 or step == count:
            for name, values in [("p", reference + pressure), ("U", velocity)]:
                arr = numpy_to_vtk(np.ascontiguousarray(values), deep=True)
                arr.SetName(name)
                grid.GetCellData().AddArray(arr)
            path = f"field_{len(frames):04d}.vtu"
            writer = vtkXMLUnstructuredGridWriter()
            writer.SetFileName(str(case / path))
            writer.SetInputData(grid)
            writer.SetDataModeToBinary()
            writer.Write()
            frames.append({"seconds": t, "path": path})
            next_write += p.write_interval_s
        if progress and step % max(1, count // 100) == 0:
            progress(0.05 + 0.85 * step / count, f"三维声学有限体积 · {t:.5g}/{p.end_time_s:g} s")
        if step == count:
            break
        pl = pressure[owner]
        ul = np.einsum("ij,ij->i", velocity[owner], norm)
        pr = pl.copy()
        ur = ul.copy()
        pr[inside] = pressure[neighbor[inside]]
        ur[inside] = np.einsum("ij,ij->i", velocity[neighbor[inside]], norm[inside])
        star_p = (pl + pr) / 2 - rho * c * (ur - ul) / 2
        star_u = (ul + ur) / 2 - (pr - pl) / (2 * rho * c)
        star_p[inlet] = 0
        star_u[inlet] = ul[inlet] + pl[inlet] / (rho * c)
        target = p.inlet_velocity_ms * np.clip(
            1 - (t + dt / 2 - p.close_start_s) / p.close_duration_s, 0, 1
        )
        star_u[outlet] = target
        star_p[outlet] = pl[outlet] + rho * c * (ul[outlet] - target)
        star_u[walls] = 0
        star_p[walls] = pl[walls] + rho * c * ul[walls]
        fp = bulk * star_u * area
        fu = star_p[:, None] * norm * area[:, None] / rho
        dp = np.bincount(owner, weights=fp, minlength=len(vol))
        du = np.column_stack(
            [np.bincount(owner, weights=fu[:, j], minlength=len(vol)) for j in range(3)]
        )
        dp -= np.bincount(neighbor[inside], weights=fp[inside], minlength=len(vol))
        for j in range(3):
            du[:, j] -= np.bincount(neighbor[inside], weights=fu[inside, j], minlength=len(vol))
        pressure -= dt * dp / vol
        velocity -= dt * du / vol[:, None]
        if not np.isfinite(pressure).all():
            raise ValueError("声学计算非有限值")
    first = [
        r["pressure_pa"][-1]
        for r in rows
        if r["seconds"] <= p.close_start_s + wave["round_trip_s"] * 0.9
    ]
    error = (max(first) - reference) / wave["joukowsky_pa"] - 1
    sample_times = np.asarray([r["seconds"] for r in rows])
    actual = np.asarray([r["pressure_pa"] for r in rows]) - reference
    travel = (p.pipe_length_m - centers[probes, 0]) / c
    analytic = wave["joukowsky_pa"] * np.clip(
        (sample_times[:, None] - travel[None, :] - p.close_start_s) / p.close_duration_s, 0, 1
    )
    valid = (
        sample_times[:, None]
        < (p.close_start_s + (p.pipe_length_m + centers[probes, 0]) / c)[None, :]
    )
    wavefront_rmse = float(
        np.sqrt(np.mean(((actual - analytic)[valid] / max(wave["joukowsky_pa"], 1e-20)) ** 2))
    )
    from backend.loads import analyze_loads

    result = {
        "case_directory": str(case),
        "manifest": manifest,
        "frames": frames,
        "loads": loads,
        "load_analysis": analyze_loads(loads),
        "pressure_probes": rows,
        "performance": [],
        "summary": {
            "cells": len(vol),
            "frames": len(frames),
            "maximum_velocity_ms": float(np.linalg.norm(velocity, axis=1).max()),
            "minimum_pressure_pa": float((pressure + reference).min()),
            "maximum_pressure_pa": float((pressure + reference).max()),
            "dt_s": dt,
            "steps": count,
        },
        "verification": {
            "real_asset_validated": False,
            "mesh_independence_established": False,
            "time_step_independence_established": False,
            "joukowsky": {
                "predicted_rise_pa": max(first) - reference,
                "reference_rise_pa": wave["joukowsky_pa"],
                "relative_error": error,
                "within_5_percent": abs(error) <= 0.05,
            },
            "wavefront_normalized_rmse": wavefront_rmse,
            "wavefront_reference": "finite linear valve closure, first incident wave, actual cell-centre probe locations",
            "single_phase_valid": all(min(r["pressure_pa"]) >= p.vapour_pressure_pa for r in rows),
        },
        "model": "3-D linear acoustic finite volume",
        "computation": "offline-local",
    }
    (case / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    return result
