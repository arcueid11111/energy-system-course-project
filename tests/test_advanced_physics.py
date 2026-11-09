import json
import math
from pathlib import Path
import numpy as np
import pytest
from backend.loads import signal_metrics, surface_load
from backend.cavitation import CavitationMap, HeadBreakdown, npsha
from backend.transient import reference_pipe, TransientSettings, TransientEvent, simulate_transient
from backend.cfd.schemas import benchmark_project, SolverRuntime, CFDProject
from backend.cfd.cases import prepare_case, wave_properties
from backend.cfd.coupling import integrate_rotor, apply_waveforms
from backend.models import default_document
from backend.validation import parse_measurements, compare_series


def test_joukowsky_and_wave_arrival_independent_moc():
    r = reference_pipe(cells=120, close_time_s=0.0004)
    first = [v for v in r["points"] if v["seconds"] < 0.02]
    assert max(v["valve_pressure_rise_pa"] for v in first) == pytest.approx(
        r["expected_joukowsky_pa"], rel=1e-8
    )
    arrival = next(
        v["seconds"] for v in first if v["mid_pressure_rise_pa"] > r["expected_joukowsky_pa"] * 0.1
    )
    assert arrival == pytest.approx(0.01, abs=0.0005)


def test_fft_resolves_force_pulsation():
    t = np.arange(0, 2, 0.0005)
    y = 12 + 3 * np.sin(2 * np.pi * 120 * t)
    r = signal_metrics(t, y, discard_fraction=0)
    assert r["dominant_hz"] == pytest.approx(120, abs=0.6)
    assert r["rms_fluctuation"] == pytest.approx(3 / math.sqrt(2), rel=0.001)
    assert max(v["amplitude"] for v in r["spectrum"]) == pytest.approx(3, rel=0.005)


def test_traction_integral_closed_surface_and_moment():
    normals = np.vstack([np.eye(3), -np.eye(3)])
    r = surface_load(np.ones(6), normals, np.full(6, 2e5), np.zeros((6, 3)), centers=normals)
    assert r["force_n"] == pytest.approx([0, 0, 0])
    assert r["moment_nm"] == pytest.approx([0, 0, 0])
    r = surface_load([2], [[1, 0, 0]], [500], [[0, 0, 0]], centers=[[0, 1, 0]])
    assert r["force_n"] == [-1000, 0, 0]
    assert r["moment_nm"] == [0, 0, 1000]


def test_cavitation_npsh3_and_no_extrapolation():
    points = []
    for q in (5, 10):
        for a, ratio in [(0, 0.6), (1, 0.9), (2, 0.97), (4, 1)]:
            points.append(
                dict(flow_m3h=q, rpm=600, npsha_m=a, head_m=10 * ratio, noncavitating_head_m=10)
            )
    m = HeadBreakdown(
        CavitationMap(source_note="synthetic unit check", source="synthetic", points=points)
    )
    assert m.ratio(7.5, 600, 2) == pytest.approx(0.97)
    assert m.npsh3()[0]["npsh3_m"] == pytest.approx(2)
    with pytest.raises(ValueError):
        m.ratio(5, 600, 10)
    with pytest.raises(ValueError):
        m.ratio(7.5, 900, 2)
    assert npsha(101325, 2, 998.2, 2339) > 10


def test_rotor_equation_energy_and_reversal():
    t = np.linspace(0, 1, 101)
    rpm = integrate_rotor(t, np.full(len(t), -2), 3, 2, 600)
    omega = rpm * 2 * np.pi / 60
    assert omega[-1] - omega[0] == pytest.approx(0.5)
    assert (omega[-1] ** 2 - omega[0] ** 2) == pytest.approx(np.trapezoid(omega, t), rel=1e-10)
    with pytest.raises(ValueError):
        integrate_rotor([0, 1], [-100, -100], 0, 1, 1)


def test_original_time_measurement_comparison():
    text = "seconds,pressure_pa\n0,100\n1,110\n2,120\n3,130\n4,140\n"
    m = parse_measurements(text, "synthetic test, absolute Pa")
    r = compare_series(
        m, [dict(seconds=t, pressure_pa=100 + 10 * t + 2) for t in range(4)], {"pressure_pa": 1}
    )
    assert r["metrics"]["pressure_pa"]["rmse"] == 2
    assert r["excluded_measurements"] == 1
    assert r["metrics"]["pressure_pa"]["passed"] is False
    assert r["time_shift_s"] == 0


def test_elastic_wall_reduces_wave_speed():
    p = benchmark_project("water_hammer")
    r = wave_properties(p)
    assert r["wave_speed_ms"] < math.sqrt(p.bulk_modulus_pa / p.rho_kgm3)
    p.wall.young_pa *= 100
    assert wave_properties(p)["wave_speed_ms"] > r["wave_speed_ms"]


def test_boundary_pressure_waveforms_do_not_prescribe_flow(tmp_path):
    p = benchmark_project("cavitating_impeller")
    m = prepare_case(p, tmp_path)
    waves = {
        "inlet_pressure_pa": [[0, 101325], [p.end_time_s, 100000]],
        "outlet_pressure_pa": [[0, 102000], [p.end_time_s, 103000]],
        "rpm": [[0, 0], [p.end_time_s, 600]],
    }
    apply_waveforms(tmp_path, m, waves)
    assert "flowRateOutletVelocity" not in (tmp_path / "0/U").read_text(encoding="utf-8")
    assert "uniformFixedValue" in (tmp_path / "0/p_rgh").read_text(encoding="utf-8")
    assert "omega {type table" in (tmp_path / "constant/dynamicMeshDict").read_text(
        encoding="utf-8"
    )
    assert (
        json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["coupling_boundary"]
        == waves
    )


def test_transient_network_trip_and_two_port_feedback():
    d = default_document()
    s = TransientSettings(
        duration_s=0.008,
        report_interval_s=0.001,
        max_dt_s=0.0001,
        events=[TransientEvent(seconds=0.002, target="P1", kind="pump_trip")],
    )
    r = simulate_transient(d, s)
    assert (
        r["points"][-1]["devices"]["P1"]["speed_ratio"]
        < r["points"][0]["devices"]["P1"]["speed_ratio"]
    )
    assert r["points"][-1]["seconds"] == 0.008
    assert all(math.isfinite(n["pressure_pa"]) for f in r["points"] for n in f["nodes"].values())


def test_actual_solver_fixture_fields_and_streamlines():
    from backend.cfd.fields import display_frame

    root = Path(__file__).resolve().parents[1] / "examples/cfd/rotor-startup"
    r = json.loads((root / "result.json").read_text(encoding="utf-8"))
    display = display_frame(
        root, r["frames"][-1], plane_axis=0, seeds=[(-0.02, 0.005, 0.001)], streamlines=True
    )
    assert display["maximum"] > 0
    assert display["cell_count"] == 480
    assert np.isfinite(display["velocity"]).all()
    assert r["verification"]["real_asset_validated"] is False


def test_three_dimensional_acoustic_fv_against_joukowsky(tmp_path):
    from backend.cfd.jobs import execute_cfd

    p = benchmark_project("water_hammer")
    p.mesh_scale = 0.25
    p.pipe_cells = 40
    p.end_time_s = 0.008
    p.close_start_s = 0.0002
    p.close_duration_s = 0.0002
    p.write_interval_s = 0.004
    r = execute_cfd(p, tmp_path)
    assert abs(r["verification"]["joukowsky"]["relative_error"]) < 0.04
    assert r["summary"]["cells"] >= 200
    assert len(r["frames"]) == 3
    from backend.cfd.fields import display_frame

    field = display_frame(r["case_directory"], r["frames"][-1], "pressure_pa")
    assert field["maximum"] > 1e6
    assert field["velocity"].shape[1] == 3


def test_step_to_closed_partitioned_stl(tmp_path):
    import gmsh
    from backend.cfd.geometry import inspect_step, export_step_surfaces, inspect_stl_directory

    source = tmp_path / "fluid.step"
    gmsh.initialize([], readConfigFiles=False, interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.addBox(0, 0, 0, 0.2, 0.04, 0.04)
        gmsh.model.occ.synchronize()
        gmsh.write(str(source))
    finally:
        gmsh.finalize()
    info = inspect_step(source)
    roles = {
        f["face"]: (
            "inlet"
            if f["center_m"][0] < 1e-7
            else "outlet"
            if f["center_m"][0] > 0.19999
            else "walls"
        )
        for f in info["faces"]
    }
    export_step_surfaces(source, tmp_path / "surfaces", roles, mesh_size_m=0.008)
    stl = inspect_stl_directory(tmp_path / "surfaces", seed=(0.1, 0.02, 0.02))
    assert stl["watertight"]
    assert set(f["role"] for f in stl["files"]) == {"inlet", "outlet", "walls"}
    with pytest.raises(ValueError):
        inspect_stl_directory(tmp_path / "surfaces", seed=(1, 1, 1))


def test_runtime_is_local_and_network_disabled(tmp_path):
    from backend.cfd.runtime import LocalSolver

    cmd = LocalSolver(SolverRuntime()).command("foamRun", [], tmp_path, "test-container")
    assert "--network=none" in cmd
    assert "--cap-drop=ALL" in cmd
    with pytest.raises(ValueError):
        LocalSolver(SolverRuntime()).command("bash", ["-c", "echo bad"], tmp_path)


def test_model_rejects_surface_and_absolute_pressure_errors():
    p = benchmark_project().model_dump()
    p["inlet_absolute_pa"] = 100
    with pytest.raises(ValueError):
        CFDProject.model_validate(p)


def test_cfd_map_rejected_before_transient_feedback():
    d = default_document()
    points = [
        dict(flow_m3h=5, rpm=600, npsha_m=a, head_m=h, noncavitating_head_m=10)
        for a, h in [(0, 6), (1, 9), (2, 9.7), (4, 10)]
    ]
    mapping = CavitationMap(
        source="cfd",
        source_note="unsettled test data",
        numerical_acceptance="failed",
        points=points,
    )
    s = TransientSettings(
        duration_s=0.004, report_interval_s=0.001, cavitation_maps={"P1": mapping}
    )
    with pytest.raises(ValueError, match="稳定性审核"):
        simulate_transient(d, s)
