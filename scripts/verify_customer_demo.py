"""Decode and visually sample the finished customer demo; write audit files."""

import hashlib
import json
from pathlib import Path
import subprocess

import imageio_ffmpeg
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "演示视频"


def verify():
    capture = json.loads((OUT / "recording.json").read_text())
    assert capture["qt_platform"] == "cocoa" and capture["visible_native_window"]
    assert capture["new_capture"] and not capture["old_media_reused"]
    assert capture["frames"] == 3600 and capture["duration_s"] == 180
    assert not capture["errors"] and not capture["layout_violations"]
    replay_capture = capture
    if (OUT / "edit_log.json").exists():
        edit = json.loads((OUT / "edit_log.json").read_text())
        assert edit["segments"][1]["movie_s"] == [70, 105]
        replay_capture = json.loads((OUT / "flow_recording.json").read_text())
        assert replay_capture["frames"] == 700
        assert not replay_capture["errors"] and not replay_capture["layout_violations"]
    assert {k: len(v) for k, v in replay_capture["captured_replay_indices"].items()} == {
        "velocity": 61,
        "pressure": 16,
        "cavity": 61,
    }
    assert not list(OUT.glob("*2分30秒.mp4"))
    assert not list(OUT.glob("*25秒.mp4"))
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    qa = OUT / "成片检查"
    qa.mkdir(exist_ok=True)
    names = [
        ("FLOWSTATE_客户演示_中文配音_3分钟.mp4", "00:03:00.00"),
        ("FLOWSTATE_三维结果专段_35秒.mp4", "00:00:35.00"),
    ]
    report = {}
    for name, duration in names:
        path = OUT / name
        scan = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-i",
                str(path),
                "-vn",
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
        )
        assert scan.returncode == 0
        assert f"Duration: {duration}" in scan.stderr
        assert "1920x1080" in scan.stderr and "48000 Hz" in scan.stderr
        assert "mean_volume: -inf" not in scan.stderr
        decode = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True,
            text=True,
        )
        assert decode.returncode == 0 and not decode.stderr
        info = [
            line.strip()
            for line in scan.stderr.splitlines()
            if any(
                s in line
                for s in [
                    "Duration:",
                    "Stream #0:0[",
                    "Stream #0:1[",
                    "mean_volume:",
                    "max_volume:",
                ]
            )
        ]
        report[name] = dict(
            bytes=path.stat().st_size,
            decode_errors=decode.stderr,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            media=info,
        )
        print(name, "\n" + "\n".join(info), flush=True)
    (qa / "media_verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (OUT / "SHA256SUMS.txt").write_text(
        "\n".join(f"{v['sha256']}  {k}" for k, v in report.items()) + "\n"
    )
    times = [
        3,
        15,
        21,
        27,
        34,
        43,
        47,
        55,
        64,
        76,
        83,
        87,
        96,
        104,
        111,
        123,
        128,
        133,
        138,
        146,
        151,
        161,
        172,
        178,
    ]
    source = OUT / names[0][0]
    sheet = Image.new("RGB", (1920, 6 * 290), "#10202a")
    draw = ImageDraw.Draw(sheet)
    for i, t in enumerate(times):
        path = qa / f"{t:03}.png"
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-v",
                "error",
                "-ss",
                str(t),
                "-i",
                str(source),
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
    sheet.save(qa / "成片总览.png")
    print(
        "PASS: new capture, fixed layouts, full replay, media decode, audio, durations, old-video deletion",
        flush=True,
    )


if __name__ == "__main__":
    verify()
