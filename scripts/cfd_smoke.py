"""Real local solver integration, intended for the separately built CFD image."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.cfd.jobs import execute_cfd
from backend.cfd.schemas import benchmark_project, SolverRuntime

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", default="")
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--output", type=Path, default=Path("reports/cfd-smoke.json"))
    args = parser.parse_args()
    records = []
    for kind, end, step in [("impeller", 0.002, 1e-4), ("cavitating_impeller", 2e-5, 1e-6)]:
        p = benchmark_project(kind)
        p.runtime = SolverRuntime(docker_command=args.docker, docker_context=args.context)
        p.mesh_scale = 0.35
        p.end_time_s = end
        p.delta_t_s = step
        p.write_interval_s = end / 2
        r = execute_cfd(p, "runtime/cfd-smoke")
        assert r["frames"][-1]["seconds"] >= end * 0.99999
        assert r["loads"] and r["performance"]
        records.append(
            {
                "kind": kind,
                "summary": r["summary"],
                "verification": r["verification"],
                "solver": r["solver_run"],
            }
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "records": records,
                "notice": "Short startup solver checks, not mesh convergence or asset validation.",
            },
            indent=2,
        )
    )
    print("Real CFD solver smoke checks passed")
