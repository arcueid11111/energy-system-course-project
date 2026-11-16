"""Align the two field-view explanations with their actual scene transitions."""

import json
from pathlib import Path
import subprocess
import wave
import imageio_ffmpeg

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "演示视频/双案例实操版"


def main():
    story = json.loads((OUT / "storyboard.json").read_text())
    scene = next(s for s in story if s["action"] == "fluid_results")
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    parts = [
        (
            112,
            134,
            "先回放零到零点三秒的速度场。颜色表示速度大小，箭头给出流动方向，右侧曲线同步显示变化。转动视角，可以观察不同位置的流速分布。",
        ),
        (
            134,
            146,
            "现在切换压力切片，并打开流线，沿着流动路径查看内部压力和速度变化。这里的网格、时间轴和工况参数，都对应刚才提交的叶轮任务。",
        ),
    ]
    result = []
    for i, (start, end, text) in enumerate(parts):
        txt = OUT / "audio" / f"field-part-{i}.txt"
        txt.write_text(text)
        aiff = txt.with_suffix(".aiff")
        raw = txt.with_suffix(".wav")
        final = OUT / "audio" / f"field-part-{i}-voice.wav"
        subprocess.run(
            ["/usr/bin/say", "-v", "Tingting", "-r", "225", "-f", str(txt), "-o", str(aiff)],
            check=True,
        )
        subprocess.run(
            [ff, "-y", "-v", "error", "-i", str(aiff), "-ar", "48000", "-ac", "1", str(raw)],
            check=True,
        )
        with wave.open(str(raw)) as w:
            duration = w.getnframes() / w.getframerate()
        tempo = max(1, duration / (end - start - 0.8))
        subprocess.run(
            [ff, "-y", "-v", "error", "-i", str(raw), "-af", f"atempo={tempo:.8f}", str(final)],
            check=True,
        )
        with wave.open(str(final)) as w:
            duration = w.getnframes() / w.getframerate()
        result.append(
            dict(start=start, end=end, text=text, audio=str(final), audio_duration=duration)
        )
    scene["voice_parts"] = result
    scene["text"] = "".join(p["text"] for p in result)
    (OUT / "storyboard.json").write_text(json.dumps(story, ensure_ascii=False, indent=2))
    print("NARRATION ALIGNED: velocity 112—134; pressure 134—146")


if __name__ == "__main__":
    main()
