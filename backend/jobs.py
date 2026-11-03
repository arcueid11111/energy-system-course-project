"""Offline simulation, optimization and calibration tasks."""

import itertools

from .models import NetworkDocument
from .physics import simulate_design


def optimize_design(doc, progress=None):
    if doc.settings.events:
        raise ValueError("转速搜索前请移除控制事件，避免事件覆盖候选设定")
    pumps = [l for l in doc.links if l.kind == "pump"]
    if not pumps:
        raise ValueError("当前管网没有泵可优化")
    if len(pumps) > 4:
        raise ValueError("当前优化支持最多 4 台泵；更多设备可使用批量仿真")
    baseline = simulate_design(doc)
    modes = [mode for mode in itertools.product((False, True), repeat=len(pumps)) if any(mode)]
    speeds = [0.7, 0.8, 0.9, 1.0, 1.1]
    total = len(modes) * len(speeds)
    evaluated = []
    best = None
    for mode in modes:
        for speed in speeds:
            if progress:
                progress(
                    0.05 + 0.85 * len(evaluated) / total, f"优化候选 {len(evaluated) + 1}/{total}"
                )
            candidate = doc.model_copy(deep=True)
            candidate.name = f"{doc.name} · {sum(mode)} 泵 {speed:.2f} 倍速"
            lookup = {p.id: (enabled, speed) for p, enabled in zip(pumps, mode)}
            for l in candidate.links:
                if l.id in lookup:
                    l.enabled, l.speed_ratio = lookup[l.id]
            reasons = []
            try:
                result = simulate_design(candidate)
                if result["summary"]["unmet_m3"] > 0.005:
                    reasons.append("供水不足")
                if result["summary"]["delivered_m3"] < baseline["summary"]["delivered_m3"] * 0.999:
                    reasons.append("供水量低于基准")
                if any(
                    p["min_pressure_m"] is not None
                    and p["min_pressure_m"] < doc.settings.required_pressure_m - 0.05
                    for p in result["points"]
                ):
                    reasons.append("末端压力不足")
                for n in doc.nodes:
                    if (
                        n.kind == "tank"
                        and result["points"][-1]["nodes"][n.id]["tank_level_m"]
                        < baseline["points"][-1]["nodes"][n.id]["tank_level_m"] - 0.02
                    ):
                        reasons.append("期末储水低于基准")
                reasons.extend(result["warnings"])
                if not reasons and (
                    best is None or result["summary"]["energy_kwh"] < best["summary"]["energy_kwh"]
                ):
                    best = result
                evaluated.append(
                    {
                        "mode": list(mode),
                        "speed_ratio": speed,
                        "energy_kwh": result["summary"]["energy_kwh"],
                        "feasible": not reasons,
                        "reasons": sorted(set(reasons)),
                    }
                )
            except Exception as exc:
                evaluated.append(
                    {
                        "mode": list(mode),
                        "speed_ratio": speed,
                        "feasible": False,
                        "reasons": [str(exc)],
                    }
                )
    return {
        "best": best,
        "baseline_summary": baseline["summary"],
        "candidates": evaluated,
        "notice": "有限网格搜索，不保证全局最优；尚未做实机验证与 NPSH 约束校核。",
    }


def execute_design_task(kind, payload, progress, save):
    doc = NetworkDocument.model_validate(payload["document"])
    if kind == "design_calibration":
        from .observations import calibrate_design

        result = calibrate_design(doc, payload, progress)
        progress(0.95, "保存校准与来源记录")
        saved = save("design_calibration", doc.name, result)
        return {"design_calibration_id": saved["id"], "curve_factor": result["curve_factor"]}
    if kind == "design_simulation":
        result = simulate_design(doc, progress)
        progress(0.95, "保存网络快照和计算结果")
        saved = save("design_run", doc.name, result)
        return {"design_run_id": saved["id"], "summary": saved["summary"]}
    if kind == "design_optimization":
        result = optimize_design(doc, progress)
        progress(0.95, "保存优化结果")
        if result["best"]:
            result["best"] = save("design_run", result["best"]["document"]["name"], result["best"])
        saved = save("design_optimization", doc.name, result)
        return {
            "design_optimization_id": saved["id"],
            "design_run_id": result["best"]["id"] if result["best"] else None,
        }
    raise ValueError("未知自定义管网任务")
