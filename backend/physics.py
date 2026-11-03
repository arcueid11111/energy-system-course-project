"""Single EPANET 2.2 engine for user-designed 2D/3D networks."""

import hashlib
import json
import math
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import networkx as nx
import numpy as np
import wntr
from wntr.network.controls import Control, ControlAction, SimTimeCondition
from .models import NetworkDocument, G


def fluid(temperature):
    rho = 1000 * (
        1
        - ((temperature + 288.9414) / (508929.2 * (temperature + 68.12963)))
        * (temperature - 3.9863) ** 2
    )
    mu = 2.414e-5 * 10 ** (247.8 / (temperature + 133.15))
    vapor_pa = 133.322 * 10 ** (8.07131 - 1730.63 / (233.426 + temperature))
    return rho, mu / rho, vapor_pa


def validate_topology(doc: NetworkDocument):
    errors = []
    samples = round(doc.settings.duration_hours * 3600 / doc.settings.step_seconds) + 1
    if samples * (len(doc.nodes) + len(doc.links)) > 600000:
        errors.append("本次网络与报告时刻组合超过 60 万个对象采样，请缩短时长或增大报告步长")
    ids = [n.id for n in doc.nodes]
    links = [l.id for l in doc.links]
    if not ids:
        errors.append("请先添加节点")
    if not doc.links:
        errors.append("请至少添加一条连接")
    if len(set(ids)) != len(ids) or len(set(links)) != len(links):
        errors.append("节点或设备编号重复")
    nodes = {n.id: n for n in doc.nodes}
    graph = nx.Graph()
    graph.add_nodes_from(ids)
    for link in doc.links:
        if link.start not in nodes or link.end not in nodes:
            errors.append(f"{link.id} 引用了不存在的节点")
            continue
        if link.start == link.end:
            errors.append(f"{link.id} 不能连接自身")
        graph.add_edge(link.start, link.end)
        if link.kind == "valve" and (
            nodes[link.start].kind != "junction" or nodes[link.end].kind != "junction"
        ):
            errors.append(f"{link.id} 阀门两端需为连接/需水节点，请在水源或水箱旁增加连接节点")
        if (
            link.kind == "pipe"
            and link.length_m + 1e-7
            < math.dist(
                (nodes[link.start].x, nodes[link.start].y, nodes[link.start].z),
                (nodes[link.end].x, nodes[link.end].y, nodes[link.end].z),
            )
            and doc.cad_metadata
        ):
            errors.append(f"{link.id} 管长小于两端三维直线距离，请检查 CAD 单位或管长")
    sources = {n.id for n in doc.nodes if n.kind in ("reservoir", "tank")}
    if not sources:
        errors.append("管网缺少水源/定水头节点或水箱；CAD 几何不会自动生成供水边界")
    for group in nx.connected_components(graph):
        if not group & sources:
            errors.append("以下节点所在连通分量没有水源：" + ", ".join(sorted(group)[:12]))
    devices = {l.id: l for l in doc.links}
    for e in doc.settings.events:
        l = devices.get(e.target)
        if not l:
            errors.append(f"控制事件引用了不存在的设备 {e.target}")
            continue
        if e.kind == "speed" and (l.kind != "pump" or not 0.2 <= e.value <= 1.2):
            errors.append(f"{e.target} 转速事件仅适用于泵，范围 0.2–1.2")
        if e.kind == "opening" and (l.kind != "valve" or not 0 <= e.value <= 1):
            errors.append(f"{e.target} 开度事件仅适用于阀门，范围 0–1")
        if e.kind == "enabled" and e.value not in (0, 1):
            errors.append("启停事件值必须为 0 或 1")
    return errors


def build_design(doc: NetworkDocument):
    errors = validate_topology(doc)
    if errors:
        raise ValueError("\n".join(errors))
    s = doc.settings
    wn = wntr.network.WaterNetworkModel()
    wn.options.hydraulic = wntr.network.options.HydraulicOptions(
        headloss="D-W",
        demand_model="PDD",
        minimum_pressure=0,
        required_pressure=s.required_pressure_m,
        accuracy=1e-7,
        trials=200,
        unbalanced="STOP",
    )
    rho, nu, _ = fluid(s.temperature_c)
    wn.options.hydraulic.viscosity = nu / 1.004e-6
    wn.options.hydraulic.specific_gravity = rho / 1000
    wn.options.time.duration = round(s.duration_hours * 3600)
    wn.options.time.hydraulic_timestep = s.step_seconds
    wn.options.time.report_timestep = s.step_seconds
    for n in doc.nodes:
        if n.kind == "reservoir":
            wn.add_reservoir(n.id, base_head=n.source_head_m, coordinates=(n.x, n.y))
        elif n.kind == "tank":
            wn.add_tank(
                n.id,
                elevation=n.elevation_m,
                init_level=n.tank_level_m,
                min_level=n.tank_min_m,
                max_level=n.tank_max_m,
                diameter=n.tank_diameter_m,
                coordinates=(n.x, n.y),
            )
        else:
            wn.add_junction(
                n.id,
                base_demand=n.demand_m3h * s.demand_multiplier / 3600,
                elevation=n.elevation_m,
                coordinates=(n.x, n.y),
            )
    for i, l in enumerate(doc.links):
        if l.kind == "pipe":
            wn.add_pipe(
                l.id,
                l.start,
                l.end,
                length=l.length_m,
                diameter=l.diameter_m,
                roughness=l.roughness_m,
                minor_loss=l.minor_loss,
                initial_status="OPEN" if l.enabled else "CLOSED",
                check_valve=l.check_valve,
            )
        elif l.kind == "pump":
            wn.add_curve(f"_curve_{i}", "HEAD", [(p.flow_m3h / 3600, p.head_m) for p in l.curve])
            wn.add_pump(
                l.id,
                l.start,
                l.end,
                pump_type="HEAD",
                pump_parameter=f"_curve_{i}",
                speed=l.speed_ratio,
                initial_status="OPEN" if l.enabled else "CLOSED",
            )
        else:
            wn.add_valve(
                l.id,
                l.start,
                l.end,
                diameter=l.diameter_m,
                valve_type="TCV",
                initial_setting=valve_k(l.opening),
                initial_status="ACTIVE" if l.enabled and l.opening else "CLOSED",
            )
    enabled = {l.id: l.enabled for l in doc.links}
    speeds = {l.id: l.speed_ratio for l in doc.links}
    openings = {l.id: l.opening for l in doc.links}
    for i, e in enumerate(sorted(s.events, key=lambda e: e.hour)):
        l = next(l for l in doc.links if l.id == e.target)
        if e.kind == "enabled":
            enabled[l.id] = bool(e.value)
        elif e.kind == "speed":
            speeds[l.id] = e.value
        else:
            openings[l.id] = e.value
        if not enabled[l.id] or (l.kind == "valve" and not openings[l.id]):
            attribute, value = "status", wntr.network.LinkStatus.Closed
        elif l.kind == "pump":
            attribute, value = "base_speed", speeds[l.id]
        elif l.kind == "valve":
            attribute, value = "setting", valve_k(openings[l.id])
        else:
            attribute, value = "status", wntr.network.LinkStatus.Open
        wn.add_control(
            f"_event_{i}",
            Control(
                SimTimeCondition(wn, ">=", round(e.hour * 3600)),
                ControlAction(wn.get_link(l.id), attribute, value),
            ),
        )
    return wn


def valve_k(opening):
    return min(1e7, 2 + 20 * (1 / max(opening, 0.005) ** 2 - 1))


def simulate_design(doc: NetworkDocument, progress=None):
    if progress:
        progress(0.05, "校验自定义管网并构建 EPANET 模型")
    wn = build_design(doc)
    with tempfile.TemporaryDirectory(prefix="flowstate-design-") as temp:
        result = wntr.sim.EpanetSimulator(wn).run_sim(
            file_prefix=str(Path(temp) / "design"), version=2.2, convergence_error=True
        )
    if result.error_code:
        raise ValueError(f"EPANET 未收敛：{result.error_code}")
    times = list(result.node["head"].index)
    if len(times) != round(doc.settings.duration_hours * 3600 / doc.settings.step_seconds) + 1:
        raise ValueError("求解时间轴不完整")
    if progress:
        progress(0.8, "计算功率、供水与节点水量平衡")
    rho, _, _ = fluid(doc.settings.temperature_c)
    points = []
    warnings = set()
    for t in times:
        flows = result.link["flowrate"].loc[t]
        heads = result.node["head"].loc[t]
        pressures = result.node["pressure"].loc[t]
        demand = result.node["demand"].loc[t]
        nr = {
            n.id: {
                "pressure_m": float(pressures[n.id]),
                "head_m": float(heads[n.id]),
                "demand_m3h": max(0, float(demand[n.id]) * 3600) if n.kind == "junction" else 0,
                "requested_m3h": n.demand_m3h * doc.settings.demand_multiplier
                if n.kind == "junction"
                else 0,
                "tank_level_m": float(heads[n.id]) - n.elevation_m if n.kind == "tank" else None,
            }
            for n in doc.nodes
        }
        lr = {}
        power = 0
        residuals = {n.id: 0.0 for n in doc.nodes if n.kind == "junction"}
        for l in doc.links:
            q = float(flows[l.id])
            h = float(heads[l.start] - heads[l.end])
            active = bool(result.link["status"].loc[t, l.id])
            if l.start in residuals:
                residuals[l.start] -= q * 3600
            if l.end in residuals:
                residuals[l.end] += q * 3600
            row = {
                "flow_m3h": q * 3600,
                "headloss_m": h,
                "active": active,
                "velocity_ms": float(result.link["velocity"].loc[t, l.id]),
                "power_kw": 0.0,
            }
            if l.kind == "pump":
                speed = l.speed_ratio
                for e in sorted(doc.settings.events, key=lambda e: e.hour):
                    if e.target == l.id and e.kind == "speed" and e.hour * 3600 <= t:
                        speed = e.value
                ref = max(q, 0) / speed * 3600
                curve = l.curve
                shaft = (
                    float(np.interp(ref, [p.flow_m3h for p in curve], [p.shaft_kw for p in curve]))
                    * speed**3
                    * rho
                    / 998.2
                    if active
                    else 0
                )
                row.update(
                    rpm=l.nominal_rpm * speed if active else 0,
                    head_m=-h if active else 0,
                    shaft_kw=shaft,
                    power_kw=shaft / (l.motor_efficiency * l.drive_efficiency),
                    efficiency=rho * G * max(q, 0) * max(-h, 0) / (shaft * 1000) if shaft else 0,
                )
                power += row["power_kw"]
                if active and ref > curve[-1].flow_m3h:
                    warnings.add(f"{l.id} 超出泵曲线范围，功率估算不适用")
                if shaft > l.motor_rated_kw:
                    warnings.add(f"{l.id} 轴功率超过电机额定值")
                if row["efficiency"] > 1.001:
                    raise ValueError(f"{l.id} 水力与轴功率曲线不一致，效率超过 1")
            lr[l.id] = row
        max_res = max((abs(v - nr[n]["demand_m3h"]) for n, v in residuals.items()), default=0)
        if max_res > 0.02:
            raise ValueError(f"节点水量守恒残差 {max_res:.4g} m³/h 超过容差")
        delivered = sum(n["demand_m3h"] for n in nr.values())
        requested = sum(n["requested_m3h"] for n in nr.values())
        pressured = [
            nr[n.id]["pressure_m"] for n in doc.nodes if n.kind == "junction" and n.demand_m3h > 0
        ]
        points.append(
            {
                "seconds": int(t),
                "hour": float(t) / 3600,
                "nodes": nr,
                "links": lr,
                "power_kw": power,
                "delivered_m3h": delivered,
                "requested_m3h": requested,
                "unmet_m3h": max(0, requested - delivered),
                "min_pressure_m": min(pressured) if pressured else None,
                "max_mass_residual_m3h": max_res,
            }
        )
    dt = doc.settings.step_seconds / 3600
    energy = sum(p["power_kw"] * dt for p in points[:-1])
    volume = sum(p["delivered_m3h"] * dt for p in points[:-1])
    unmet = sum(p["unmet_m3h"] * dt for p in points[:-1])
    if unmet > 0.01:
        warnings.add("存在未满足需水量；请检查边界、泵选型、管径与阀门")
    payload = doc.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return {
        "document": payload,
        "points": points,
        "summary": {
            "energy_kwh": energy,
            "cost_yuan": energy * doc.settings.electricity_price,
            "delivered_m3": volume,
            "unmet_m3": unmet,
            "peak_power_kw": max(p["power_kw"] for p in points),
            "max_mass_residual_m3h": max(p["max_mass_residual_m3h"] for p in points),
        },
        "warnings": sorted(warnings),
        "meta": {
            "solver": "EPANET 2.2",
            "headloss": "Darcy–Weisbach",
            "demand_model": "PDD",
            "converged": True,
            "model_version": "custom-network-1",
            "input_sha256": digest,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "validated_on_real_asset": False,
            "assumptions": [
                "准稳态清水，未含水锤或两相流",
                "绘图坐标 z 与水力高程分别保存，需明确确认映射",
                "功率按假设/输入曲线及转速相似关系估算",
                "累计量按报告间隔左端积分",
            ],
        },
    }


def export_design_inp(doc):
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "design.inp"
        wntr.network.write_inpfile(build_design(doc), str(path), units="LPS", version=2.2)
        return path.read_text()
