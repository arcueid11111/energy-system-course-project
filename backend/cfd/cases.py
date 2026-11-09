"""Generate inspectable OpenFOAM 14 cases with physical, absolute-pressure inputs."""

import hashlib
import json
import math
import re
import shutil
import sys
from pathlib import Path

from .schemas import CFDProject


def resource_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))


def foam_header(name, cls="dictionary"):
    return f"FoamFile\n{{ format ascii; class {cls}; object {name}; }}\n"


def write_foam(path, content, cls="dictionary"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(foam_header(path.name, cls) + content + "\n", encoding="utf-8")


def vector(values):
    return "(" + " ".join(f"{v:.12g}" for v in values) + ")"


def wave_properties(project):
    p = project
    compliance = 1 / p.bulk_modulus_pa + p.wall.restraint_factor * p.pipe_diameter_m / (
        p.wall.young_pa * p.wall.thickness_m
    )
    effective_bulk = 1 / compliance
    speed = math.sqrt(effective_bulk / p.rho_kgm3)
    return {
        "effective_bulk_pa": effective_bulk,
        "wave_speed_ms": speed,
        "round_trip_s": 2 * p.pipe_length_m / speed,
        "joukowsky_pa": p.rho_kgm3 * speed * p.inlet_velocity_ms,
    }


def field(case, name, dimension, value, boundaries, vector_field=False):
    cls = "volVectorField" if vector_field else "volScalarField"
    write_foam(
        case / "0" / name,
        f"dimensions [{dimension}];\ninternalField uniform {value};\nboundaryField\n{{\n"
        '#includeEtc "caseDicts/setConstraintTypes"\n' + boundaries + "\n}\n",
        cls,
    )


def scale_mesh(path, factor, three_dimensional=False):
    text = path.read_text(encoding="utf-8")

    def replace(match):
        vals = [max(2, round(int(n) * factor)) for n in match.groups()]
        if three_dimensional:
            vals[2] = max(3, vals[2])
        return "(" + " ".join(map(str, vals)) + ") simpleGrading"

    text = re.sub(r"\(\s*(\d+)\s+(\d+)\s+(\d+)\s*\)\s*simpleGrading", replace, text)
    if three_dimensional:
        text = text.replace("type empty;", "type wall;")
    path.write_text(text, encoding="utf-8")


def liquid_properties(case, p, bulk):
    # Tait EOS: d(rho)/dp = rho/K at the reference state. Effective K includes
    # pipe-wall compliance only for the long-wave water-hammer benchmark.
    gamma = 7.0
    offset = bulk / gamma - 101325
    write_foam(
        case / "constant/physicalProperties.water",
        f"""
thermoType
{{
 type heRhoThermo; mixture pureMixture; transport const; thermo eConst;
 equationOfState adiabaticPerfectFluid; specie specie; energy sensibleInternalEnergy;
}}
mixture
{{
 specie {{ molWeight 18.0153; }}
 equationOfState {{ rho0 {p.rho_kgm3:.12g}; p0 101325; gamma {gamma}; B {offset:.12g}; }}
 thermodynamics {{ Cv 4195; hf 0; }}
 transport {{ mu {p.viscosity_pas:.12g}; Pr 7; }}
}}
""",
    )


def turbulence_fields(case, p, rotor=False):
    write_foam(
        case / "constant/momentumTransport",
        "simulationType laminar;"
        if p.turbulence == "laminar"
        else "simulationType RAS;\nRAS { model kOmegaSST; turbulence on; printCoeffs on; }",
    )
    if p.turbulence == "laminar":
        for name in ("epsilon", "k", "omega", "nut"):
            (case / "0" / name).unlink(missing_ok=True)
        return
    u = max(0.1, p.inlet_velocity_ms)
    k = 1.5 * (0.05 * u) ** 2
    omega = math.sqrt(k) / (0.09**0.25 * max(0.001, 0.07 * p.pipe_diameter_m))
    for name, dim, value, wall_type in [
        ("k", "0 2 -2 0 0 0 0", k, "kqRWallFunction"),
        ("omega", "0 0 -1 0 0 0 0", omega, "omegaWallFunction"),
        ("nut", "0 2 -1 0 0 0 0", 0, "nutkWallFunction"),
    ]:
        inlet_type = "calculated" if name == "nut" else "fixedValue"
        outlet_type = "calculated" if name == "nut" else "inletOutlet"
        noncouple_type = "calculated" if name == "nut" else "zeroGradient"
        field(
            case,
            name,
            dim,
            f"{value:.12g}",
            f"""
inlet {{ type {inlet_type}; value $internalField; }}
outlet {{ type {outlet_type}; inletValue $internalField; value $internalField; }}
wall {{ type {wall_type}; value $internalField; }}
nonCouple {{ type {noncouple_type}; value $internalField; }}
""",
        )
    (case / "0/epsilon").unlink(missing_ok=True)


def pressure_velocity_fields(case, p, compressible, rotor=False, hammer=False):
    q = p.flow_m3h / 3600 / (12 if rotor else 1)
    u0 = vector((p.inlet_velocity_ms, 0, 0)) if hammer else "(0 0 0)"
    wall_u = "type noSlip;"
    walls = (
        f"wall {{ {wall_u} }}\nnonCouple {{ type movingWallSlipVelocity; value $internalField; }}"
    )
    if rotor:
        walls = """
"(statorDuct|nonCoupleIn1|nonCoupleOut2)" { type noSlip; }
"(rotorDuct|rotorBlades|nonCoupleIn2|nonCoupleOut1)"
{ type movingWallVelocity; value $internalField; }
nonCouple { type movingWallSlipVelocity; value $internalField; }
"""
    if hammer:
        q0 = p.inlet_velocity_ms * math.pi * p.pipe_diameter_m**2 / 4
        table = (
            f"((0 {q0:.12g}) ({p.close_start_s:.12g} {q0:.12g}) "
            f"({p.close_start_s + p.close_duration_s:.12g} 0) ({p.end_time_s:.12g} 0))"
        )
        outflow = "volumetricFlowRate { type table; values " + table + "; };"
    else:
        outflow = f"volumetricFlowRate {q:.12g};"
    field(
        case,
        "U",
        "velocity",
        u0,
        f"""
inlet {{ type pressureInletOutletVelocity; value $internalField; }}
outlet {{ type flowRateOutletVelocity; {outflow} value $internalField; }}
{walls}
""",
        True,
    )
    if hammer:
        u0 = vector((p.inlet_velocity_ms, 0, 0))
        table = f"((0 {u0}) ({p.close_start_s:.12g} {u0}) ({p.close_start_s + p.close_duration_s:.12g} (0 0 0)) ({p.end_time_s:.12g} (0 0 0)))"
        wall = "slip" if p.wave_frictionless_benchmark else "noSlip"
        field(
            case,
            "U",
            "velocity",
            u0,
            f"""
inlet {{ type pressureInletOutletVelocity; value $internalField; }}
outlet {{ type uniformFixedValue; uniformValue {{type table; values {table};}} value $internalField; }}
wall {{ type {wall}; }}
""",
            True,
        )
    if not compressible:
        field(
            case,
            "p",
            "kinematicPressure",
            "0",
            """
inlet { type totalPressure; p0 uniform 0; value uniform 0; }
outlet { type zeroGradient; }
wall { type fixedFluxPressure; }
nonCouple { type fixedFluxPressure; }
""",
        )
        return
    pressure = p.inlet_absolute_pa
    total = pressure + (0.5 * p.rho_kgm3 * p.inlet_velocity_ms**2 if hammer else 0)
    field(
        case,
        "p",
        "pressure",
        str(pressure),
        """
"(inlet|outlet)" { type calculated; value $internalField; }
wall { type calculated; value $internalField; }
nonCouple { type calculated; value $internalField; }
""",
    )
    field(
        case,
        "p_rgh",
        "pressure",
        str(pressure),
        f"""
inlet {{ type prghTotalPressure; p0 uniform {total:.12g}; value $internalField; }}
outlet {{ type fixedFluxPressure; value $internalField; }}
wall {{ type fixedFluxPressure; value $internalField; }}
nonCouple {{ type fixedFluxPressure; value $internalField; }}
""",
    )
    field(
        case,
        "alpha.vapour",
        "0 0 0 0 0 0 0",
        "0",
        """
inlet { type fixedValue; value uniform 0; }
outlet { type inletOutlet; inletValue uniform 0; value uniform 0; }
wall { type zeroGradient; }
nonCouple { type zeroGradient; }
""",
    )
    field(
        case,
        "T",
        "temperature",
        str(p.temperature_k),
        """
inlet { type fixedValue; value $internalField; }
outlet { type inletOutlet; inletValue $internalField; value $internalField; }
wall { type zeroGradient; }
nonCouple { type zeroGradient; }
""",
    )


def pipe_mesh(case, p):
    """Five-block circular O-grid, with true circular arcs and a 3-D cross-section."""
    radius = p.pipe_diameter_m / 2
    a, b = radius * 0.35, radius / math.sqrt(2)
    yz = [(-a, -a), (a, -a), (a, a), (-a, a), (-b, -b), (b, -b), (b, b), (-b, b)]
    verts = [(x, y, z) for x in (0, p.pipe_length_m) for y, z in yz]
    quads = [(0, 1, 2, 3), (4, 5, 1, 0), (5, 6, 2, 1), (6, 7, 3, 2), (7, 4, 0, 3)]
    nx = max(10, round(p.pipe_cells * p.mesh_scale))
    nr = max(2, round(p.radial_cells * p.mesh_scale))
    blocks, inlet, outlet = [], [], []
    for i, (j0, j1, j2, j3) in enumerate(quads):
        ids = (j0, j0 + 8, j1 + 8, j1, j3, j3 + 8, j2 + 8, j2)
        counts = (nx, 2 * nr, 2 * nr if i == 0 else nr)
        blocks.append(f"hex {vector(ids)} {vector(counts)} simpleGrading (1 1 1)")
        inlet.append(vector((j0, j3, j2, j1)))
        outlet.append(vector((j0 + 8, j1 + 8, j2 + 8, j3 + 8)))
    arcs = []
    for offset, x in ((0, 0), (8, p.pipe_length_m)):
        for (i, j), (y, z) in zip(
            [(4, 5), (5, 6), (6, 7), (7, 4)], [(0, -radius), (radius, 0), (0, radius), (-radius, 0)]
        ):
            arcs.append(f"arc {i + offset} {j + offset} {vector((x, y, z))}")
    faces = [vector((i, i + 8, j + 8, j)) for i, j in [(4, 5), (5, 6), (6, 7), (7, 4)]]
    write_foam(
        case / "system/blockMeshDict",
        "vertices\n(\n"
        + "\n".join(map(vector, verts))
        + "\n);\nblocks\n(\n"
        + "\n".join(blocks)
        + "\n);\nedges\n(\n"
        + "\n".join(arcs)
        + "\n);\nboundary\n(\n"
        + "inlet {type patch; faces ("
        + " ".join(inlet)
        + ");}\n"
        + "outlet {type patch; faces ("
        + " ".join(outlet)
        + ");}\n"
        + "walls {type wall; faces ("
        + " ".join(faces)
        + ");}\n);\n",
    )


def function_objects(case, p, compressible, force_patches, origin):
    rho = "rho" if compressible else "rhoInf"
    force = f"""
forces
{{
 type forces; libs ("libforces.so"); patches ({force_patches});
 p p; U U; rho {rho}; rhoInf {p.rho_kgm3:.12g}; pRef {0 if not compressible else 101325};
 CofR {vector(origin)};
 executeControl timeStep; executeInterval 1; writeControl timeStep; writeInterval 1;
}}
#includeFunc patchFlowRate(name=inletFlowRate, patch=inlet)
#includeFunc patchFlowRate(name=outletFlowRate, patch=outlet)
"""
    force += """
velocitySquared
{
 type magSqr; libs ("libfieldFunctionObjects.so"); field U; result magSqrU;
 executeControl timeStep; executeInterval 1; writeControl writeTime;
}
"""
    port_fields = "p p_rgh magSqrU alpha.vapour" if compressible else "p magSqrU"
    for patch in ("inlet", "outlet"):
        force += f"""
portAverage_{patch}
{{
 type surfaceFieldValue; libs ("libfieldFunctionObjects.so");
 patch {patch}; operation average; weightField phi;
 fields ({port_fields}); writeFields false; log false;
 executeControl timeStep; executeInterval 1; writeControl timeStep; writeInterval 1;
}}
"""
    probes = p.probe_points
    if p.kind == "water_hammer" and not probes:
        probes = [(p.pipe_length_m * x, 0, 0) for x in (0.25, 0.5, 0.75, 0.995)]
    if probes:
        force += f"""
pressureProbes
{{
 type probes; libs ("libsampling.so");
 fields (p U {"alpha.vapour" if compressible else ""});
 writeControl timeStep; writeInterval 1;
 probeLocations ({" ".join(map(vector, probes))});
}}
"""
    write_foam(case / "system/functions", force)


def prepare_case(project: CFDProject, directory):
    p = project
    case = Path(directory).resolve()
    if case.exists() and any(case.iterdir()):
        raise ValueError("算例目录必须为空，防止覆盖已有计算")
    case.mkdir(parents=True, exist_ok=True)
    root = resource_root() / "resources/cfd/openfoam14"
    rotor = p.kind in ("impeller", "cavitating_impeller")
    compressible = p.kind != "impeller" and (
        p.kind != "cad" or p.cad_cavitation or p.cad_elastic_bulk
    )
    source = "impeller" if rotor or not compressible else "throttle"
    shutil.copytree(root / source, case, dirs_exist_ok=True)
    steps = [("blockMesh", [])]
    sector = 30 if rotor else 360
    notes = []
    if rotor:
        scale_mesh(case / "system/blockMeshDict", p.mesh_scale)
        text = (case / "constant/dynamicMeshDict").read_text(encoding="utf-8")
        text = text.replace("omega         60 [rpm];", f"omega         {p.rpm:.12g} [rpm];")
        (case / "constant/dynamicMeshDict").write_text(text, encoding="utf-8")
        steps.append(("createNonConformalCouples", []))
        notes.append(
            "30°周期叶轮扇区，流量为整周等效流量的1/12；载荷是扇区载荷，不能直接充当完整蜗壳径向力。"
        )
    elif p.kind == "throttle":
        scale_mesh(case / "system/blockMeshDict", p.mesh_scale, True)
        notes.append("三维挤出节流通道基准；不是离心泵叶轮。")
    elif p.kind == "water_hammer":
        pipe_mesh(case, p)
        notes.append("三维圆管可压缩计算；以等效体积模量表示长波管壁柔度，不求管壁位移/应力。")
        if p.wave_frictionless_benchmark:
            notes.append(
                "滑移壁面用于无摩阻Joukowsky基准；真实管壁需关闭此选项并做边界层网格校核。"
            )
    elif p.kind == "cad":
        from .geometry import prepare_stl_case

        steps = prepare_stl_case(case, p)
        rotor = p.cad_rotating
        sector = 360
    if compressible:
        for name in ["phaseProperties", "physicalProperties.vapour", "fvModels", "g"]:
            shutil.copy2(root / "throttle/constant" / name, case / "constant" / name)
        for name in ("fvSchemes", "fvSolution"):
            shutil.copy2(root / "throttle/system" / name, case / "system" / name)
        solution = case / "system/fvSolution"
        text = solution.read_text(encoding="utf-8").replace(
            "PIMPLE\n{", "PIMPLE\n{\n    correctPhi yes;\n    correctMeshPhi yes;"
        )
        text = text.replace(
            "solvers\n{",
            'solvers\n{\n    "pcorr.*" { solver GAMG; smoother DIC; tolerance 1e-9; relTol 0; }\n    "MeshPhi.*" { solver GAMG; smoother symGaussSeidel; tolerance 1e-9; relTol 0; }',
        )
        solution.write_text(text, encoding="utf-8")
        write_foam(
            case / "constant/g",
            f"dimensions [0 1 -2 0 0 0 0]; value {vector(p.gravity_ms2)};",
            "uniformDimensionedVectorField",
        )
        coefficients = p.cavitation
        write_foam(
            case / "constant/fvModels",
            f"""
VoFCavitation
{{ type compressible::VoFCavitation; model SchnerrSauer;
 SchnerrSauer {{ liquid water; pSat {p.vapour_pressure_pa:.12g};
 n {coefficients.nuclei_per_m3:.12g}; dNuc {coefficients.nucleus_diameter_m:.12g};
 Cv {coefficients.evaporation:.12g}; Cc {coefficients.condensation:.12g}; }}
}}
""",
        )
        bulk = (
            wave_properties(p)["effective_bulk_pa"]
            if p.kind == "water_hammer" or p.cad_elastic_bulk
            else p.bulk_modulus_pa
        )
        liquid_properties(case, p, bulk)
    else:
        write_foam(
            case / "constant/physicalProperties",
            f"viscosityModel constant;\nnu {p.viscosity_pas / p.rho_kgm3:.12g};",
        )
        for name in ("fvSchemes", "fvSolution"):
            file = case / "system" / name
            file.write_text(
                file.read_text(encoding="utf-8").replace("epsilon", "omega"), encoding="utf-8"
            )
        with (case / "system/fvSchemes").open("a") as out:
            out.write("\nwallDist { method meshWave; }\n")
    pressure_velocity_fields(
        case, p, compressible, rotor and p.kind != "cad", p.kind == "water_hammer"
    )
    if p.kind == "cad" and not p.cad_rotating:
        (case / "constant/dynamicMeshDict").unlink(missing_ok=True)
    if p.kind == "cad" and p.cad_rotating:
        # Patch names have explicit roles from preflight; the geometry mapper
        # changes the generic rotor wall to the moving-wall condition.
        u = (
            (case / "0/U")
            .read_text(encoding="utf-8")
            .replace(
                "wall { type noSlip; }",
                "wall { type noSlip; }\nrotor { type movingWallVelocity; value $internalField; }",
            )
        )
        (case / "0/U").write_text(u, encoding="utf-8")
    if p.kind == "throttle":
        for file in (case / "0").iterdir():
            text = file.read_text(encoding="utf-8")
            text = text.replace("type            empty;", "type            zeroGradient;")
            file.write_text(text, encoding="utf-8")
        # Pressure-controlled throttle benchmark, not the fixed-flow pump test.
        field(
            case,
            "U",
            "velocity",
            "(0 0 0)",
            """
"(inlet|outlet)" { type pressureInletOutletVelocity; value uniform (0 0 0); }
wall { type noSlip; }
""",
            True,
        )
        field(
            case,
            "p_rgh",
            "pressure",
            str(p.inlet_absolute_pa),
            f"""
inlet {{ type prghTotalPressure; p0 uniform {p.inlet_absolute_pa}; value $internalField; }}
outlet {{ type prghTotalPressure; p0 uniform {p.outlet_absolute_pa}; value $internalField; }}
wall {{ type fixedFluxPressure; value $internalField; }}
""",
        )
    turbulence_fields(case, p, rotor)
    dt = min(p.delta_t_s, p.rotor_angle_per_step_deg / (6 * p.rpm)) if rotor else p.delta_t_s
    write_foam(
        case / "system/controlDict",
        f"""
solver {"compressibleVoF" if compressible else "incompressibleFluid"};
startFrom startTime; startTime 0; stopAt endTime; endTime {p.end_time_s:.12g};
deltaT {dt:.12g}; adjustTimeStep {"no" if p.kind == "water_hammer" else "yes"};
maxDeltaT {dt:.12g}; maxCo {p.max_courant}; maxAlphaCo {p.max_courant};
writeControl adjustableRunTime; writeInterval {p.write_interval_s:.12g};
purgeWrite 0; writeFormat ascii; writePrecision 12; writeCompression off;
timeFormat general; timePrecision 12; runTimeModifiable true;
""",
    )
    force_patches = (
        "rotor" if p.kind == "cad" and rotor else ('"rotor(Duct|Blades)"' if rotor else '"walls.*"')
    )
    if p.kind == "water_hammer":
        force_patches = "outlet"
    function_objects(case, p, compressible, force_patches, p.rotor_origin)
    write_foam(
        case / "system/meshQualityDict", '#includeEtc "caseDicts/mesh/generation/meshQualityDict"\n'
    )
    (case / "flowstate.foam").touch()
    manifest = {
        "schema_version": "flowstate-cfd-case-1",
        "solver": "OpenFOAM Foundation 14",
        "project": p.model_dump(mode="json"),
        "compressible": compressible,
        "pressure_unit": "Pa absolute"
        if compressible
        else "m2/s2 gauge relative to inlet total pressure",
        "pressure_reference_pa": p.inlet_absolute_pa,
        "sector_angle_deg": sector,
        "load_patches": force_patches,
        "load_pressure_reference_pa": 101325 if compressible else p.inlet_absolute_pa,
        "steps": steps,
        "wave_reference": wave_properties(p) if p.kind == "water_hammer" else None,
        "assumptions": notes,
        "validated_on_real_asset": False,
    }
    manifest["input_hashes"] = {
        str(f.relative_to(case)): hashlib.sha256(f.read_bytes()).hexdigest()
        for directory in ("0", "constant", "system")
        for f in (case / directory).rglob("*")
        if f.is_file()
    }
    (case / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest
