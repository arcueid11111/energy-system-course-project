"""Explicit SI inputs for local, reproducible CFD jobs (OpenFOAM Foundation 14)."""

from typing import Literal

from pydantic import Field, model_validator

from backend.models import StrictModel


class SolverRuntime(StrictModel):
    mode: Literal["docker", "native", "wsl"] = "docker"
    docker_command: str = "docker"
    docker_context: str = ""
    image: str = "flowstate-openfoam:14"
    bashrc: str = "/opt/openfoam14/etc/bashrc"
    wsl_distribution: str = "Ubuntu"
    cpus: int = Field(2, ge=1, le=64)
    memory_gb: int = Field(3, ge=1, le=128)
    timeout_seconds: int = Field(3600, ge=30, le=604800)


class PipeWall(StrictModel):
    thickness_m: float = Field(0.003, gt=0, le=1)
    young_pa: float = Field(2.1e11, gt=1e6, le=1e13)
    poisson: float = Field(0.3, ge=0, lt=0.5)
    restraint_factor: float = Field(1, gt=0, le=2)


class CavitationCoefficients(StrictModel):
    nuclei_per_m3: float = Field(1.6e13, gt=0, le=1e20)
    nucleus_diameter_m: float = Field(2e-6, gt=1e-10, le=0.01)
    evaporation: float = Field(1, gt=0, le=1e6)
    condensation: float = Field(1, gt=0, le=1e6)


class CFDProject(StrictModel):
    schema_version: Literal["flowstate-cfd-1"] = "flowstate-cfd-1"
    name: str = Field("三维流场基准", min_length=1, max_length=100)
    kind: Literal["impeller", "cavitating_impeller", "water_hammer", "throttle", "cad"] = "impeller"
    runtime: SolverRuntime = Field(default_factory=SolverRuntime)
    provenance: Literal["parameterized_benchmark", "user_geometry", "measured_asset"] = (
        "parameterized_benchmark"
    )
    source_note: str = "参数化数值基准，未做实机验证"
    mesh_scale: float = Field(1, ge=0.25, le=8)
    end_time_s: float = Field(0.02, gt=0, le=1e5)
    delta_t_s: float = Field(1e-5, gt=1e-10, le=1)
    write_interval_s: float = Field(0.002, gt=0, le=1e4)
    max_courant: float = Field(0.3, gt=0, le=0.8)
    temperature_k: float = Field(293.15, ge=278.15, le=333.15)
    rho_kgm3: float = Field(998.2, gt=100, le=2000)
    viscosity_pas: float = Field(1.002e-3, gt=1e-6, le=1)
    bulk_modulus_pa: float = Field(2.2e9, gt=1e6, le=1e11)
    vapour_pressure_pa: float = Field(2339, gt=0, le=1e6)
    inlet_absolute_pa: float = Field(101325, gt=0, le=1e8)
    outlet_absolute_pa: float = Field(101325, gt=0, le=1e8)
    inlet_velocity_ms: float = Field(1, ge=0, le=100)
    flow_m3h: float = Field(5, gt=0, le=1e6)
    rpm: float = Field(600, gt=0, le=60000)
    rotor_angle_per_step_deg: float = Field(1, gt=0, le=5)
    turbulence: Literal["laminar", "kOmegaSST"] = "kOmegaSST"
    cavitation: CavitationCoefficients = Field(default_factory=CavitationCoefficients)
    pipe_length_m: float = Field(20, gt=0, le=1e5)
    pipe_diameter_m: float = Field(0.1, gt=1e-4, le=10)
    pipe_cells: int = Field(80, ge=10, le=10000)
    radial_cells: int = Field(4, ge=2, le=64)
    wave_frictionless_benchmark: bool = True
    wave_solver: Literal["acoustic_fv", "openfoam"] = "acoustic_fv"
    wall: PipeWall = Field(default_factory=PipeWall)
    close_start_s: float = Field(0.002, ge=0)
    close_duration_s: float = Field(0.001, gt=0)
    cad_directory: str = ""
    geometry_units: Literal["m", "mm"] = "m"
    fluid_seed: tuple[float, float, float] = (0, 0, 0)
    cad_bounds: tuple[float, float, float, float, float, float] | None = None
    rotor_origin: tuple[float, float, float] = (0, 0, 0)
    rotor_axis: tuple[float, float, float] = (1, 0, 0)
    cad_rotating: bool = False
    cad_cavitation: bool = False
    cad_elastic_bulk: bool = False
    probe_points: list[tuple[float, float, float]] = Field(default_factory=list, max_length=100)
    gravity_ms2: tuple[float, float, float] = (0, 0, 0)

    @model_validator(mode="after")
    def physical_checks(self):
        if self.kind == "impeller" and any(self.gravity_ms2):
            raise ValueError("含重力的三维算例请使用可压缩/两相求解模式")
        if self.delta_t_s > self.end_time_s or self.write_interval_s > self.end_time_s:
            raise ValueError("时间步长及输出间隔不能超过仿真时长")
        if self.end_time_s / self.write_interval_s > 2000:
            raise ValueError("最多输出 2000 帧；请增加输出间隔")
        if self.rpm * 6 * self.delta_t_s > self.rotor_angle_per_step_deg:
            raise ValueError("每步叶轮转角过大；请减小时间步长")
        if self.vapour_pressure_pa >= self.inlet_absolute_pa and self.kind != "water_hammer":
            raise ValueError("入口绝压需高于饱和蒸汽压；不能把表压当作绝压")
        if self.kind == "water_hammer":
            if self.close_start_s + self.close_duration_s >= self.end_time_s:
                raise ValueError("关阀过程必须在仿真结束前完成")
            if self.pipe_diameter_m <= 2 * self.wall.thickness_m:
                raise ValueError("壁厚与内径设定不合理")
        if self.kind == "cad":
            if not self.cad_directory or self.cad_bounds is None:
                raise ValueError("CAD 流域需指定分区 STL 目录、包围盒和流体种子点")
            b = self.cad_bounds
            if any(b[i] >= b[i + 3] for i in range(3)):
                raise ValueError("CAD 包围盒必须按 xmin,ymin,zmin,xmax,ymax,zmax 给定")
            if any(not b[i] < self.fluid_seed[i] < b[i + 3] for i in range(3)):
                raise ValueError("流体种子点必须位于包围盒内部（单位 m）")
            if sum(v * v for v in self.rotor_axis) < 1e-12:
                raise ValueError("旋转轴不能为零向量")
        return self


def benchmark_project(kind="impeller"):
    if kind == "water_hammer":
        return CFDProject(
            name="弹性直管快速关阀 · 三维可压缩基准",
            kind=kind,
            radial_cells=2,
            end_time_s=0.02,
            delta_t_s=2e-5,
            write_interval_s=0.0025,
            turbulence="laminar",
        )
    if kind == "throttle":
        return CFDProject(
            name="节流汽蚀 · 三维两相基准",
            kind=kind,
            end_time_s=0.0002,
            delta_t_s=1e-8,
            write_interval_s=2e-5,
            inlet_absolute_pa=3500000,
            outlet_absolute_pa=100000,
            temperature_k=300,
        )
    return CFDProject(
        name="径向叶轮周期扇区 · " + ("两相汽蚀" if kind == "cavitating_impeller" else "单相脉动"),
        kind=kind,
        end_time_s=0.01 if kind == "cavitating_impeller" else 0.05,
        delta_t_s=1e-6 if kind == "cavitating_impeller" else 1e-4,
        write_interval_s=0.001 if kind == "cavitating_impeller" else 0.005,
        rpm=600,
    )
