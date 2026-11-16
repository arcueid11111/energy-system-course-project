"""Publish the verified local revision and remove superseded recording media."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import imageio_ffmpeg

ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "演示视频"
OUT = MEDIA / "双案例实操版"
FINAL = OUT / "FLOWSTATE_双案例实机演示_3分钟.mp4"


def main():
    verified = json.loads((OUT / "成片检查/verification.json").read_text())
    sha = hashlib.sha256(FINAL.read_bytes()).hexdigest()
    assert sha == verified["sha256"] and not verified["decode_errors"]
    target = MEDIA / "FLOWSTATE_客户演示_中文配音_3分钟.mp4"
    temporary = target.with_suffix(".tmp.mp4")
    shutil.copy2(FINAL, temporary)
    temporary.replace(target)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == sha
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    delivered = [target]
    for start, duration, name in [
        (8, 68, "FLOWSTATE_案例一_三维建网与水力求解_68秒.mp4"),
        (76, 82, "FLOWSTATE_案例二_三维设定求解与结果_82秒.mp4"),
    ]:
        path = MEDIA / name
        subprocess.run(
            [
                ff,
                "-y",
                "-v",
                "error",
                "-ss",
                str(start),
                "-i",
                str(FINAL),
                "-t",
                str(duration),
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "17",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                str(path),
            ],
            check=True,
        )
        info = subprocess.run(
            [ff, "-hide_banner", "-i", str(path)], capture_output=True, text=True
        ).stderr
        assert (
            f"Duration: 00:01:{duration - 60:02}.00" in info
            and "1920x1080" in info
            and "48000 Hz" in info
        )
        decoded = subprocess.run(
            [ff, "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True
        )
        assert decoded.returncode == 0 and not decoded.stderr
        delivered.append(path)
    deleted = []
    for name in [
        "FLOWSTATE_三维结果专段_35秒.mp4",
        "新录屏_无配音.mp4",
        "新录屏_合并无配音.mp4",
        "三维补录_无配音.mp4",
        "audio",
        "frames",
        "成片检查",
        "flow_recording.json",
        "rehearsal.json",
        "encode.log",
        "rehearsal_encode.log",
    ]:
        p = MEDIA / name
        if not p.exists():
            continue
        deleted.append(name)
        if p.is_symlink() or p.is_file():
            p.unlink()
        else:
            shutil.rmtree(p)
    for name in [
        "中文字幕.srt",
        "配音与章节.md",
        "recording.json",
        "ending_recording.json",
        "edit_log.json",
        "storyboard.json",
    ]:
        p = OUT / name
        if p.exists():
            shutil.copy2(p, MEDIA / name)
    (MEDIA / "制作说明.md").write_text(
        "当前交付为双案例实操版。工程、求解结果、录制日志与检查材料位于“ 双案例实操版 ”目录。下文的工作路径相对该目录。\n\n"
        + (OUT / "制作说明.md").read_text()
    )
    (MEDIA / "旧素材清理记录_双案例版.json").write_text(
        json.dumps(
            {
                "replaced_main_video": target.name,
                "deleted_old_media": deleted,
                "retained": "数值算例与计算记录",
                "current_files": [p.name for p in delivered],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    (MEDIA / "SHA256SUMS.txt").write_text(
        "\n".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in delivered)
        + "\n"
    )
    print("DELIVERED:")
    for p in delivered:
        print(p.resolve(), p.stat().st_size)
    print("OLD MEDIA REMOVED", len(deleted))


if __name__ == "__main__":
    main()
