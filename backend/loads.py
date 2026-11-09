"""Surface-integrated CFD loads and time/frequency analysis, in SI units."""

import math
import re
from pathlib import Path

import numpy as np

NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?")


def numeric_rows(path):
    rows = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        values = [float(x) for x in NUMBER.findall(line)]
        if values and all(math.isfinite(v) for v in values):
            rows.append(values)
    return rows


def read_forces(case):
    by_time = {}
    for path in sorted((Path(case) / "postProcessing").glob("forces/*/forces.dat")):
        for row in numeric_rows(path):
            if len(row) not in (13, 19):
                raise ValueError(f"不支持的载荷格式（应含时间+12或18个分量）：{path}")
            parts = np.asarray(row[1:]).reshape(2, (len(row) - 1) // 6, 3)
            force, moment = parts.sum(axis=1)
            by_time[row[0]] = {
                "seconds": row[0],
                "force_n": force.tolist(),
                "moment_nm": moment.tolist(),
                "pressure_force_n": parts[0, 0].tolist(),
                "viscous_force_n": parts[0, 1].tolist(),
            }
    return [by_time[t] for t in sorted(by_time)]


def signal_metrics(times, values, discard_fraction=0.3):
    t, y = np.asarray(times, dtype=float), np.asarray(values, dtype=float)
    if t.ndim != 1 or y.shape != t.shape or len(t) < 4:
        raise ValueError("时序至少需要四个标量采样")
    if not np.all(np.isfinite(t)) or not np.all(np.isfinite(y)) or np.any(np.diff(t) <= 0):
        raise ValueError("时间需严格递增，数据需为有限值")
    if not 0 <= discard_fraction <= 0.8:
        raise ValueError("启动段舍弃比例需为0–0.8")
    keep = min(int(len(t) * discard_fraction), len(t) - 4)
    tt, yy = t[keep:], y[keep:]
    dt = float(np.median(np.diff(tt)))
    max_gap = float(np.max(np.diff(tt)))
    if max_gap > 4 * dt:
        raise ValueError("时序采样间隙过大，不能可靠计算FFT")
    uniform_t = np.arange(tt[0], tt[-1] + dt * 0.01, dt)
    uniform_y = np.interp(uniform_t, tt, yy)
    window = np.hanning(len(uniform_y))
    mean = float(np.mean(uniform_y))
    amplitude = 2 * np.abs(np.fft.rfft((uniform_y - mean) * window)) / window.sum()
    frequency = np.fft.rfftfreq(len(uniform_y), dt)
    peak = int(np.argmax(amplitude[1:])) + 1
    return {
        "mean": mean,
        "rms_fluctuation": float(np.sqrt(np.mean((uniform_y - mean) ** 2))),
        "minimum": float(np.min(yy)),
        "maximum": float(np.max(yy)),
        "peak_to_peak": float(np.ptp(yy)),
        "impulse_above_initial": float(np.trapezoid(y - y[0], t)),
        "dominant_hz": float(frequency[peak]),
        "frequency_resolution_hz": float(1 / (len(uniform_y) * dt)),
        "nyquist_hz": float(0.5 / dt),
        "discarded_samples": keep,
        "resampled": bool(np.max(np.abs(np.diff(tt) - dt)) > dt * 1e-6),
        "spectrum": [{"hz": float(f), "amplitude": float(a)} for f, a in zip(frequency, amplitude)],
    }


def analyze_loads(rows, rpm=None, blade_count=None, discard_fraction=0.3):
    if len(rows) < 4:
        return {"samples": len(rows), "notice": "载荷记录不足四帧，尚不能进行频谱分析"}
    times = [r["seconds"] for r in rows]
    result = {"samples": len(rows), "axes": {}}
    for key, vector_key in [("force", "force_n"), ("moment", "moment_nm")]:
        for axis, i in zip("xyz", range(3)):
            result["axes"][f"{key}_{axis}"] = signal_metrics(
                times, [r[vector_key][i] for r in rows], discard_fraction
            )
    if rpm and blade_count:
        result["blade_passing_hz"] = rpm * blade_count / 60
    return result


def surface_load(
    areas, normals, pressure_pa, shear_pa, reference_pa=0, centers=None, origin=(0, 0, 0)
):
    """Independent traction integral; normals point from the solid into the fluid."""
    a, n, p, tau = map(np.asarray, (areas, normals, pressure_pa, shear_pa))
    if n.shape != (len(a), 3) or p.shape != a.shape or tau.shape != n.shape:
        raise ValueError("表面载荷数组维度不一致")
    if np.any(a <= 0) or not all(np.all(np.isfinite(v)) for v in (a, n, p, tau)):
        raise ValueError("表面面积需为正，载荷数据需为有限值")
    if not np.allclose(np.linalg.norm(n, axis=1), 1, atol=1e-5):
        raise ValueError("表面法向必须归一化")
    traction = (-(p - reference_pa)[:, None] * n + tau) * a[:, None]
    result = {"force_n": traction.sum(axis=0).tolist()}
    if centers is not None:
        centers = np.asarray(centers)
        if centers.shape != n.shape:
            raise ValueError("面心数组维度不一致")
        result["moment_nm"] = np.cross(centers - np.asarray(origin), traction).sum(axis=0).tolist()
    return result
