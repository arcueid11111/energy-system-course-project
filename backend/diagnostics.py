"""Engineering checks on a finished steady/extended-period design result.

The checks only read values that the EPANET run already produced, plus the
document's own pump curves and settings.  They do not change the solution and
they do not replace a validated design review; thresholds are common
rules of thumb and are stored with every finding so the reader can judge them.
"""

from __future__ import annotations

import math

from .models import G, NetworkDocument
from .physics import fluid

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
SEVERITY_NAMES = {"error": "严重", "warning": "警告", "info": "提示"}

# Rule-of-thumb limits; kept in one place so the GUI can display them.
LIMITS = {
    "velocity_high_ms": 2.5,
    "velocity_low_ms": 0.1,
    "bep_low_ratio": 0.6,
    "bep_high_ratio": 1.2,
    "npsh_margin_ratio": 1.3,
    "npsh_margin_m": 0.5,
    "tank_band_m": 0.05,
}


def _interp(points, x):
    """Linear interpolation on (x, y) pairs sorted by x; clamps at the ends."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 > x0 else y1
    return points[-1][1]


def pump_curve_bep(link, rho=998.2):
    """Best-efficiency point of a reference curve at speed ratio 1.

    Returns ``(flow_m3h, head_m, efficiency)``.  Efficiency is the hydraulic
    power divided by the curve's shaft power, so it is only as good as the curve.
    """
    best = (0.0, link.curve[0].head_m, 0.0)
    for p in link.curve:
        if p.flow_m3h <= 0:
            continue
        eta = rho * G * p.flow_m3h / 3600 * p.head_m / (p.shaft_kw * 1000)
        if eta > best[2]:
            best = (p.flow_m3h, p.head_m, eta)
    return best


def npsh_required(link, flow_m3h, speed_ratio):
    """NPSH3 from the reference curve, scaled by the affinity law (∝ n²)."""
    s = max(speed_ratio, 1e-6)
    points = [(p.flow_m3h, p.npsh3_m) for p in link.curve]
    return _interp(points, flow_m3h / s) * s * s


def _finding(findings, key, severity, category, kind, target, hour, message, value, limit):
    """Keep one finding per (rule, element), remembering first time and worst value."""
    current = findings.get(key)
    if current is None:
        findings[key] = {
            "severity": severity,
            "category": category,
            "target_kind": kind,
            "target": target,
            "first_hour": hour,
            "count": 1,
            "message": message,
            "value": value,
            "limit": limit,
        }
        return
    current["count"] += 1
    worse = value < current["value"] if category in ("低压", "汽蚀余量", "水箱") else value > current["value"]
    if worse:
        current["value"] = value
        current["message"] = message
    if SEVERITY_ORDER[severity] < SEVERITY_ORDER[current["severity"]]:
        current["severity"] = severity


def diagnose(result: dict) -> dict:
    """Return ``{"findings": [...], "metrics": {...}, "limits": LIMITS}``."""
    doc = NetworkDocument.model_validate(result["document"])
    settings = doc.settings
    rho, _, vapor_pa = fluid(settings.temperature_c)
    atmosphere_m = settings.atmospheric_kpa * 1000 / (rho * G)
    vapor_m = vapor_pa / (rho * G)
    nodes = {n.id: n for n in doc.nodes}
    links = {l.id: l for l in doc.links}
    findings: dict = {}
    pump_energy = {}
    pump_hydraulic = {}
    points = result.get("points") or []
    step_h = settings.step_seconds / 3600
    for index, point in enumerate(points):
        hour = point["hour"]
        weight = step_h if index < len(points) - 1 else 0.0
        for nid, state in point["nodes"].items():
            node = nodes.get(nid)
            if node is None or node.kind == "reservoir":
                continue
            pressure = state.get("pressure_m")
            if node.kind == "junction" and pressure is None:
                continue
            if node.kind != "junction":
                pass
            elif pressure < 0:
                _finding(
                    findings, ("neg", nid), "error", "低压", "node", nid, hour,
                    f"{nid} 出现负压 {pressure:.2f} m，可能吸入空气或产生汽化",
                    pressure, 0.0,
                )
            elif node.demand_m3h > 0 and pressure < settings.required_pressure_m - 0.05:
                _finding(
                    findings, ("low", nid), "warning", "低压", "node", nid, hour,
                    f"{nid} 压力 {pressure:.2f} m 低于所需 {settings.required_pressure_m:.1f} m",
                    pressure, settings.required_pressure_m,
                )
            requested = state.get("requested_m3h") or 0
            delivered = state.get("demand_m3h") or 0
            if requested > 0 and requested - delivered > max(0.005, requested * 0.01):
                _finding(
                    findings, ("unmet", nid), "error", "缺水", "node", nid, hour,
                    f"{nid} 仅供水 {delivered:.2f} / {requested:.2f} m³/h",
                    requested - delivered, 0.0,
                )
            if node.kind == "tank" and state.get("tank_level_m") is not None:
                level = state["tank_level_m"]
                if level <= node.tank_min_m + LIMITS["tank_band_m"]:
                    _finding(
                        findings, ("tank_low", nid), "warning", "水箱", "node", nid, hour,
                        f"{nid} 液位 {level:.2f} m 触及最低液位", level, node.tank_min_m,
                    )
        for lid, state in point["links"].items():
            link = links.get(lid)
            if link is None or not state.get("active", True):
                continue
            flow = abs(state.get("flow_m3h") or 0)
            if link.kind == "pipe":
                v = abs(state.get("velocity_ms") or 0)
                if v > LIMITS["velocity_high_ms"]:
                    _finding(
                        findings, ("vhigh", lid), "warning", "流速", "link", lid, hour,
                        f"{lid} 流速 {v:.2f} m/s 偏高，水头损失与水锤风险增大，可考虑增大管径",
                        v, LIMITS["velocity_high_ms"],
                    )
                elif 1e-4 < flow and v < LIMITS["velocity_low_ms"]:
                    _finding(
                        findings, ("vlow", lid), "info", "流速", "link", lid, hour,
                        f"{lid} 流速 {v:.3f} m/s 很低，长期滞流需关注水质",
                        -v, -LIMITS["velocity_low_ms"],
                    )
            elif link.kind == "pump":
                rpm = state.get("rpm") or 0
                speed = rpm / link.nominal_rpm if link.nominal_rpm else link.speed_ratio
                if speed <= 0:
                    continue
                pump_energy[lid] = pump_energy.get(lid, 0.0) + (state.get("power_kw") or 0) * weight
                head = state.get("head_m") or 0
                pump_hydraulic[lid] = (
                    pump_hydraulic.get(lid, 0.0) + rho * G * flow / 3600 * head / 1000 * weight
                )
                bep_q, _, bep_eta = pump_curve_bep(link, rho)
                bep_q *= speed
                if flow < 1e-3:
                    _finding(
                        findings, ("dead", lid), "warning", "泵工况", "link", lid, hour,
                        f"{lid} 运行但几乎零流量（关死点），会快速发热",
                        0.0, bep_q,
                    )
                elif bep_q > 0:
                    ratio = flow / bep_q
                    if ratio < LIMITS["bep_low_ratio"] or ratio > LIMITS["bep_high_ratio"]:
                        _finding(
                            findings, ("bep", lid), "warning", "泵工况", "link", lid, hour,
                            f"{lid} 流量为最高效率点的 {ratio * 100:.0f}%"
                            f"（建议 {LIMITS['bep_low_ratio'] * 100:.0f}–"
                            f"{LIMITS['bep_high_ratio'] * 100:.0f}%），效率 "
                            f"{(state.get('efficiency') or 0) * 100:.0f}% / 最高 {bep_eta * 100:.0f}%",
                            abs(ratio - 1), 0.0,
                        )
                shaft = state.get("shaft_kw") or 0
                if shaft > link.motor_rated_kw:
                    _finding(
                        findings, ("overload", lid), "error", "电机", "link", lid, hour,
                        f"{lid} 轴功率 {shaft:.2f} kW 超过电机额定 {link.motor_rated_kw:.2f} kW",
                        shaft, link.motor_rated_kw,
                    )
                suction = point["nodes"].get(link.start, {}).get("pressure_m")
                if suction is not None:
                    npsha = atmosphere_m + suction - vapor_m
                    npshr = npsh_required(link, flow, speed)
                    need = max(npshr * LIMITS["npsh_margin_ratio"], npshr + LIMITS["npsh_margin_m"])
                    if npsha < npshr:
                        _finding(
                            findings, ("npsh", lid), "error", "汽蚀余量", "link", lid, hour,
                            f"{lid} NPSHa {npsha:.2f} m < NPSH3 {npshr:.2f} m，扬程将明显下降",
                            npsha, npshr,
                        )
                    elif npsha < need:
                        _finding(
                            findings, ("npsh", lid), "warning", "汽蚀余量", "link", lid, hour,
                            f"{lid} NPSHa {npsha:.2f} m，裕量不足（建议 ≥ {need:.2f} m）",
                            npsha, need,
                        )
    ordered = sorted(
        findings.values(),
        key=lambda f: (SEVERITY_ORDER[f["severity"]], f["first_hour"], f["target"]),
    )
    summary = result.get("summary", {})
    delivered = summary.get("delivered_m3") or 0
    energy = summary.get("energy_kwh") or 0
    electric = sum(pump_energy.values())
    metrics = {
        "specific_energy_kwh_m3": energy / delivered if delivered > 1e-9 else None,
        "wire_to_water_efficiency": (
            sum(pump_hydraulic.values()) / electric if electric > 1e-12 else None
        ),
        "npsh_basis": "NPSHa = 大气压头 + 吸入节点表压头 − 汽化压头（不含速度头，偏保守）",
        "error_count": sum(f["severity"] == "error" for f in ordered),
        "warning_count": sum(f["severity"] == "warning" for f in ordered),
        "info_count": sum(f["severity"] == "info" for f in ordered),
    }
    return {"findings": ordered, "metrics": metrics, "limits": dict(LIMITS)}


def format_metric(value, digits=3, scale=1.0, suffix=""):
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    return f"{value * scale:.{digits}f}{suffix}"
