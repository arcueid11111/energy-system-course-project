"""Validated offline engineering document; all values use SI units unless named otherwise."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

G = 9.80665


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CurvePoint(StrictModel):
    flow_m3h: float = Field(ge=0)
    head_m: float = Field(gt=0)
    shaft_kw: float = Field(gt=0)
    npsh3_m: float = Field(gt=0)


def default_pump_curve():
    # Hypothetical reference data, never a validated manufacturer curve.
    values = [
        (0, 32, 0.95, 1.4),
        (5, 30, 0.84972, 1.5),
        (10, 26, 1.07118, 1.8),
        (15, 19, 1.10708, 2.4),
        (20, 9, 0.87404, 3.3),
    ]
    return [CurvePoint(flow_m3h=q, head_m=h, shaft_kw=p, npsh3_m=n) for q, h, p, n in values]


ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,30}$"


class DesignNode(StrictModel):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["junction", "reservoir", "tank"] = "junction"
    x: float = 0
    y: float = 0
    z: float = 0
    elevation_m: float = 0
    demand_m3h: float = Field(0, ge=0, le=1e6)
    source_head_m: float = 0
    tank_level_m: float = Field(5, ge=0)
    tank_min_m: float = Field(0, ge=0)
    tank_max_m: float = Field(10, gt=0)
    tank_diameter_m: float = Field(3, gt=0)
    note: str = ""

    @model_validator(mode="after")
    def tank_bounds(self):
        if self.kind == "tank" and not self.tank_min_m <= self.tank_level_m <= self.tank_max_m:
            raise ValueError("水箱初始液位必须位于最小和最大液位之间")
        if self.kind == "tank" and self.tank_min_m >= self.tank_max_m:
            raise ValueError("水箱最大液位必须大于最小液位")
        return self


class DesignLink(StrictModel):
    id: str = Field(pattern=ID_PATTERN)
    kind: Literal["pipe", "pump", "valve"] = "pipe"
    start: str
    end: str
    length_m: float = Field(50, gt=0, le=1e7)
    diameter_m: float = Field(0.05, gt=0, le=100)
    roughness_m: float = Field(0.0001, ge=0, le=1)
    minor_loss: float = Field(0, ge=0, le=1e7)
    enabled: bool = True
    check_valve: bool = False
    opening: float = Field(1, ge=0, le=1)
    speed_ratio: float = Field(1, ge=0.2, le=1.2)
    nominal_rpm: float = Field(2900, gt=0)
    motor_efficiency: float = Field(0.87, gt=0, le=1)
    drive_efficiency: float = Field(0.96, gt=0, le=1)
    motor_rated_kw: float = Field(2.2, gt=0)
    curve: list[CurvePoint] = Field(default_factory=default_pump_curve, min_length=3, max_length=50)
    geometry: list[tuple[float, float, float]] = Field(default_factory=list, max_length=5000)
    source: Literal["hypothetical", "manufacturer", "measured", "cad_geometry"] = "hypothetical"
    note: str = ""

    @model_validator(mode="after")
    def validate_curve(self):
        if self.kind == "pump":
            if self.curve[0].flow_m3h != 0 or any(
                a.flow_m3h >= b.flow_m3h or a.head_m <= b.head_m
                for a, b in zip(self.curve, self.curve[1:])
            ):
                raise ValueError("泵曲线应从零流量开始，流量递增且扬程递减")
            if any(
                998.2 * G * p.flow_m3h / 3600 * p.head_m > p.shaft_kw * 1000 for p in self.curve
            ):
                raise ValueError("泵曲线的水力功率超过轴功率，请检查单位")
        return self


class DesignEvent(StrictModel):
    hour: float = Field(ge=0)
    target: str
    kind: Literal["enabled", "speed", "opening"]
    value: float


class DesignSettings(StrictModel):
    duration_hours: float = Field(1, ge=0.05, le=168)
    step_seconds: int = Field(300, ge=10, le=3600)
    demand_multiplier: float = Field(1, ge=0.01, le=10)
    required_pressure_m: float = Field(15, gt=0, le=1000)
    temperature_c: float = Field(20, ge=5, le=60)
    atmospheric_kpa: float = Field(101.325, ge=65, le=110)
    electricity_price: float = Field(0.8, ge=0, le=100)
    events: list[DesignEvent] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def times(self):
        duration = round(self.duration_hours * 3600, 6)
        if duration / self.step_seconds > 1200 or abs(duration % self.step_seconds) > 1e-5:
            raise ValueError("时长必须是步长的整数倍，且最多 1200 个报告间隔")
        if any(
            e.hour >= self.duration_hours or abs(e.hour * 3600 % self.step_seconds) > 1e-5
            for e in self.events
        ):
            raise ValueError("控制事件必须在结束前且落在报告时刻")
        if len({(e.hour, e.target) for e in self.events}) != len(self.events):
            raise ValueError("同一设备同一时刻只允许一个控制事件")
        return self


class NetworkDocument(StrictModel):
    schema_version: Literal["flowstate-network-1"] = "flowstate-network-1"
    name: str = Field("自定义管网", min_length=1, max_length=100)
    coordinate_unit: Literal["m"] = "m"
    nodes: list[DesignNode] = Field(default_factory=list, max_length=1200)
    links: list[DesignLink] = Field(default_factory=list, max_length=2000)
    settings: DesignSettings = Field(default_factory=DesignSettings)
    cad_metadata: dict = Field(default_factory=dict)
    note: str = "未完成实机验证。坐标与水力参数需分别核对。"


def default_document():
    nodes = [
        DesignNode(id="R1", kind="reservoir", x=0, y=20, z=0, source_head_m=2),
        DesignNode(id="J1", x=10, y=20),
        DesignNode(id="J2", x=30, y=20),
        DesignNode(id="J3", x=50, y=5, z=3, elevation_m=3, demand_m3h=3.5),
        DesignNode(id="J4", x=65, y=20, z=5, elevation_m=5, demand_m3h=4),
        DesignNode(id="J5", x=50, y=35, z=8, elevation_m=8, demand_m3h=3),
    ]
    links = [
        DesignLink(id="L1", start="R1", end="J1", length_m=10, diameter_m=0.065, minor_loss=2),
        DesignLink(id="P1", kind="pump", start="J1", end="J2"),
        DesignLink(id="P2", kind="pump", start="J1", end="J2", enabled=False),
        DesignLink(id="L2", start="J2", end="J3", length_m=60, diameter_m=0.04),
        DesignLink(id="L3", start="J2", end="J4", length_m=85, diameter_m=0.04),
        DesignLink(id="L4", start="J2", end="J5", length_m=110, diameter_m=0.04),
    ]
    return NetworkDocument(name="可编辑三维泵网示例", nodes=nodes, links=links)
