"""Verify fresh native actions, solver provenance and the finished 180 s media."""

import hashlib
import json
from pathlib import Path
import subprocess

import imageio_ffmpeg
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "演示视频/双案例实操版"
FINAL = OUT / "FLOWSTATE_双案例实机演示_3分钟.mp4"


def verify():
    r = json.loads((OUT / "recording.json").read_text())
    assert not r["rehearsal"] and not r["errors"]
    assert r["frames"] == 3600 and r["duration_s"] == 180
    assert r["qt_platform"] == "cocoa" and r["native_visible_window"]
    assert len(r["jobs"]) == 5 and all(j["state"] == "succeeded" for j in r["jobs"])
    assert len([a for a in r["actions"] if "created_node" in a]) == 5
    assert len([a for a in r["actions"] if "created_link" in a]) == 4
    assert any(a.get("network_matches_target") for a in r["actions"])
    assert any(a.get("fluid_parameters_verified") for a in r["actions"])
    assert {a["inspect"] for a in r["actions"] if "inspect" in a} == {"J4", "J2", "P1"}
    assert all(
        l["outer_scroll"] == 0 and all(v["within_window"] for v in l["views"]) for l in r["layouts"]
    )
    assert r["captured_fields"]["velocity"] == list(range(31))
    assert r["captured_fields"]["pressure"] == list(range(16))
    if (OUT / "edit_log.json").exists():
        edit = json.loads((OUT / "edit_log.json").read_text())
        patch = json.loads((OUT / "ending_recording.json").read_text())
        assert edit["segments"][1]["movie_s"] == [174, 180]
        assert patch["frames_captured"] == 120 and not patch["errors"]
        assert patch["cfd_case"] == r["cfd_case"]
        assert all(v["within_window"] for l in patch["layouts"] for v in l["views"])
    job = r["actual_cfd_job"]
    assert job["state"] == "succeeded" and job["started_at"] and job["finished_at"]
    result = json.loads((OUT / "cases/案例二_三维求解结果.json").read_text())
    project = json.loads((OUT / "cases/案例二_三维工况设置.json").read_text())
    assert result["manifest"]["project"] == project
    assert (
        result["case_directory"] == r["cfd_case"]
        and str(Path(r["workspace"]) / "cfd") in r["cfd_case"]
    )
    assert "preflight" not in r["cfd_case"]
    assert result["solver_run"]["version"] == "OpenFOAM-14" and result["summary"]["cells"] == 1264
    assert len(result["frames"]) == 31 and abs(result["frames"][-1]["seconds"] - 0.3) < 1e-9
    case = Path(r["cfd_case"])
    assert "Mesh OK." in (case / "log.checkMesh").read_text()
    assert "Time = 0.3" in (case / "log.foamRun").read_text()
    assert any(
        "foamRun" in state[3] and state[1] == "running"
        for a in r["actions"]
        for state in a.get("actual_job_states", [])
    )
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    scan = subprocess.run(
        [ff, "-hide_banner", "-i", str(FINAL), "-vn", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    assert scan.returncode == 0
    assert (
        "Duration: 00:03:00.00" in scan.stderr
        and "1920x1080" in scan.stderr
        and "48000 Hz" in scan.stderr
        and "mean_volume: -inf" not in scan.stderr
    )
    decode = subprocess.run(
        [ff, "-v", "error", "-i", str(FINAL), "-f", "null", "-"], capture_output=True, text=True
    )
    assert decode.returncode == 0 and not decode.stderr
    qa = OUT / "成片检查"
    qa.mkdir(exist_ok=True)
    info = [
        s.strip()
        for s in scan.stderr.splitlines()
        if any(
            v in s
            for v in ["Duration:", "Stream #0:0[", "Stream #0:1[", "mean_volume:", "max_volume:"]
        )
    ]
    report = {
        "media": info,
        "decode_errors": decode.stderr,
        "sha256": hashlib.sha256(FINAL.read_bytes()).hexdigest(),
        "bytes": FINAL.stat().st_size,
        "fresh_network_built": True,
        "new_cfd_job_id": job["id"],
        "fresh_cfd_case": r["cfd_case"],
        "all_jobs_succeeded": True,
        "native_clicks_and_parameters_verified": True,
        "complete_flow_frames": 31,
        "pressure_slices": 16,
        "layouts_checked": len(r["layouts"]),
    }
    (qa / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (OUT / "SHA256SUMS.txt").write_text(f"{report['sha256']}  {FINAL.name}\n")
    times = [
        2,
        10,
        16,
        22,
        28,
        34,
        40,
        44,
        49,
        55,
        60,
        66,
        72,
        78,
        86,
        94,
        98,
        104,
        110,
        114,
        122,
        130,
        134,
        140,
        145,
        150,
        155,
        159,
        163,
        168,
        173,
        178,
    ]
    for half in range(2):
        sheet = Image.new("RGB", (1920, 1160), "#10202a")
        draw = ImageDraw.Draw(sheet)
        for i, t in enumerate(times[half * 16 : (half + 1) * 16]):
            path = qa / f"{t:03}.png"
            subprocess.run(
                [
                    ff,
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    str(t),
                    "-i",
                    str(FINAL),
                    "-frames:v",
                    "1",
                    str(path),
                ],
                check=True,
            )
            im = Image.open(path)
            im.thumbnail((480, 270))
            x, y = i % 4 * 480, i // 4 * 290
            sheet.paste(im, (x, y))
            draw.text((x + 8, y + 273), f"{t // 60:02}:{t % 60:02}", fill="white")
        sheet.save(qa / f"成片总览_{half + 1}.png")
    print(
        "VERIFIED: native 180s; five fresh jobs; original GUI construction; new OpenFOAM result; full fields; media decode/audio/layouts",
        flush=True,
    )
    print("\n".join(info), flush=True)


if __name__ == "__main__":
    verify()
