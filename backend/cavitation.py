"""Measured/CFD head-breakdown maps with explicit provenance and no extrapolation."""

import csv
import io
import math
from typing import Literal

import numpy as np
from pydantic import Field, model_validator
from scipy.interpolate import PchipInterpolator, LinearNDInterpolator

from .models import G, StrictModel


class CavitationPoint(StrictModel):
    flow_m3h: float = Field(gt=0)
    rpm: float = Field(gt=0)
    npsha_m: float = Field(ge=0)
    head_m: float = Field(ge=0)
    noncavitating_head_m: float = Field(gt=0)
    shaft_kw: float | None = Field(None, gt=0)


class CavitationMap(StrictModel):
    schema_version: Literal["flowstate-cavitation-1"] = "flowstate-cavitation-1"
    pump_id: str = "P1"
    source: str = "cfd"
    source_note: str
    validated_on_real_asset: bool = False
    numerical_acceptance: Literal["unreviewed", "passed", "failed"] = "unreviewed"
    validation_notes: list[str] = Field(default_factory=list)
    points: list[CavitationPoint] = Field(min_length=4, max_length=10000)

    @model_validator(mode="after")
    def checks(self):
        if self.source not in ("cfd", "measured", "synthetic"):
            raise ValueError("汽蚀图谱来源需为 cfd/measured/synthetic")
        if len({(p.flow_m3h, p.rpm, p.npsha_m) for p in self.points}) != len(self.points):
            raise ValueError("同一流量、转速、汽蚀余量下不能重复记录")
        for point in self.points:
            if point.head_m > point.noncavitating_head_m * 1.02:
                raise ValueError("汽蚀工况扬程高于基准超过2%，请核对基准、平均时间段和测量误差")
        return self


def npsha(
    absolute_pressure_pa,
    velocity_ms,
    density_kgm3,
    vapour_pressure_pa,
    suction_elevation_m=0,
    datum_elevation_m=0,
):
    if not all(
        math.isfinite(v)
        for v in (
            absolute_pressure_pa,
            velocity_ms,
            density_kgm3,
            vapour_pressure_pa,
            suction_elevation_m,
            datum_elevation_m,
        )
    ):
        raise ValueError("汽蚀余量输入必须为有限值")
    if absolute_pressure_pa <= 0 or density_kgm3 <= 0 or vapour_pressure_pa <= 0:
        raise ValueError("必须使用正的绝对压力、密度与饱和蒸汽压")
    return (
        (absolute_pressure_pa - vapour_pressure_pa) / (density_kgm3 * G)
        + velocity_ms**2 / (2 * G)
        + suction_elevation_m
        - datum_elevation_m
    )


def parse_map_csv(text, pump_id, source, source_note):
    rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    points = []
    for row in rows:
        values = {
            k: float(row[k])
            for k in ("flow_m3h", "rpm", "npsha_m", "head_m", "noncavitating_head_m")
        }
        if row.get("shaft_kw", "").strip():
            values["shaft_kw"] = float(row["shaft_kw"])
        points.append(CavitationPoint(**values))
    return CavitationMap(pump_id=pump_id, source=source, source_note=source_note, points=points)


class HeadBreakdown:
    def __init__(self, mapping: CavitationMap):
        self.mapping = mapping
        self.groups = {}
        for p in mapping.points:
            self.groups.setdefault((p.flow_m3h, p.rpm), []).append(p)
        self.interpolators = {}
        for key, points in self.groups.items():
            points.sort(key=lambda p: p.npsha_m)
            if len(points) < 4:
                raise ValueError("每组固定流量/转速至少需要4个汽蚀余量点")
            self.interpolators[key] = PchipInterpolator(
                [p.npsha_m for p in points],
                [p.head_m / p.noncavitating_head_m for p in points],
                extrapolate=False,
            )

    def ratio(self, flow_m3h, rpm, available_m):
        key = (flow_m3h, rpm)
        if key in self.interpolators:
            value = float(self.interpolators[key](available_m))
        else:
            samples, ratios = [], []
            for (q, n), fun in self.interpolators.items():
                r = float(fun(available_m))
                if math.isfinite(r):
                    samples.append((q, n))
                    ratios.append(r)
            if len(samples) < 2:
                raise ValueError("汽蚀图谱没有覆盖该流量、转速和汽蚀余量；禁止外推")
            samples = np.asarray(samples)
            if len(np.unique(samples[:, 1])) == 1 and math.isclose(rpm, samples[0, 1]):
                order = np.argsort(samples[:, 0])
                xs = samples[order, 0]
                value = (
                    float(np.interp(flow_m3h, xs, np.asarray(ratios)[order]))
                    if xs[0] <= flow_m3h <= xs[-1]
                    else math.nan
                )
            else:
                scale = np.ptp(samples, axis=0)
                if len(samples) < 3 or np.any(scale == 0):
                    raise ValueError("图谱维度不足，无法进行此工况插值")
                value = float(
                    LinearNDInterpolator(samples / scale, ratios)(
                        np.array([flow_m3h, rpm]) / scale
                    ).item()
                )
        if not math.isfinite(value):
            raise ValueError("汽蚀工况位于有效图谱范围之外；禁止外推")
        return max(0, float(value))

    def npsh3(self):
        from scipy.optimize import brentq

        values = []
        for (q, n), fun in self.interpolators.items():
            points = self.groups[(q, n)]
            crossings = []
            for a, b in zip(points, points[1:]):
                fa, fb = float(fun(a.npsha_m) - 0.97), float(fun(b.npsha_m) - 0.97)
                if fa * fb <= 0:
                    crossings.append(
                        float(brentq(lambda x: float(fun(x) - 0.97), a.npsha_m, b.npsha_m))
                    )
            values.append(
                {
                    "flow_m3h": q,
                    "rpm": n,
                    "npsh3_m": max(crossings) if crossings else None,
                    "notice": "3%扬程下降判据，不代表汽蚀初生",
                }
            )
        return values
