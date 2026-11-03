"""Explicit sensor mapping for arbitrary-network offline calibration."""

import csv
import io
import math
from datetime import datetime

import numpy as np
from scipy.optimize import minimize_scalar
from .physics import simulate_design


def parse_observations(text):
    if len(text) > 2_000_000:
        raise ValueError("观测 CSV 最大 2 MB")
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    rows = []
    first = None
    last = None
    if "timestamp" not in (reader.fieldnames or []):
        raise ValueError("需要带时区的 timestamp 列")
    for row in reader:
        if len(rows) >= 5000:
            raise ValueError("最多 5000 条记录")
        stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or (last is not None and stamp <= last):
            raise ValueError("时间戳必须包含时区且严格递增")
        first = first or stamp
        last = stamp
        value = {"seconds": (stamp - first).total_seconds()}
        for key in ("pressure_m", "flow_m3h", "power_kw"):
            if row.get(key, "").strip():
                n = float(row[key])
                if not math.isfinite(n):
                    raise ValueError("观测数据不能包含 NaN 或无穷值")
                value[key] = n
        if len(value) < 2:
            raise ValueError("每行至少包含一种测量量")
        rows.append(value)
    if len(rows) < 2:
        raise ValueError("至少需要两条观测")
    return rows


def observation_report(result, rows, node, link):
    if node not in result["points"][0]["nodes"] or link not in result["points"][0]["links"]:
        raise ValueError("测点映射不存在，请选择节点和设备")
    lookup = {p["seconds"]: p for p in result["points"]}
    residuals = []
    metrics = {}
    for row in rows:
        if row["seconds"] not in lookup:
            continue
        p = lookup[row["seconds"]]
        predicted = {
            "pressure_m": p["nodes"][node]["pressure_m"],
            "flow_m3h": p["links"][link]["flow_m3h"],
            "power_kw": p["power_kw"],
        }
        residuals.append(
            {
                "seconds": row["seconds"],
                **{key: row[key] - predicted[key] for key in predicted if key in row},
            }
        )
    if not residuals:
        raise ValueError("观测与报告时刻没有精确匹配；首条观测对应 t=0")
    for key in ("pressure_m", "flow_m3h", "power_kw"):
        values = [r[key] for r in residuals if key in r]
        if values:
            metrics[key] = {
                "n": len(values),
                "bias": float(np.mean(values)),
                "rmse": float(np.sqrt(np.mean(np.square(values)))),
            }
    return {
        "metrics": metrics,
        "matched_rows": len(residuals),
        "unmatched_rows": len(rows) - len(residuals),
        "residuals": residuals,
        "sensor_mapping": {"pressure_node": node, "flow_link": link, "power": "全泵组电输入功率"},
        "notice": "按报告时刻精确匹配；来源由导入者声明，不构成实机或故障验证。",
    }


def calibrate_design(doc, options, progress=None):
    rows = parse_observations(options["observation_csv"])
    pump_id = options["calibration_pump"]
    node = options["measurement_node"]
    link = options["measurement_link"]
    if len(rows) < 10:
        raise ValueError("校准至少需要 10 条观测")
    if doc.settings.events:
        raise ValueError("单参数校准前请移除显式控制事件")
    pump = next((l for l in doc.links if l.id == pump_id and l.kind == "pump"), None)
    if pump is None:
        raise ValueError("请选择一个有效泵作为待校准设备")
    available = set.intersection(*[set(r) - {"seconds"} for r in rows])
    if "pressure_m" not in available or len(available) < 2:
        raise ValueError("每行需包含压力，以及流量或电功率")
    before = simulate_design(doc)
    report = observation_report(before, rows, node, link)
    if report["unmatched_rows"]:
        raise ValueError("校准需要全部观测精确匹配报告时刻")
    split = max(6, int(len(rows) * 0.7))
    sigma = {"pressure_m": 0.25, "flow_m3h": 0.15, "power_kw": 0.03}
    count = 0

    def loss(result, subset):
        mapping = {
            r["seconds"]: r for r in observation_report(result, subset, node, link)["residuals"]
        }
        return float(
            np.mean(
                [
                    (mapping[row["seconds"]][key] / sigma[key]) ** 2
                    for row in subset
                    for key in available
                ]
            )
        )

    def candidate(factor):
        copy = doc.model_copy(deep=True)
        target = next(l for l in copy.links if l.id == pump_id)
        for p in target.curve:
            p.head_m *= factor
            p.shaft_kw *= factor
        return copy

    def objective(factor):
        nonlocal count
        count += 1
        if progress:
            progress(min(0.85, 0.1 + count * 0.03), f"单泵曲线系数校准 · 评估 {count}")
        return loss(simulate_design(candidate(factor)), rows[:split])

    fit = minimize_scalar(
        objective, bounds=(0.7, 1.1), method="bounded", options={"xatol": 1e-4, "maxiter": 24}
    )
    if not fit.success:
        raise ValueError("校准未收敛")
    calibrated = candidate(float(fit.x))
    calibrated.name = (doc.name + " · 校准")[:100]
    after = simulate_design(calibrated)
    return {
        "document": calibrated.model_dump(mode="json"),
        "pump": pump_id,
        "curve_factor": float(fit.x),
        "training_rows": split,
        "holdout_rows": len(rows) - split,
        "training_mse_before": loss(before, rows[:split]),
        "training_mse_after": loss(after, rows[:split]),
        "holdout_mse_before": loss(before, rows[split:]),
        "holdout_mse_after": loss(after, rows[split:]),
        "source": options.get("observation_source", "measured"),
        "source_note": options.get("source_note", ""),
        "report": observation_report(after, rows, node, link),
        "notice": "一次仅拟合一台泵的曲线倍率（扬程与轴功率同步缩放），其余参数固定；同一记录后 30% 留出，不等于跨工况独立验证。",
    }
