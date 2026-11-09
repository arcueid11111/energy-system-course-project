"""Explicit fluid-domain STEP/STL import, mesh preflight and rotating-zone setup."""

import hashlib
import json
import shutil
import threading
from functools import wraps

from pathlib import Path
from .cases import vector, write_foam

_GMSH_LOCK = threading.RLock()


def serialized(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with _GMSH_LOCK:
            return fn(*args, **kwargs)

    return wrapper


ROLES = {"inlet", "outlet", "walls", "rotor", "rotatingInterface"}


def _gmsh():
    try:
        import gmsh
    except (ImportError, OSError) as exc:
        raise ValueError("STEP 流体域转换需要 gmsh（安装 requirements-cfd.txt）") from exc
    return gmsh


@serialized
def inspect_step(path, units="m"):
    gmsh = _gmsh()
    gmsh.initialize([], readConfigFiles=False, interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.importShapes(str(Path(path).resolve()))
        gmsh.model.occ.synchronize()
        volumes = gmsh.model.getEntities(3)
        if len(volumes) != 1:
            raise ValueError("STEP 必须是一个已提取的流体体积；零件装配需先做流体域布尔提取")
        scale = 0.001 if units == "mm" else 1
        faces = []
        for dim, tag in gmsh.model.getBoundary(volumes, oriented=False):
            bounds = gmsh.model.getBoundingBox(dim, tag)
            center = gmsh.model.occ.getCenterOfMass(dim, tag)
            faces.append(
                {
                    "face": tag,
                    "area_m2": gmsh.model.occ.getMass(dim, tag) * scale**2,
                    "center_m": [x * scale for x in center],
                    "bounds_m": [x * scale for x in bounds],
                }
            )
        return {
            "faces": faces,
            "bounds_m": [x * scale for x in gmsh.model.getBoundingBox(-1, -1)],
            "source_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        }
    finally:
        gmsh.finalize()


@serialized
def export_step_surfaces(path, directory, face_roles, units="m", mesh_size_m=0.002):
    """Only user-assigned STEP faces become ports; no hidden face classification."""
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("几何输出目录必须为空")
    directory.mkdir(parents=True, exist_ok=True)
    info = inspect_step(path, units)
    tags = {f["face"] for f in info["faces"]}
    assigned = {int(k): str(v) for k, v in face_roles.items()}
    if set(assigned) != tags or not set(assigned.values()) <= ROLES - {"rotatingInterface"}:
        raise ValueError("需要给全部 STEP 边界面指定 inlet/outlet/walls/rotor 角色")
    if not {"inlet", "outlet", "walls"} <= set(assigned.values()):
        raise ValueError("流体域必须包含入口、出口和壁面")
    gmsh = _gmsh()
    gmsh.initialize([], readConfigFiles=False, interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.importShapes(str(Path(path).resolve()))
        if units == "mm":
            gmsh.model.occ.dilate(gmsh.model.occ.getEntities(3), 0, 0, 0, 0.001, 0.001, 0.001)
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", mesh_size_m)
        gmsh.option.setNumber("Mesh.MeshSizeMin", mesh_size_m / 3)
        gmsh.option.setNumber("Mesh.Binary", 0)
        gmsh.option.setNumber("Mesh.SaveAll", 0)
        gmsh.model.mesh.generate(2)
        for role in sorted(set(assigned.values())):
            group = gmsh.model.addPhysicalGroup(2, [k for k, v in assigned.items() if v == role])
            gmsh.model.setPhysicalName(2, group, role)
            gmsh.write(str(directory / f"{role}.stl"))
            gmsh.model.removePhysicalGroups([(2, group)])
    finally:
        gmsh.finalize()
    info.update(
        {
            "units": "m",
            "original_units": units,
            "face_roles": assigned,
            "geometry_type": "user_supplied_fluid_domain",
        }
    )
    (directory / "geometry.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return info


def inspect_stl_directory(directory, units="m", seed=None):
    try:
        from vtkmodules.vtkIOGeometry import vtkSTLReader
        from vtkmodules.vtkFiltersCore import vtkAppendPolyData, vtkCleanPolyData, vtkFeatureEdges
        from vtkmodules.vtkFiltersModeling import vtkSelectEnclosedPoints
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkPolyData
    except ImportError as exc:
        raise ValueError("三维流域检查需要 VTK（安装 requirements-cfd.txt）") from exc
    directory = Path(directory).resolve()
    files = {p.stem: p for p in directory.glob("*.stl")}
    if not {"inlet", "outlet", "walls"} <= files.keys() or not files.keys() <= ROLES:
        raise ValueError(
            "STL 目录需含 inlet.stl、outlet.stl、walls.stl；可选 rotor.stl、rotatingInterface.stl"
        )
    append = vtkAppendPolyData()
    inventory = []
    for role, path in sorted(files.items()):
        if path.is_symlink() or path.stat().st_size > 250_000_000:
            raise ValueError("不接受符号链接或大于250MB的单个STL文件")
        reader = vtkSTLReader()
        reader.SetFileName(str(path))
        reader.Update()
        data = reader.GetOutput()
        if data.GetNumberOfCells() == 0:
            raise ValueError(f"STL 中没有有效三角面：{path.name}")
        inventory.append(
            {
                "role": role,
                "triangles": data.GetNumberOfCells(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
        if role != "rotatingInterface":
            append.AddInputData(data)
    append.Update()
    clean = vtkCleanPolyData()
    clean.SetInputConnection(append.GetOutputPort())
    clean.Update()
    edges = vtkFeatureEdges()
    edges.SetInputConnection(clean.GetOutputPort())
    edges.BoundaryEdgesOn()
    edges.NonManifoldEdgesOn()
    edges.FeatureEdgesOff()
    edges.ManifoldEdgesOff()
    edges.Update()
    if edges.GetOutput().GetNumberOfCells():
        raise ValueError("入口、出口、壁面与转子面合并后不是闭合流体边界；请修复缺口或非流形边")
    raw_bounds = clean.GetOutput().GetBounds()
    scale = 0.001 if units == "mm" else 1
    bounds = [raw_bounds[i] * scale for i in (0, 2, 4, 1, 3, 5)]
    if seed is not None:
        points = vtkPoints()
        points.InsertNextPoint(*[v / scale for v in seed])
        sample = vtkPolyData()
        sample.SetPoints(points)
        enclosed = vtkSelectEnclosedPoints()
        enclosed.SetInputData(sample)
        enclosed.SetSurfaceData(clean.GetOutput())
        enclosed.Update()
        if not enclosed.IsInside(0):
            raise ValueError("给定种子点不在流体域内部；请勿选择在叶轮实体或流域外")
    return {"files": inventory, "bounds_m": bounds, "watertight": True, "units": units}


def prepare_stl_case(case, p):
    info = inspect_stl_directory(p.cad_directory, p.geometry_units, p.fluid_seed)
    destination = case / "constant/geometry"
    destination.mkdir(parents=True, exist_ok=True)
    if p.cad_rotating and not {"rotor", "rotatingInterface"} <= {f["role"] for f in info["files"]}:
        raise ValueError("旋转 CAD 还需 rotor.stl 和闭合的 rotatingInterface.stl 旋转区域包络面")
    from vtkmodules.vtkIOGeometry import vtkSTLReader, vtkSTLWriter
    from vtkmodules.vtkFiltersGeneral import vtkTransformPolyDataFilter
    from vtkmodules.vtkCommonTransforms import vtkTransform

    for item in info["files"]:
        role = item["role"]
        source = Path(p.cad_directory) / f"{role}.stl"
        if p.geometry_units == "mm":
            reader = vtkSTLReader()
            reader.SetFileName(str(source))
            transform = vtkTransform()
            transform.Scale(0.001, 0.001, 0.001)
            filt = vtkTransformPolyDataFilter()
            filt.SetTransform(transform)
            filt.SetInputConnection(reader.GetOutputPort())
            writer = vtkSTLWriter()
            writer.SetInputConnection(filt.GetOutputPort())
            writer.SetFileName(str(destination / source.name))
            writer.Write()
        else:
            shutil.copy2(source, destination / source.name)
    b = p.cad_bounds
    margin = max(b[i + 3] - b[i] for i in range(3)) * 0.1
    lo = [b[i] - margin for i in range(3)]
    hi = [b[i + 3] + margin for i in range(3)]
    vertices = [
        (lo[0], lo[1], lo[2]),
        (hi[0], lo[1], lo[2]),
        (hi[0], hi[1], lo[2]),
        (lo[0], hi[1], lo[2]),
        (lo[0], lo[1], hi[2]),
        (hi[0], lo[1], hi[2]),
        (hi[0], hi[1], hi[2]),
        (lo[0], hi[1], hi[2]),
    ]
    counts = [
        max(4, round(12 * p.mesh_scale * (hi[i] - lo[i]) / max(hi[j] - lo[j] for j in range(3))))
        for i in range(3)
    ]
    write_foam(
        case / "system/blockMeshDict",
        "vertices ("
        + " ".join(map(vector, vertices))
        + ");\n"
        + f"blocks (hex (0 1 2 3 4 5 6 7) {vector(counts)} simpleGrading (1 1 1));\n"
        + "edges (); boundary (outer {type patch; faces ((0 4 7 3)(1 2 6 5)(0 1 5 4)(3 7 6 2)(0 3 2 1)(4 5 6 7));});",
    )
    geometry = []
    surfaces = []
    for item in info["files"]:
        role = item["role"]
        geometry.append(f'{role} {{ type triSurfaceMesh; file "{role}.stl"; name {role}; }}')
        if role == "rotatingInterface":
            surfaces.append(
                "rotatingInterface {level (1 1); faceZone rotatingInterface; cellZone rotating; cellZoneInside inside; faceType internal;}"
            )
        else:
            typ = "patch" if role in ("inlet", "outlet") else "wall"
            surfaces.append(f"{role} {{level (2 2); patchInfo {{type {typ};}} }}")
    write_foam(
        case / "system/snappyHexMeshDict",
        f"""
castellatedMesh true; snap true; addLayers false;
geometry {{ {" ".join(geometry)} }}
castellatedMeshControls
{{ maxLocalCells 500000; maxGlobalCells 1000000; minRefinementCells 0;
 nCellsBetweenLevels 3; features (); refinementSurfaces {{ {" ".join(surfaces)} }}
 resolveFeatureAngle 30; refinementRegions {{}}; locationInMesh {vector(p.fluid_seed)};
 allowFreeStandingZoneFaces true;
}}
snapControls {{ nSmoothPatch 3; tolerance 2; nSolveIter 30; nRelaxIter 5; }}
addLayersControls {{ relativeSizes true; layers {{}}; expansionRatio 1.2; finalLayerThickness 0.3;
 minThickness 0.1; nGrow 0; featureAngle 60; nRelaxIter 3; nSmoothSurfaceNormals 1;
 nSmoothNormals 3; nSmoothThickness 10; maxFaceThicknessRatio 0.5; maxThicknessToMedialRatio 0.3;
 minMedianAxisAngle 90; nBufferCellsNoExtrude 0; nLayerIter 50; }}
meshQualityControls
{{
 #includeEtc "caseDicts/mesh/generation/meshQualityDict"
}}
mergeTolerance 1e-6;
""",
    )
    steps = [("blockMesh", []), ("snappyHexMesh", ["-overwrite"])]
    if p.cad_rotating:
        write_foam(
            case / "system/createBafflesDict",
            """
internalFacesOnly true;
baffles { rotatingInterface {type faceZone; zoneName rotatingInterface;
 patches { master {name rotorNCC; type patch; inGroups (nonCouple);} slave {name statorNCC; type patch; inGroups (nonCouple);} } } }
""",
        )
        write_foam(
            case / "system/createNonConformalCouplesDict",
            "nonConformalCouples { rotorCouple {patches (rotorNCC statorNCC); transform none;} }",
        )
        write_foam(
            case / "constant/dynamicMeshDict",
            f"""
mover {{ type solidBody; libs ("libpointMeshMovers.so"); cellZone rotating;
 solidBodyMotionFunction {{ type rotatingMotion; origin {vector(p.rotor_origin)};
 axis {vector(p.rotor_axis)}; omega {p.rpm:.12g} [rpm]; }} }}
""",
        )
        steps += [("createBaffles", ["-overwrite"]), ("createNonConformalCouples", [])]
    (case / "geometry-preflight.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return steps
