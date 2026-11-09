"""Queued local CFD, pressure scans and explicit numerical verification studies."""

import hashlib
import json
import uuid
from pathlib import Path

import numpy as np

from backend.cavitation import CavitationMap, CavitationPoint, HeadBreakdown
from backend.loads import analyze_loads, numeric_rows, read_forces
from .cases import prepare_case
from .fields import arrays, discover_frames, performance_series, read_vtk
from .runtime import LocalSolver
from .schemas import CFDProject


def read_pressure_probes(case, manifest):
    result = {}
    for path in sorted((Path(case) / "postProcessing/pressureProbes").glob("*/p")):
        for row in numeric_rows(path):
            pressure = row[1:]
            if not manifest["compressible"]:
                pressure = [
                    p * manifest["project"]["rho_kgm3"] + manifest["pressure_reference_pa"]
                    for p in pressure
                ]
            result[row[0]] = {"seconds": row[0], "pressure_pa": pressure}
    return [result[t] for t in sorted(result)]


def collect_result(case, solver_run=None):
    case = Path(case).resolve()
    manifest = json.loads((case / "manifest.json").read_text(encoding="utf-8"))
    frames = discover_frames(case)
    if len(frames) < 2 or frames[-1]["seconds"] < manifest["project"]["end_time_s"] * (1 - 1e-5):
        raise ValueError("CFD 时轴未完整到达结束时刻，不能记为成功结果")
    final = read_vtk(case / frames[-1]["path"])
    _, _, _, values = arrays(final, manifest)
    if not {"speed_ms", "pressure_pa"} <= values.keys():
        raise ValueError("CFD 结果缺少速度或压力场")
    from vtkmodules.util.numpy_support import vtk_to_numpy

    cell_values = {}
    for name, key in (("p", "pressure_pa"), ("U", "speed_ms"), ("alpha.vapour", "vapour_fraction")):
        array = final.GetCellData().GetArray(name)
        if array is not None:
            raw = vtk_to_numpy(array).astype(float)
            if not np.isfinite(raw).all():
                raise ValueError("CFD单元数据含非有限值")
            if name == "U":
                raw = np.linalg.norm(raw, axis=1)
            if name == "p" and not manifest["compressible"]:
                raw = raw * manifest["project"]["rho_kgm3"] + manifest["pressure_reference_pa"]
            cell_values[key] = raw
    values = {**values, **cell_values}
    if manifest["compressible"]:
        if "vapour_fraction" not in values:
            raise ValueError("两相算例没有有效蒸汽体积分数字段")
        if values["vapour_fraction"].min() < -1e-6 or values["vapour_fraction"].max() > 1 + 1e-6:
            raise ValueError("汽相体积分数超出物理范围")
    loads = read_forces(case)
    project = manifest["project"]
    analysis = analyze_loads(
        loads, rpm=project["rpm"], blade_count=12 if manifest["sector_angle_deg"] == 30 else None
    )
    probes = read_pressure_probes(case, manifest)
    performance = performance_series(case, frames)
    summary = {
        "cells": final.GetNumberOfCells(),
        "frames": len(frames),
        "maximum_velocity_ms": float(values["speed_ms"].max()),
        "minimum_pressure_pa": float(values["pressure_pa"].min()),
        "maximum_pressure_pa": float(values["pressure_pa"].max()),
        "maximum_vapour_fraction": float(values.get("vapour_fraction", np.zeros(1)).max()),
    }
    if len(loads) > 1:
        steps = np.diff([row["seconds"] for row in loads])
        summary["time_steps_s"] = {
            "minimum": float(steps.min()),
            "median": float(np.median(steps)),
            "maximum": float(steps.max()),
        }
    verification = {
        "real_asset_validated": False,
        "absolute_pressure_positive": bool(values["pressure_pa"].min() > 0),
        "mesh_independence_established": False,
        "time_step_independence_established": False,
    }
    if manifest["wave_reference"] and probes:
        initial = probes[0]["pressure_pa"][-1]
        first = [
            row["pressure_pa"][-1]
            for row in probes
            if row["seconds"]
            <= project["close_start_s"] + manifest["wave_reference"]["round_trip_s"] * 0.9
        ]
        peak = max(first)
        expected = manifest["wave_reference"]["joukowsky_pa"]
        verification["joukowsky"] = {
            "predicted_rise_pa": peak - initial,
            "reference_rise_pa": expected,
            "relative_error": (peak - initial) / expected - 1,
            "notice": "仅在快速关阀、首次反射前、近似无摩阻单相条件下可与Joukowsky直接比较",
        }
    result = {
        "case_directory": str(case),
        "manifest": manifest,
        "frames": frames,
        "loads": loads,
        "load_analysis": analysis,
        "pressure_probes": probes,
        "performance": performance,
        "summary": summary,
        "verification": verification,
        "solver_run": solver_run,
        "model": "3-D finite-volume OpenFOAM 14",
        "computation": "offline-local",
    }
    (case / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    return result


def execute_cfd(project: CFDProject, root, progress=None, overrides=None):
    project = CFDProject.model_validate(project.model_dump())
    if project.kind == "water_hammer" and project.wave_solver == "acoustic_fv":
        if overrides:
            raise ValueError("声学基准不接受旋转耦合边界")
        from .acoustics import execute_acoustics

        return execute_acoustics(project, root, progress)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    case = root / uuid.uuid4().hex
    solver = LocalSolver(project.runtime)
    if progress:
        progress(0.01, "检查本机 OpenFOAM 14")
    solver_info = solver.probe(root / "solver-probe")
    if progress:
        progress(0.03, "生成三维流体域与边界条件")
    manifest = prepare_case(project, case)
    if overrides:
        from .coupling import apply_waveforms

        apply_waveforms(case, manifest, overrides)
    for i, (program, args) in enumerate(manifest["steps"]):
        solver.run(program, args, case, progress, start=0.04, span=0.06)
    solver.run("checkMesh", ["-allTopology", "-meshQuality"], case, progress, start=0.10, span=0.02)
    mesh_log = (case / "log.checkMesh").read_text(encoding="utf-8", errors="replace")
    if "Mesh OK." not in mesh_log:
        raise ValueError("网格质量检查没有通过；请查看 log.checkMesh，不能接受该计算")
    run = solver.run(
        "foamRun", [], case, progress, start=0.12, span=0.72, end_time=project.end_time_s
    )
    if run["last_time_s"] < project.end_time_s * (1 - 1e-5):
        raise ValueError("求解器提前退出，结果不完整")
    log = (case / "log.foamRun").read_text(encoding="utf-8", errors="replace")
    import re

    if re.search(r"\b(?:nan|inf)\b", log, re.IGNORECASE):
        raise ValueError("求解日志含非有限数值；结果不可用于物理分析")
    if progress:
        progress(0.86, "导出真实网格场、载荷和时序")
    # Binary VTK avoids ASCII float32 underflow on physically negligible
    # alpha/omega values; VTK readers read the IEEE values directly.
    solver.run("foamToVTK", [], case, progress, start=0.86, span=0.03)
    result = collect_result(case, {**run, **solver_info})
    if progress:
        progress(0.94, "保存场数据与校核标记")
    return result


def mean_performance(result, discard_fraction=0.5):
    rows = result["performance"]
    if len(rows) < 4:
        raise ValueError("扬程统计至少需要4个非零时刻，请减小输出间隔")
    rows = rows[int(len(rows) * discard_fraction) :]
    if len(rows) < 2:
        raise ValueError("稳态统计时间窗不足")
    heads = np.asarray([r["head_m"] for r in rows])
    split = max(1, len(heads) // 2)
    drift = abs(float(heads[:split].mean() - heads[split:].mean())) / max(
        abs(float(heads.mean())), 1e-6
    )
    time = np.asarray([r["seconds"] for r in rows])

    def average(key):
        return float(np.trapezoid([r[key] for r in rows], time) / (time[-1] - time[0]))

    return {
        "head_m": average("head_m"),
        "flow_m3h": average("flow_m3h"),
        "npsha_m": average("npsha_m"),
        "head_window_drift": drift,
        "window_start_s": float(time[0]),
        "window_end_s": float(time[-1]),
    }


def cavitation_scan(project, options, root, progress=None):
    if project.kind not in ("cavitating_impeller", "cad") or (
        project.kind == "cad" and not project.cad_cavitation
    ):
        raise ValueError("汽蚀扫描需要两相旋转流体域")
    pressures = sorted(
        set(float(p) for p in options.get("pressures_pa", [101325, 20000, 8000, 5000, 3000])),
        reverse=True,
    )
    if not 4 <= len(pressures) <= 20 or min(pressures) <= project.vapour_pressure_pa:
        raise ValueError("需4–20个高于饱和蒸汽压的入口绝压点")
    if project.end_time_s * project.rpm / 60 < 3:
        raise ValueError("汽蚀性能扫描至少计算3个转周期，短时启动场不能充当性能图谱")
    outcomes = []
    baseline = None
    points = []
    reasons = []
    for i, pressure in enumerate(pressures):
        candidate = project.model_copy(deep=True)
        candidate.inlet_absolute_pa = pressure

        def report(fraction, message):
            if progress:
                progress(
                    0.02 + 0.91 * (i + fraction) / len(pressures),
                    f"汽蚀扫描 {i + 1}/{len(pressures)} · {message}",
                )

        run = execute_cfd(candidate, root, report)
        stats = mean_performance(run)
        if baseline is None:
            baseline = stats["head_m"]
            if baseline <= 0:
                raise ValueError("高压基准未得到正扬程，不能建立退化图谱")
        if stats["head_window_drift"] > 0.02:
            reasons.append(f"入口绝压{pressure:g}Pa的两个统计半窗扬程偏差超过2%，需延长计算")
        if abs(stats["flow_m3h"] / project.flow_m3h - 1) > 0.01:
            reasons.append("流量偏离固定流量试验目标超过1%")
        outcomes.append(
            {
                "case_directory": run["case_directory"],
                "inlet_absolute_pa": pressure,
                **stats,
                "maximum_vapour_fraction": run["summary"]["maximum_vapour_fraction"],
            }
        )
        points.append(
            CavitationPoint(
                flow_m3h=project.flow_m3h,
                rpm=project.rpm,
                npsha_m=stats["npsha_m"],
                head_m=stats["head_m"],
                noncavitating_head_m=baseline,
            )
        )
    mapping = CavitationMap(
        pump_id=options.get("pump_id", "P1"),
        source="cfd",
        source_note="OpenFOAM 14 Schnerr–Sauer pressure scan; case directories retained",
        numerical_acceptance="failed" if reasons else "passed",
        validation_notes=sorted(set(reasons)),
        points=points,
    )
    # An unresolved scan stays inspectable but cannot be activated as a ROM.
    return {
        "project": project.model_dump(mode="json"),
        "cases": outcomes,
        "map": mapping.model_dump(mode="json"),
        "npsh3": HeadBreakdown(mapping).npsh3(),
        "accepted_for_reduced_model": not reasons,
        "rejection_reasons": sorted(set(reasons)),
        "real_asset_validated": False,
        "notice": "网格/步长独立性与实测验证另行完成；NPSH3不是汽蚀初生",
    }


def convergence_study(project, options, root, progress=None):
    mode = options.get("mode", "mesh")
    if mode not in ("mesh", "time"):
        raise ValueError("校核类型必须为 mesh 或 time")
    rows = []
    for i, factor in enumerate([1, 1.5, 2.25]):
        p = project.model_copy(deep=True)
        if mode == "mesh":
            p.mesh_scale *= factor
        else:
            p.delta_t_s /= factor

        def report(fraction, message):
            if progress:
                progress(0.02 + 0.91 * (i + fraction) / 3, f"{mode}校核 {i + 1}/3 · {message}")

        run = execute_cfd(p, root, report)
        force = np.asarray([r["force_n"] for r in run["loads"]])
        row = {
            "factor": factor,
            "cells": run["summary"]["cells"],
            "requested_max_dt_s": p.delta_t_s,
            "dt_s": run["summary"].get(
                "dt_s", run["summary"].get("time_steps_s", {}).get("median", p.delta_t_s)
            ),
            "case_directory": run["case_directory"],
            "peak_force_n": float(np.linalg.norm(force, axis=1).max()) if len(force) else 0,
            "maximum_pressure_pa": run["summary"]["maximum_pressure_pa"],
        }
        if run["performance"]:
            row["mean_head_m"] = float(
                np.mean([r["head_m"] for r in run["performance"]][len(run["performance"]) // 2 :])
            )
        if "joukowsky" in run["verification"]:
            row["joukowsky_relative_error"] = run["verification"]["joukowsky"]["relative_error"]
        if "wavefront_normalized_rmse" in run["verification"]:
            row["wavefront_normalized_rmse"] = run["verification"]["wavefront_normalized_rmse"]
        rows.append(row)
    if mode == "time" and max(r["dt_s"] for r in rows) / min(r["dt_s"] for r in rows) < 1.2:
        raise ValueError(
            "实际时间步未明显改变（可能均受CFL限制）；请把基础delta_t_s降至当前实际步长以下再校核。算例已保留。"
        )
    if mode == "mesh" and len({r["cells"] for r in rows}) < 3:
        raise ValueError("三层实际单元数没有逐层变化，请调整基础网格。算例已保留。")
    comparisons = {}
    for key in ("peak_force_n", "maximum_pressure_pa", "mean_head_m", "wavefront_normalized_rmse"):
        if all(key in r for r in rows):
            values = [r[key] for r in rows]
            fine_change = abs(values[-1] - values[-2]) / max(abs(values[-1]), 1e-12)
            monotonic = (values[1] - values[0]) * (values[2] - values[1]) > 0
            comparisons[key] = {
                "fine_relative_change": fine_change,
                "monotonic": monotonic,
                "within_requested_tolerance": fine_change <= float(options.get("tolerance", 0.02)),
            }
    return {
        "mode": mode,
        "runs": rows,
        "comparisons": comparisons,
        "notice": "三层数值收敛检查；相邻变化小不等于实测准确，非单调结果不声明Richardson外推或GCI。",
    }


def result_manifest_hash(result):
    return hashlib.sha256(json.dumps(result["manifest"], sort_keys=True).encode()).hexdigest()
