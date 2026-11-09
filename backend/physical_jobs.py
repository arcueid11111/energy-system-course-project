"""Physics jobs share the same persistent, cancellable desktop dispatcher."""

from pathlib import Path
from .cfd.schemas import CFDProject
from .models import NetworkDocument

KINDS = {
    "physics_cfd",
    "physics_scan",
    "physics_mesh",
    "physics_timestep",
    "physics_transient",
    "physics_coupled",
    "physics_validation",
}


def execute_physics_task(kind, payload, progress, save):
    root = Path(payload["workspace"]).resolve() / "cfd"
    options = payload.get("options", {})
    if kind == "physics_validation":
        from .validation import compare_series, parse_measurements, result_series

        measurement = parse_measurements(payload["csv_text"], options.get("source_note", ""))
        result = compare_series(
            measurement,
            result_series(
                payload["result"],
                int(options.get("probe_index", 0)),
                options.get("node_id", ""),
                options.get("device_id", ""),
            ),
            options.get("tolerances"),
        )
        name = "实测对比"
    elif kind == "physics_transient":
        from .transient import TransientSettings, simulate_transient

        doc = NetworkDocument.model_validate(payload["document"])
        result = simulate_transient(doc, TransientSettings.model_validate(options), progress)
        name = doc.name + " · 弹性瞬变"
    else:
        from .cfd.jobs import execute_cfd, cavitation_scan, convergence_study

        p = CFDProject.model_validate(payload["project"])
        name = p.name
        if kind == "physics_cfd":
            result = execute_cfd(p, root, progress)
        elif kind == "physics_scan":
            result = cavitation_scan(p, options, root, progress)
        elif kind in ("physics_mesh", "physics_timestep"):
            result = convergence_study(
                p, {**options, "mode": "mesh" if kind == "physics_mesh" else "time"}, root, progress
            )
        elif kind == "physics_coupled":
            from .cfd.coupling import coupled_simulation

            result = coupled_simulation(p, payload["document"], options, root, progress)
        else:
            raise ValueError("未知物理计算任务")
    result["task_kind"] = kind
    progress(0.97, "保存本机物理结果及验证记录")
    saved = save("physics_result", name, result)
    return {"physics_result_id": saved["id"], "summary": result.get("summary", {}), "kind": kind}
