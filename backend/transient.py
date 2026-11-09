"""Elastic-water-hammer reference model, with pump inertia and head-breakdown maps.

Method of characteristics with interpolated characteristic feet (CFL <= 1).
This is the independently labelled 1-D reference/coupling model, never a 3-D field.
"""

import math
from typing import Literal

import numpy as np
from pydantic import Field, model_validator
from scipy.optimize import root

from .cavitation import CavitationMap, HeadBreakdown, npsha
from .cfd.schemas import PipeWall
from .models import G, NetworkDocument, StrictModel
from .physics import fluid, simulate_design, valve_k


class TransientEvent(StrictModel):
    seconds: float = Field(ge=0)
    target: str
    kind: Literal["pump_trip", "speed_target", "valve_opening"]
    value: float = 0
    ramp_seconds: float = Field(0.01, ge=0)


class TransientSettings(StrictModel):
    duration_s: float = Field(0.3, gt=0, le=3600)
    max_dt_s: float = Field(0.0005, gt=1e-7, le=1)
    report_interval_s: float = Field(0.002, gt=0, le=10)
    cells_per_pipe: int = Field(20, ge=4, le=1000)
    wall: PipeWall = Field(default_factory=PipeWall)
    pipe_walls: dict[str, PipeWall] = Field(default_factory=dict)
    bulk_modulus_pa: float = Field(2.2e9, gt=1e6, le=1e11)
    pump_inertia_kgm2: float = Field(0.02, gt=1e-7, le=1e6)
    pipe_friction: bool = True
    events: list[TransientEvent] = Field(default_factory=list, max_length=100)
    cavitation_maps: dict[str, CavitationMap] = Field(default_factory=dict)
    # External port flow waveforms are used only by the partitioned CFD coupling.
    prescribed_flows: dict[str, list[tuple[float, float]]] = Field(default_factory=dict)
    prescribed_outflows: dict[str, list[tuple[float, float]]] = Field(default_factory=dict)
    prescribed_speeds: dict[str, list[tuple[float, float]]] = Field(default_factory=dict)
    start_from_rest: bool = False

    @model_validator(mode="after")
    def checks(self):
        if (
            self.report_interval_s > self.duration_s
            or self.duration_s / self.report_interval_s > 10000
        ):
            raise ValueError("瞬变输出间隔需小于总时长，且最多10000帧")
        if self.duration_s / self.max_dt_s > 300000:
            raise ValueError("瞬变步数过多，请缩短时间窗口或增加时间步长")
        if any(e.seconds >= self.duration_s for e in self.events):
            raise ValueError("瞬变事件必须在计算结束前")
        for values in [
            *self.prescribed_flows.values(),
            *self.prescribed_outflows.values(),
            *self.prescribed_speeds.values(),
        ]:
            if len(values) < 2 or any(a[0] >= b[0] for a, b in zip(values, values[1:])):
                raise ValueError("耦合流量时序需严格递增且至少两点")
            if values[0][0] > 0 or values[-1][0] < self.duration_s:
                raise ValueError("耦合流量时序必须覆盖完整计算窗口")
        return self


def acoustic_speed(rho, bulk, diameter, wall):
    return math.sqrt(
        1
        / (rho * (1 / bulk + wall.restraint_factor * diameter / (wall.young_pa * wall.thickness_m)))
    )


def friction_factor(q, diameter, roughness, nu):
    velocity = np.abs(q) / (math.pi * diameter**2 / 4)
    reynolds = np.maximum(velocity * diameter / nu, 1e-12)
    turbulent = (
        0.25
        / np.log10(roughness / (3.7 * diameter) + 5.74 / np.maximum(reynolds, 2300) ** 0.9) ** 2
    )
    laminar = 64 / np.maximum(reynolds, 1)
    weight = np.clip((reynolds - 2300) / 1700, 0, 1)
    return (1 - weight) * laminar + weight * turbulent


def reference_pipe(
    length_m=20,
    diameter_m=0.1,
    wave_speed_ms=1000,
    velocity_ms=1,
    rho_kgm3=998.2,
    duration_s=0.06,
    cells=100,
    close_time_s=0.001,
):
    """Frictionless reservoir-pipe-valve benchmark, suitable for Joukowsky checks."""
    dt = length_m / cells / wave_speed_ms
    count = math.ceil(duration_s / dt)
    dt = duration_s / count
    dx = length_m / cells
    x = np.linspace(0, length_m, cells + 1)
    h = np.zeros(cells + 1)
    v = np.full(cells + 1, velocity_ms)
    rows = []
    for step in range(count + 1):
        t = step * dt
        rows.append(
            {
                "seconds": t,
                "valve_pressure_rise_pa": float(rho_kgm3 * G * h[-1]),
                "mid_pressure_rise_pa": float(rho_kgm3 * G * h[cells // 2]),
                "valve_velocity_ms": float(v[-1]),
            }
        )
        if step == count:
            break
        cp = np.interp(np.maximum(0, x - wave_speed_ms * dt), x, h + wave_speed_ms / G * v)
        cm = np.interp(np.minimum(length_m, x + wave_speed_ms * dt), x, h - wave_speed_ms / G * v)
        new_h = (cp + cm) / 2
        new_v = (cp - cm) * G / (2 * wave_speed_ms)
        new_h[0] = 0
        new_v[0] = -cm[0] * G / wave_speed_ms
        new_v[-1] = velocity_ms * max(0, 1 - (t + dt) / close_time_s)
        new_h[-1] = cp[-1] - wave_speed_ms / G * new_v[-1]
        h, v = new_h, new_v
    return {
        "model": "1-D elastic MOC reference",
        "points": rows,
        "expected_joukowsky_pa": rho_kgm3 * wave_speed_ms * velocity_ms,
        "round_trip_s": 2 * length_m / wave_speed_ms,
        "dt_s": dt,
        "dx_m": dx,
        "notice": "无摩阻、单相弹性参考；不是三维流场或实测结果",
    }


def simulate_transient(doc: NetworkDocument, settings: TransientSettings, progress=None):
    if doc.settings.events:
        raise ValueError("瞬变分析使用独立的秒级事件，请移除准稳态小时级事件")
    if len(doc.links) > 100 or len(doc.nodes) > 100:
        raise ValueError("当前弹性参考模型最多100节点/设备；大网请分区计算")
    nodes = {n.id: n for n in doc.nodes}
    links = {l.id: l for l in doc.links}
    pipes = [l for l in doc.links if l.kind == "pipe"]
    devices = [l for l in doc.links if l.kind != "pipe"]
    if not pipes:
        raise ValueError("瞬变管网至少需要一条有储能的管道")
    for e in settings.events:
        if e.target not in links:
            raise ValueError("瞬变事件设备不存在：" + e.target)
        link = links[e.target]
        if (e.kind == "valve_opening" and link.kind != "valve") or (
            e.kind != "valve_opening" and link.kind != "pump"
        ):
            raise ValueError("瞬变事件与设备类型不一致")
        if e.kind == "valve_opening" and not 0 <= e.value <= 1:
            raise ValueError("阀门开度应在0–1之间")
        if e.kind == "speed_target" and not 0 <= e.value <= 1.2:
            raise ValueError("目标转速比应在0–1.2之间")
    baseline = simulate_design(doc)["points"][0]
    if settings.start_from_rest:
        fixed = [
            n.source_head_m if n.kind == "reservoir" else n.elevation_m + n.tank_level_m
            for n in doc.nodes
            if n.kind != "junction"
        ]
        if not fixed or max(fixed) - min(fixed) > 1e-6 or any(n.demand_m3h for n in doc.nodes):
            raise ValueError("静止初场要求所有定水头/水箱初始水头相同且节点需水为零")
        for value in baseline["nodes"].values():
            value["head_m"] = fixed[0]
        for value in baseline["links"].values():
            value["flow_m3h"] = 0
    if not set(settings.prescribed_flows) <= {l.id for l in devices}:
        raise ValueError("外部流量只可绑定泵或阀门端口")
    if not set(settings.prescribed_outflows) <= set(settings.prescribed_flows):
        raise ValueError("外部出口流量必须同时提供入口流量")
    if not set(settings.prescribed_speeds) <= {l.id for l in devices if l.kind == "pump"}:
        raise ValueError("外部转速必须绑定泵")
    rho, nu, pv = fluid(doc.settings.temperature_c)
    for key, mapping in settings.cavitation_maps.items():
        if mapping.source == "cfd" and mapping.numerical_acceptance != "passed":
            raise ValueError(key + " 的CFD汽蚀图谱尚未通过数值稳定性审核，不能用于降阶反馈")
    maps = {key: HeadBreakdown(value) for key, value in settings.cavitation_maps.items()}
    state = {}
    dt = settings.max_dt_s
    for link in pipes:
        wall = settings.pipe_walls.get(link.id, settings.wall)
        speed = acoustic_speed(rho, settings.bulk_modulus_pa, link.diameter_m, wall)
        nx = max(4, settings.cells_per_pipe)
        dx = link.length_m / nx
        dt = min(dt, dx / speed)
        area = math.pi * link.diameter_m**2 / 4
        q = baseline["links"][link.id]["flow_m3h"] / 3600
        state[link.id] = {
            "x": np.linspace(0, link.length_m, nx + 1),
            "a": speed,
            "b": speed / (G * area),
            "area": area,
            "q": np.full(nx + 1, q),
            "h": np.linspace(
                baseline["nodes"][link.start]["head_m"],
                baseline["nodes"][link.end]["head_m"],
                nx + 1,
            ),
        }
    count = math.ceil(settings.duration_s / dt)
    dt = settings.duration_s / count
    if count > 300000 or sum(len(v["x"]) for v in state.values()) * count > 100000000:
        raise ValueError("瞬变计算规模超过本机参考求解器限额，请缩短时窗或减少管段采样")
    unknown = [n.id for n in doc.nodes if n.kind == "junction"]
    idx = {name: i for i, name in enumerate(unknown)}
    device_idx = {l.id: len(unknown) + i for i, l in enumerate(devices)}
    heads = {n.id: baseline["nodes"][n.id]["head_m"] for n in doc.nodes}
    speeds = {l.id: l.speed_ratio if l.enabled else 0.0 for l in devices if l.kind == "pump"}
    openings = {l.id: l.opening if l.enabled else 0.0 for l in devices if l.kind == "valve"}
    device_q = {l.id: baseline["links"][l.id]["flow_m3h"] / 3600 for l in devices}
    tripped = set()
    frames = []
    next_report = 0.0
    warnings = set()
    if maps:
        warnings.add(
            "初场采用非汽蚀EPANET工况；当前图谱从t=0激活，属于汽蚀突加瞬变，不能当作已收敛汽蚀稳态初场"
        )
    initial = np.array([heads[k] for k in unknown] + [device_q[l.id] for l in devices])
    head_scale = max(10, max(abs(v) for v in heads.values()))
    q_scale = max(0.001, max(abs(v) for v in device_q.values()) if device_q else 0.001)

    scheduled = {}
    for event in sorted(settings.events, key=lambda e: e.seconds):
        if event.kind == "pump_trip":
            continue
        key = (event.target, event.kind)
        link = links[event.target]
        previous = scheduled.setdefault(key, [])
        base = link.speed_ratio if event.kind == "speed_target" else link.opening
        if previous:
            last, initial = previous[-1]
            f = min(1, max(0, (event.seconds - last.seconds) / max(last.ramp_seconds, 1e-12)))
            base = initial + (last.value - initial) * f
        previous.append((event, base))

    def controls(t):
        for e in settings.events:
            if e.kind == "pump_trip" and t >= e.seconds:
                tripped.add(e.target)
        for (target, kind), entries in scheduled.items():
            for e, base in entries:
                if t < e.seconds:
                    break
                f = min(1, max(0, (t - e.seconds) / max(e.ramp_seconds, 1e-12)))
                if kind == "speed_target" and target not in tripped:
                    speeds[target] = base + (e.value - base) * f
                elif kind == "valve_opening":
                    openings[target] = base + (e.value - base) * f
        for target, values in settings.prescribed_speeds.items():
            wave = np.asarray(values)
            speeds[target] = float(np.interp(t, wave[:, 0], wave[:, 1]))

    def pump_head(link, q, h):
        ratio = max(speeds[link.id], 0.001)
        ref = max(q, 0) * 3600 / ratio
        curve = link.curve
        if ref > curve[-1].flow_m3h:
            warnings.add(link.id + " 超出泵参考曲线；该段结果需四象限/扩展曲线验证")
        base = (
            float(np.interp(ref, [p.flow_m3h for p in curve], [p.head_m for p in curve])) * ratio**2
        )
        if link.id in maps and q > 1e-10:
            node = nodes[link.start]
            inlet_pa = doc.settings.atmospheric_kpa * 1000 + rho * G * (
                h[link.start] - node.elevation_m
            )
            available = npsha(inlet_pa, q / (math.pi * link.diameter_m**2 / 4), rho, pv)
            base *= maps[link.id].ratio(q * 3600, link.nominal_rpm * ratio, available)
        return base

    for step in range(count + 1):
        t = step * dt
        controls(t)
        if t + dt * 0.01 >= next_report or step == count:
            frame = {
                "seconds": t,
                "nodes": {
                    k: {
                        "head_m": float(h),
                        "pressure_pa": float(
                            doc.settings.atmospheric_kpa * 1000
                            + rho * G * (h - nodes[k].elevation_m)
                        ),
                    }
                    for k, h in heads.items()
                },
                "devices": {
                    k: {"flow_m3h": q * 3600, "speed_ratio": speeds.get(k)}
                    for k, q in device_q.items()
                },
                "pipes": {},
            }
            for link in pipes:
                st = state[link.id]
                z = np.linspace(
                    nodes[link.start].elevation_m, nodes[link.end].elevation_m, len(st["x"])
                )
                pressure = doc.settings.atmospheric_kpa * 1000 + rho * G * (st["h"] - z)
                if pressure.min() < pv:
                    warnings.add(
                        "压力低于饱和蒸汽压：单相参考模型不再适用，应读取两相CFD结果；未强行截断压力"
                    )
                frame["pipes"][link.id] = {
                    "x_m": st["x"].tolist(),
                    "pressure_pa": pressure.tolist(),
                    "velocity_ms": (st["q"] / st["area"]).tolist(),
                }
            frames.append(frame)
            next_report += settings.report_interval_s
        if progress and (step % max(1, count // 100) == 0):
            progress(0.05 + 0.85 * step / count, f"弹性MOC · {t:.5g}/{settings.duration_s:g} s")
        if step == count:
            break
        outgoing = {}
        incoming = {}
        for link in pipes:
            st = state[link.id]
            x, h, q = st["x"], st["h"], st["q"]
            a, b = st["a"], st["b"]
            plus = np.interp(np.maximum(0, x - a * dt), x, h + b * q)
            minus = np.interp(np.minimum(link.length_m, x + a * dt), x, h - b * q)
            if settings.pipe_friction:
                f = friction_factor(q, link.diameter_m, link.roughness_m, nu)
                resistance = (
                    (f / link.diameter_m + link.minor_loss / link.length_m)
                    * a
                    * dt
                    / (2 * G * st["area"] ** 2)
                )
                losses = resistance * q * np.abs(q)
                plus -= np.interp(np.maximum(0, x - a * dt), x, losses)
                minus += np.interp(np.minimum(link.length_m, x + a * dt), x, losses)
            st["new_h"] = (plus + minus) / 2
            st["new_q"] = (plus - minus) / (2 * b)
            incoming[link.id] = float(minus[0])
            outgoing[link.id] = float(plus[-1])

        def equations(values):
            h = {**heads, **{key: values[i] for key, i in idx.items()}}
            balance = {key: 0.0 for key in unknown}
            for link in pipes:
                st = state[link.id]
                qa = (h[link.start] - incoming[link.id]) / st["b"]
                qb = (outgoing[link.id] - h[link.end]) / st["b"]
                if link.start in balance:
                    balance[link.start] += qa
                if link.end in balance:
                    balance[link.end] -= qb
            device_res = []
            for link in devices:
                q = values[device_idx[link.id]]
                if link.start in balance:
                    balance[link.start] += q
                if link.end in balance:
                    if link.id in settings.prescribed_outflows:
                        wave = np.asarray(settings.prescribed_outflows[link.id])
                        balance[link.end] -= float(np.interp(t + dt, wave[:, 0], wave[:, 1]))
                    else:
                        balance[link.end] -= q
                if link.id in settings.prescribed_flows:
                    wave = np.asarray(settings.prescribed_flows[link.id])
                    device_res.append(
                        (q - float(np.interp(t + dt, wave[:, 0], wave[:, 1]))) / q_scale
                    )
                elif link.kind == "valve":
                    opening = openings[link.id]
                    area = math.pi * link.diameter_m**2 / 4
                    device_res.append(
                        q / q_scale
                        if opening <= 1e-6
                        else (
                            h[link.start]
                            - h[link.end]
                            - valve_k(opening) * q * abs(q) / (2 * G * area**2)
                        )
                        / head_scale
                    )
                elif speeds[link.id] < 0.001 and not link.enabled:
                    device_res.append(q / q_scale)
                else:
                    gap = (h[link.end] - h[link.start] - pump_head(link, q, h)) / head_scale
                    qq = q / q_scale
                    # One-way pump/check-valve complementarity; reverse rotation
                    # requires real four-quadrant curves and is not invented here.
                    device_res.append(math.sqrt(qq * qq + gap * gap) - qq - gap)
            residual = []
            for key in unknown:
                n = nodes[key]
                pressure_head = max(0, h[key] - n.elevation_m)
                supplied = (
                    n.demand_m3h
                    * doc.settings.demand_multiplier
                    / 3600
                    * min(1, math.sqrt(pressure_head / doc.settings.required_pressure_m))
                )
                residual.append((balance[key] + supplied) / q_scale)
            return residual + device_res

        solved = root(equations, initial, method="hybr", options={"xtol": 1e-8})
        residual = float(np.max(np.abs(equations(solved.x)))) if len(initial) else 0
        if not np.all(np.isfinite(solved.x)) or residual > 1e-5:
            raise ValueError(f"瞬变边界未收敛 t={t:.7g}s，归一残差={residual:.3g}")
        initial = solved.x
        for key, i in idx.items():
            heads[key] = float(solved.x[i])
        for link in devices:
            device_q[link.id] = float(solved.x[device_idx[link.id]])
        tank_net = {n.id: 0.0 for n in doc.nodes if n.kind == "tank"}
        for link in pipes:
            st = state[link.id]
            h, q = st["new_h"], st["new_q"]
            h[0], h[-1] = heads[link.start], heads[link.end]
            q[0] = (h[0] - incoming[link.id]) / st["b"]
            q[-1] = (outgoing[link.id] - h[-1]) / st["b"]
            if not link.enabled:
                raise ValueError("参考MOC暂不接受关闭的管段，请移除隔离管或改用显式阀门")
            if link.start in tank_net:
                tank_net[link.start] -= q[0]
            if link.end in tank_net:
                tank_net[link.end] += q[-1]
            st["h"], st["q"] = h, q
        for link in devices:
            q = device_q[link.id]
            if link.start in tank_net:
                tank_net[link.start] -= q
            if link.end in tank_net:
                tank_net[link.end] += (
                    float(np.interp(t + dt, *np.asarray(settings.prescribed_outflows[link.id]).T))
                    if link.id in settings.prescribed_outflows
                    else q
                )
            if (
                link.kind == "pump"
                and link.id in tripped
                and speeds[link.id] > 0
                and link.id not in settings.prescribed_speeds
            ):
                ratio = max(0.001, speeds[link.id])
                omega0 = link.nominal_rpm * 2 * math.pi / 60
                ref = max(q, 0) * 3600 / ratio
                shaft = (
                    float(
                        np.interp(
                            ref, [p.flow_m3h for p in link.curve], [p.shaft_kw for p in link.curve]
                        )
                    )
                    * 1000
                    * ratio**3
                    * rho
                    / 998.2
                )
                torque = shaft / (ratio * omega0)
                speeds[link.id] = max(
                    0, speeds[link.id] - dt * torque / (settings.pump_inertia_kgm2 * omega0)
                )
        for key, net in tank_net.items():
            node = nodes[key]
            heads[key] += dt * net / (math.pi * node.tank_diameter_m**2 / 4)
            level = heads[key] - node.elevation_m
            if not node.tank_min_m <= level <= node.tank_max_m:
                raise ValueError("瞬变期间水箱液位越界：" + key)
    return {
        "model": "1-D elastic MOC reference",
        "document": doc.model_dump(mode="json"),
        "settings": settings.model_dump(mode="json"),
        "points": frames,
        "dt_s": dt,
        "wave_speeds_ms": {key: st["a"] for key, st in state.items()},
        "warnings": sorted(warnings),
        "validated_on_real_asset": False,
        "notice": "弹性一维参考；不代表三维速度场，不含反转四象限曲线或水柱分离模型",
    }
