"""Reproducible comparison on overlapping physical time, without hidden time shifts."""

import csv
import hashlib
import io
import math
import numpy as np

UNITS = {
    "pressure_pa": "Pa absolute",
    "flow_m3h": "m3/h",
    "head_m": "m",
    "force_x_n": "N",
    "force_y_n": "N",
    "force_z_n": "N",
    "moment_x_nm": "N m",
    "moment_y_nm": "N m",
    "moment_z_nm": "N m",
    "rpm": "rpm",
}


def parse_measurements(text, source_note):
    if not source_note.strip():
        raise ValueError("实测文件需要填写仪器/测点/压力参考及来源说明")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if "seconds" not in (reader.fieldnames or []):
        raise ValueError("测量CSV必须包含seconds列，单位秒")
    fields = [k for k in reader.fieldnames if k in UNITS]
    if not fields:
        raise ValueError("测量CSV需包含pressure_pa/flow_m3h/head_m/force_x_n等明确单位列")
    rows = []
    for row in reader:
        value = {k: float(row[k]) for k in ["seconds", *fields]}
        if not all(math.isfinite(v) for v in value.values()):
            raise ValueError("测量数据含缺失或非有限值")
        if "pressure_pa" in value and value["pressure_pa"] <= 0:
            raise ValueError("pressure_pa必须为正的绝对压力，请先把表压转换为绝压")
        rows.append(value)
    if len(rows) < 4 or any(a["seconds"] >= b["seconds"] for a, b in zip(rows, rows[1:])):
        raise ValueError("至少4个采样，时间严格递增")
    return {
        "points": rows,
        "units": {k: UNITS[k] for k in fields},
        "source_note": source_note,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "provenance": "user_declared_measurement",
        "independently_authenticated": False,
    }


def compare_series(measurements, simulation, tolerances=None):
    measured = measurements["points"]
    t = np.array([r["seconds"] for r in simulation], float)
    if len(t) < 4 or np.any(np.diff(t) <= 0):
        raise ValueError("仿真采样至少4点且时间严格递增")
    if not all(math.isfinite(v) for r in simulation for v in r.values()):
        raise ValueError("仿真数据含非有限值")
    times = np.array([r["seconds"] for r in measured])
    keep = (times >= t[0]) & (times <= t[-1])
    if keep.sum() < 4:
        raise ValueError("仿真与测量重叠时间不足4个测量点；不允许时间外推")
    metrics = {}
    for field in measurements["units"]:
        selected = [r for r in simulation if field in r]
        if len(selected) < 4:
            continue
        ft = np.asarray([r["seconds"] for r in selected])
        fkeep = (times >= ft[0]) & (times <= ft[-1])
        if fkeep.sum() < 4:
            continue
        obs = np.array([r[field] for r in measured])[fkeep]
        pred = np.interp(times[fkeep], ft, [r[field] for r in selected])
        error = pred - obs
        rmse = float(np.sqrt(np.mean(error**2)))
        metrics[field] = {
            "samples": int(fkeep.sum()),
            "rmse": rmse,
            "bias": float(error.mean()),
            "maximum_absolute_error": float(abs(error).max()),
            "peak_time_error_s": float(
                times[fkeep][np.argmax(pred)] - times[fkeep][np.argmax(obs)]
            ),
            "unit": UNITS[field],
        }
        if tolerances and field in tolerances:
            limit = float(tolerances[field])
            if limit <= 0:
                raise ValueError("RMSE容许误差必须为正")
            metrics[field].update({"rmse_tolerance": limit, "passed": rmse <= limit})
    if not metrics:
        raise ValueError("仿真与测量没有同名物理量，请先选择测点/载荷轴")
    return {
        "metrics": metrics,
        "time_overlap_s": [float(times[keep][0]), float(times[keep][-1])],
        "excluded_measurements": int((~keep).sum()),
        "measurements": measurements,
        "time_shift_s": 0,
        "notice": "使用原始物理时间，不自动对齐峰值；来源为用户声明，未据此自动标记整机已验证。",
    }


def result_series(result, probe_index=0, node_id="", device_id=""):
    if "cfd" in result:
        return result_series(result["cfd"], probe_index, node_id, device_id)
    if "frames" in result:
        values = {}
        for row in result.get("performance", []):
            values.setdefault(row["seconds"], {"seconds": row["seconds"]}).update(
                {k: row[k] for k in ("flow_m3h", "head_m")}
            )
        for row in result.get("loads", []):
            point = values.setdefault(row["seconds"], {"seconds": row["seconds"]})
            for i, axis in enumerate("xyz"):
                point["force_" + axis + "_n"] = row["force_n"][i]
                point["moment_" + axis + "_nm"] = row["moment_nm"][i]
        for row in result.get("pressure_probes", []):
            if not 0 <= probe_index < len(row["pressure_pa"]):
                raise ValueError("测压点编号超出范围")
            values.setdefault(row["seconds"], {"seconds": row["seconds"]})["pressure_pa"] = row[
                "pressure_pa"
            ][probe_index]
        # Different solver outputs have different sampling grids; retain each
        # measured quantity at its own actual timestamps.
        return [values[t] for t in sorted(values) if t > 0]
    rows = []
    for frame in result.get("points", []):
        row = {"seconds": frame["seconds"]}
        if node_id in frame.get("nodes", {}):
            row["pressure_pa"] = frame["nodes"][node_id]["pressure_pa"]
            row["head_m"] = frame["nodes"][node_id]["head_m"]
        if device_id in frame.get("devices", {}):
            row["flow_m3h"] = frame["devices"][device_id]["flow_m3h"]
        rows.append(row)
    return rows
