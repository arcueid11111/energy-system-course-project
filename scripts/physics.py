"""CLI equivalent of the local desktop physics queue; no web server."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.physical_jobs import execute_physics_task

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "kind", choices=["cfd", "scan", "mesh", "timestep", "transient", "coupled", "validation"]
    )
    parser.add_argument("--project", type=Path)
    parser.add_argument("--network", type=Path)
    parser.add_argument("--options", type=Path)
    parser.add_argument("--measurements", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path("runtime/physics"))
    parser.add_argument("--output", type=Path, default=Path("runtime/physics-result.json"))
    args = parser.parse_args()
    payload = {
        "workspace": str(args.workspace),
        "options": json.loads(args.options.read_text()) if args.options else {},
    }
    if args.measurements:
        payload["csv_text"] = args.measurements.read_text(encoding="utf-8-sig")
    if args.result:
        payload["result"] = json.loads(args.result.read_text())
    if args.project:
        payload["project"] = json.loads(args.project.read_text())
    if args.network:
        payload["document"] = json.loads(args.network.read_text())

    def save(kind, name, result):
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return {**result, "id": str(args.output)}

    result = execute_physics_task(
        "physics_" + args.kind,
        payload,
        lambda value, message: print(f"{value:.0%} {message}", flush=True),
        save,
    )
    print(json.dumps(result, ensure_ascii=False))
