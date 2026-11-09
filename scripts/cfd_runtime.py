"""Build/load/probe a local OpenFOAM image, without changing Docker context globally."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.cfd.schemas import SolverRuntime
from backend.cfd.runtime import LocalSolver

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["probe", "build", "load", "save"])
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--context", default="")
    parser.add_argument("--image", default="flowstate-openfoam:14")
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    config = SolverRuntime(
        docker_command=args.docker, docker_context=args.context, image=args.image
    )
    solver = LocalSolver(config)
    base = solver.docker_base()
    if args.action == "build":
        subprocess.run(
            base
            + ["build", "-t", args.image, str(Path(__file__).resolve().parents[1] / "cfd_runtime")],
            check=True,
        )
    elif args.action in ("load", "save"):
        if not args.archive:
            parser.error("load/save requires --archive")
        subprocess.run(
            base
            + (
                ["load", "--input", str(args.archive)]
                if args.action == "load"
                else ["save", "--output", str(args.archive), args.image]
            ),
            check=True,
        )
    print(json.dumps(solver.probe(Path("runtime/solver-probe")), ensure_ascii=False, indent=2))
