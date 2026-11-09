"""Local partitioned waveform iteration, with explicit interface convergence tests."""

import json
import math
import re
from pathlib import Path

import numpy as np
from backend.models import G, NetworkDocument
from backend.physics import fluid, simulate_design
from backend.transient import TransientSettings, simulate_transient
from .cases import field


def waveform(values, end, label):
    a = np.asarray(values, dtype=float)
    if a.ndim != 2 or a.shape[1] != 2 or len(a) < 2 or not np.isfinite(a).all():
        raise ValueError(label + "必须是有限的[秒,数值]数组")
    if np.any(np.diff(a[:, 0]) <= 0) or a[0, 0] != 0 or a[-1, 0] < end:
        raise ValueError(label + "必须从0开始、严格递增并覆盖完整计算窗口")
    return a


def table(a):
    return "(" + " ".join(f"({t:.12g} {v:.12g})" for t, v in a) + ")"


def apply_waveforms(case, manifest, overrides):
    case = Path(case)
    p = manifest["project"]
    end = p["end_time_s"]
    inlet = waveform(overrides["inlet_pressure_pa"], end, "入口绝压")
    outlet = waveform(overrides["outlet_pressure_pa"], end, "出口绝压")
    if min(inlet[:, 1].min(), outlet[:, 1].min()) <= 0:
        raise ValueError("耦合边界出现非正绝压")
    compressible = manifest["compressible"]
    # Impose static port pressures and leave BOTH velocity boundaries free.
    # Prescribing pressure and flow on the same CFD port would overconstrain it.
    field(
        case,
        "U",
        "velocity",
        "(0 0 0)",
        """
"(inlet|outlet)" {type pressureInletOutletVelocity; value $internalField;}
"(statorDuct|nonCoupleIn1|nonCoupleOut2)" {type noSlip;}
"(rotorDuct|rotorBlades|nonCoupleIn2|nonCoupleOut1|rotor)" {type movingWallVelocity;value $internalField;}
wall {type noSlip;} nonCouple {type movingWallSlipVelocity;value $internalField;}
""",
        True,
    )
    boundaries = ""
    for port, values in [("inlet", inlet), ("outlet", outlet)]:
        values = values.copy()
        if not compressible:
            values[:, 1] = (values[:, 1] - manifest["pressure_reference_pa"]) / p["rho_kgm3"]
        boundaries += f"{port} {{type uniformFixedValue; uniformValue {{type table; values {table(values)};}} value uniform {values[0, 1]:.12g};}}\n"
    boundaries += "wall {type fixedFluxPressure; value $internalField;} nonCouple {type fixedFluxPressure;value $internalField;}"
    field(
        case,
        "p_rgh" if compressible else "p",
        "pressure" if compressible else "kinematicPressure",
        str(
            inlet[0, 1]
            if compressible
            else (inlet[0, 1] - manifest["pressure_reference_pa"]) / p["rho_kgm3"]
        ),
        boundaries,
    )
    speed = waveform(overrides["rpm"], end, "转速")
    if np.any(speed[:, 1] < 0):
        raise ValueError("当前耦合不支持反转；需要完整四象限验证")
    speed[:, 1] *= 2 * math.pi / 60
    dynamic = case / "constant/dynamicMeshDict"
    text = dynamic.read_text(encoding="utf-8")
    text, n = re.subn(
        r"omega\s+[-+\d.eE]+\s*\[rpm\]\s*;", f"omega {{type table; values {table(speed)};}}", text
    )
    if n != 1:
        raise ValueError("无法唯一设置旋转网格转速时序")
    dynamic.write_text(text, encoding="utf-8")
    if overrides.get("checkpoint"):
        restore_checkpoint(case, manifest, overrides["checkpoint"])
    manifest["coupling_boundary"] = overrides
    import hashlib

    manifest["input_hashes"] = {
        str(f.relative_to(case)): hashlib.sha256(f.read_bytes()).hexdigest()
        for d in ("0", "constant", "system")
        for f in (case / d).rglob("*")
        if f.is_file()
    }
    (case / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def restore_checkpoint(case, manifest, source):
    """Transfer a matching mesh's actual internal fields, preserving NEW port BCs."""
    source = Path(source).resolve()
    old = json.loads((source / "result.json").read_text(encoding="utf-8"))
    a = old["manifest"]["project"]
    b = manifest["project"]
    for key in (
        "kind",
        "mesh_scale",
        "cad_directory",
        "cad_bounds",
        "rho_kgm3",
        "viscosity_pas",
        "rotor_axis",
        "rotor_origin",
    ):
        if a.get(key) != b.get(key):
            raise ValueError("初场检查点与当前网格/介质不匹配：" + key)
    if not old["performance"]:
        raise ValueError("检查点缺少端口时序")
    times = [
        (float(f.name), f)
        for f in source.iterdir()
        if f.is_dir() and re.fullmatch(r"[\d.eE+-]+", f.name)
    ]
    _, latest = max(times)
    for target in (case / "0").iterdir():
        src = latest / target.name
        if not target.is_file() or not src.is_file():
            continue
        text = src.read_text(encoding="utf-8")
        match = re.search(r"internalField\s+(.*?)(?=\s*boundaryField\s*\{)", text, re.S)
        if not match:
            raise ValueError("检查点字段缺少internalField：" + target.name)
        new = re.sub(
            r"internalField\s+.*?(?=\s*boundaryField\s*\{)",
            "internalField " + match[1] + "\n",
            target.read_text(encoding="utf-8"),
            flags=re.S,
        )
        target.write_text(new, encoding="utf-8")
    # A checkpoint must be an integer revolution from its original geometry.
    rotations = a["rpm"] * old["frames"][-1]["seconds"] / 60
    if abs(rotations - round(rotations)) > 1e-5:
        raise ValueError("旋转检查点必须在整数转数时刻，避免场与初始网格相位错位")


def integrate_rotor(times, fluid_torque_nm, motor_torque_nm, inertia, initial_rpm, loss_nm=0):
    t = np.asarray(times, float)
    f = np.asarray(fluid_torque_nm, float)
    motor = np.broadcast_to(motor_torque_nm, t.shape)
    if (
        inertia <= 0
        or len(t) < 2
        or np.any(np.diff(t) <= 0)
        or not np.isfinite([*t, *f, *motor]).all()
    ):
        raise ValueError("无效的转子惯量/力矩时序")
    omega = np.empty(len(t))
    omega[0] = initial_rpm * 2 * math.pi / 60
    for i, dt in enumerate(np.diff(t)):
        omega[i + 1] = (
            omega[i] + dt * (0.5 * (f[i] + f[i + 1] + motor[i] + motor[i + 1]) - loss_nm) / inertia
        )
        if omega[i + 1] < 0:
            raise ValueError("转速发生反转；当前耦合需四象限模型，不能截零冒充解")
    return omega * 60 / (2 * math.pi)


def coupled_simulation(project, document, options, root, progress=None):
    from .jobs import execute_cfd

    if project.kind not in ("cavitating_impeller", "impeller", "cad") or (
        project.kind == "cad" and not project.cad_rotating
    ):
        raise ValueError("耦合需要旋转三维流体域")
    if any(project.gravity_ms2):
        raise ValueError("当前端口耦合要求CFD局部坐标无重力；高程由管网端口水头转换")
    doc = NetworkDocument.model_validate(document)
    pump_id = options.get("pump_id", "P1")
    pump = next((l for l in doc.links if l.id == pump_id and l.kind == "pump"), None)
    if pump is None:
        raise ValueError("需绑定管网中的一台泵")
    settings = TransientSettings.model_validate(options.get("transient", {}))
    if abs(settings.duration_s - project.end_time_s) > 1e-9:
        raise ValueError("CFD与管网必须使用同一时间窗口")
    max_iter = int(options.get("max_iterations", 8))
    relax = float(options.get("relaxation", 0.3))
    tol = float(options.get("relative_tolerance", 0.02))
    if not 2 <= max_iter <= 30 or not 0 < relax <= 1 or not 1e-5 <= tol <= 0.1:
        raise ValueError("无效的耦合迭代控制")
    checkpoint = options.get("checkpoint", "")
    rho, _, _ = fluid(doc.settings.temperature_c)
    if abs(rho / project.rho_kgm3 - 1) > 0.01:
        raise ValueError("CFD与管网密度不一致")
    times = np.linspace(
        0,
        project.end_time_s,
        max(21, min(1001, math.ceil(project.end_time_s / settings.report_interval_s) + 1)),
    )
    initial = simulate_design(doc)["points"][0]
    q0 = initial["links"][pump_id]["flow_m3h"] / 3600
    if settings.start_from_rest:
        q0 = 0
        rpm0 = 0
        ramp = float(options.get("start_ramp_s", 0.02))
        rpm = project.rpm * np.minimum(times / max(ramp, 1e-8), 1)
    else:
        if not checkpoint:
            raise ValueError("运行态耦合需提供已稳定的整数转数CFD检查点，或显式选择start_from_rest")
        old = json.loads((Path(checkpoint) / "result.json").read_text(encoding="utf-8"))
        last = old["performance"][-1]
        if abs(last["flow_m3h"] / 3600 - q0) > max(abs(q0) * 0.05, 1e-8):
            raise ValueError("检查点初始流量与管网运行态不匹配（>5%）")
        for port, nodeid in [("inlet", pump.start), ("outlet", pump.end)]:
            expected = doc.settings.atmospheric_kpa * 1000 + rho * G * (
                initial["nodes"][nodeid]["head_m"]
                - next(n.elevation_m for n in doc.nodes if n.id == nodeid)
            )
            if abs(last[port]["pressure_pa"] - expected) > float(
                options.get("initial_pressure_tolerance_pa", 1000)
            ):
                raise ValueError("检查点初始端口压力与管网不匹配：" + port)
        rpm0 = pump.nominal_rpm * pump.speed_ratio
        rpm = np.full(len(times), rpm0)
    qi = np.full(len(times), q0, dtype=float)
    qo = qi.copy()
    history = []
    network = None
    cfd = None
    motor_wave = (
        waveform(options["motor_torque_nm"], project.end_time_s, "电机力矩")
        if "motor_torque_nm" in options
        else None
    )
    x_history = []
    r_history = []
    previous_residual = None
    alpha = relax
    for iteration in range(max_iter):
        if progress:
            progress(iteration / max_iter, f"双向耦合 {iteration + 1}/{max_iter}")
        ss = settings.model_copy(deep=True)
        ss.prescribed_flows[pump_id] = [(float(t), float(v)) for t, v in zip(times, qi)]
        ss.prescribed_outflows[pump_id] = [(float(t), float(v)) for t, v in zip(times, qo)]
        ss.prescribed_speeds[pump_id] = [
            (float(t), float(v)) for t, v in zip(times, rpm / pump.nominal_rpm)
        ]
        network = simulate_transient(doc, ss)
        overrides = {"rpm": list(zip(times, rpm)), "checkpoint": checkpoint}
        for port, nodeid in [("inlet", pump.start), ("outlet", pump.end)]:
            overrides[port + "_pressure_pa"] = [
                (f["seconds"], f["nodes"][nodeid]["pressure_pa"]) for f in network["points"]
            ]
        callback = (
            (lambda v, m: progress((iteration + v) / max_iter, "耦合 · " + m)) if progress else None
        )
        cfd = execute_cfd(project, root, callback, overrides)
        rows = cfd["performance"]
        ft = np.array([r["seconds"] for r in rows])
        if len(ft) < 4:
            raise ValueError("耦合CFD端口时序不足")
        factor = 360 / cfd["manifest"]["sector_angle_deg"]
        ni = np.interp(times, ft, [-r["inlet"]["outward_flow_m3s"] * factor for r in rows])
        no = np.interp(times, ft, [r["outlet"]["outward_flow_m3s"] * factor for r in rows])
        ni[0] = no[0] = q0
        new_rpm = rpm.copy()
        if motor_wave is not None:
            loads = cfd["loads"]
            lt = np.asarray([r["seconds"] for r in loads])
            axis = np.asarray(project.rotor_axis)
            axis /= np.linalg.norm(axis)
            torque = np.asarray([np.dot(r["moment_nm"], axis) * factor for r in loads])
            new_rpm = integrate_rotor(
                times,
                np.interp(times, lt, torque),
                np.interp(times, *motor_wave.T),
                settings.pump_inertia_kgm2,
                rpm0,
                float(options.get("loss_torque_nm", 0)),
            )
        qerr = max(np.max(abs(ni - qi)), np.max(abs(no - qo))) / max(
            np.max(abs(ni)), np.max(abs(no)), 1e-6
        )
        werr = np.max(abs(new_rpm - rpm)) / max(project.rpm, 1)
        history.append(
            {
                "iteration": iteration + 1,
                "flow_relative_residual": float(qerr),
                "speed_relative_residual": float(werr),
                "case_directory": cfd["case_directory"],
            }
        )
        if max(qerr, werr) <= tol:
            break
        qs = max(project.flow_m3h / 3600, 1e-6)
        x = np.concatenate([qi / qs, qo / qs, rpm / max(project.rpm, 1)])
        residual = np.concatenate(
            [(ni - qi) / qs, (no - qo) / qs, (new_rpm - rpm) / max(project.rpm, 1)]
        )
        x_history.append(x.copy())
        r_history.append(residual.copy())
        if len(x_history) > 21:
            x_history.pop(0)
            r_history.pop(0)
        if options.get("acceleration", "anderson") == "anderson" and len(r_history) > 1:
            dx = np.column_stack([b - a for a, b in zip(x_history, x_history[1:])])
            dr = np.column_stack([b - a for a, b in zip(r_history, r_history[1:])])
            gamma = np.linalg.lstsq(dr, residual, rcond=1e-8)[0]
            new = x + relax * residual - (dx + relax * dr) @ gamma
            alpha = relax
        else:
            if previous_residual is not None and options.get("aitken", True):
                delta = residual - previous_residual
                denominator = float(np.dot(delta, delta))
                if denominator > 1e-24:
                    alpha = float(
                        np.clip(-alpha * np.dot(previous_residual, delta) / denominator, 0.001, 1)
                    )
            new = x + alpha * residual
        history[-1]["relaxation"] = alpha
        previous_residual = residual
        n = len(times)
        qi = new[:n] * qs
        qo = new[n : 2 * n] * qs
        rpm = new[2 * n :] * max(project.rpm, 1)
        qi[0] = qo[0] = q0
        rpm[0] = rpm0
    converged = (
        max(history[-1]["flow_relative_residual"], history[-1]["speed_relative_residual"]) <= tol
    )
    return {
        "model": "partitioned 3-D CFD / elastic 1-D MOC",
        "converged": converged,
        "history": history,
        "cfd": cfd,
        "network": network,
        "summary": {"converged": converged, "iterations": len(history)},
        "real_asset_validated": False,
        "notice": "边界波形迭代；未收敛结果不可作为耦合解。压缩储存由进出口两条流量分别反馈；转子力矩按明确轴投影。",
    }
