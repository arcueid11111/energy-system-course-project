"""Prepare measured product claims and fresh offline Chinese narration."""

import json
import pickle
from pathlib import Path
import subprocess
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.models import NetworkDocument, DesignEvent
from backend.physics import simulate_design
from backend.observations import parse_observations
from backend.cfd.fields import display_frame
import imageio_ffmpeg

OUT = ROOT / "演示视频"
ASSETS = OUT / "assets"


def load(name):
    return json.loads((ASSETS / f"{name}.json").read_text())


def save(name, data):
    (ASSETS / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))


def prepare():
    baseline = load("baseline")
    doc = NetworkDocument.model_validate(baseline["document"])
    high = doc.model_copy(deep=True)
    high.name = "客户演示：单泵1.1倍速，同边界对比"
    for link in high.links:
        if link.kind == "pump":
            link.speed_ratio = 1.1
    reference = simulate_design(high)
    save("reference_high_speed", reference)
    dynamic = high.model_copy(deep=True)
    dynamic.name = "客户演示：转速切换与末端压力"
    dynamic.settings.events = [
        DesignEvent(hour=0.5, target="P1", kind="speed", value=1.0),
        DesignEvent(hour=0.75, target="P1", kind="speed", value=0.9),
    ]
    save("operating_replay", simulate_design(dynamic))
    calibrated = simulate_design(NetworkDocument.model_validate(load("calibration")["document"]))
    save("calibrated_run", calibrated)
    rows = parse_observations((ROOT / "examples/custom_network_observations.csv").read_text())
    save("observations", rows)
    wave_result = load("wave")
    frames = [
        display_frame(wave_result["case_directory"], frame, metric="pressure_pa")
        for frame in wave_result["frames"]
    ]
    with (ASSETS / "wave_replay.pkl").open("wb") as stream:
        pickle.dump(frames, stream)
    savings = 100 * (1 - baseline["summary"]["energy_kwh"] / reference["summary"]["energy_kwh"])
    assert 23 < savings < 24
    assert abs(reference["summary"]["delivered_m3"] - baseline["summary"]["delivered_m3"]) < 0.01
    save(
        "customer_claims",
        {
            "energy_saving_percent": savings,
            "comparison": "相同供水边界、同一时长：单泵1.1倍速与单泵1.0倍速",
            "reference_energy_kwh": reference["summary"]["energy_kwh"],
            "selected_energy_kwh": baseline["summary"]["energy_kwh"],
            "selected_min_pressure_m": min(p["min_pressure_m"] for p in baseline["points"]),
            "required_pressure_m": doc.settings.required_pressure_m,
            "real_asset_validated": False,
        },
    )
    story = [
        (
            0,
            12,
            "先看结果，再做决定",
            "intro",
            "供水是否达标？泵组怎样运行更省电？停泵后有什么风险？FLOWSTATE把管网、泵和三维流场放在同一个工作台，让方案有据可比。",
        ),
        (
            12,
            29,
            "把设计变成可计算的系统",
            "design",
            "您可以直接修改管网、连接支路、调整设备，再切换三维视角检查空间关系。也兼容CAD中心线和流体域，让设计方案能够继续进入水力与流场分析。",
        ),
        (
            29,
            49,
            "供水够不够，关键指标同屏看",
            "operation",
            "看这里：供水量、末端压力、用电和泵的运行点联动显示。切换转速，结果随之变化。降低转速虽然省电，但压力不足也会直接显现，帮助您找到运行约束，而不是只看单一能耗。",
        ),
        (
            49,
            70,
            "比较运行方案，找出有条件的节能空间",
            "optimization",
            "软件可以批量比较泵组组合和转速，并筛除供水或压力不达标的方案。这个示例中，相同供水条件下，单泵从一点一倍速调整到额定转速，电能减少约百分之二十三。具体收益以您的模型和工况为准。",
        ),
        (
            70,
            105,
            "把流动过程看清楚",
            "flow",
            "三维回放让您看到数值表格背后的流动。速度着色展示分布，矢量显示方向；转动视角后，再用压力切片和流线检查内部变化。最后，汽相率等值面显示低吸压下的汽蚀演化。这里连续回放零到零点六秒的全部输出时刻，来自本机实际求解结果。这个周期扇区示例用于展示分析能力，尚未完成真实设备标定。",
        ),
        (
            105,
            119,
            "让脉动与载荷变成可比较的曲线",
            "loads",
            "流场结果还能进一步给出流体力、力矩和频谱，便于比较不同运行条件的脉动特征。图中载荷属于周期扇区，不能直接当成整泵载荷。",
        ),
        (
            119,
            140,
            "提前评估关阀与停泵冲击",
            "hammer",
            "面对快速关阀，软件可以回放压力波，并把测点压力峰值单独显示。面对停泵，还能联动查看管网压力和转速变化。您可以先在模型中评估瞬态响应，再安排进一步试验与防护方案验证。",
        ),
        (
            140,
            155,
            "识别汽蚀带来的性能退化",
            "cavitation",
            "降低吸入压力，汽蚀可能伴随扬程下降。软件将汽蚀余量、扬程和统计状态放在一起展示。这个低压工况尚未统计稳定，因此保留为趋势分析，不作为最终选型结论。",
        ),
        (
            155,
            168,
            "让模型与观测逐步对齐",
            "calibration",
            "接入观测后，可以比较偏差、校准泵曲线，并查看留出记录的误差。这里明确使用合成数据演示；换成同步现场数据后，才能开展真实设备校准。",
        ),
        (
            168,
            176,
            "计算可追踪，结果可复用",
            "delivery",
            "任务统一调度，计算与校核状态清楚可查。工程和结果保存在本机，可以导出复核和继续分析。",
        ),
        (
            176,
            180,
            "设计 · 运行 · 风险分析",
            "ending",
            "FLOWSTATE，让每次运行决策，都有可见的计算依据。",
        ),
    ]
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    result = []
    for i, (start, end, title, action, text) in enumerate(story):
        folder = OUT / "audio"
        folder.mkdir(exist_ok=True)
        txt = folder / f"{i:02}.txt"
        txt.write_text(text)
        aiff = txt.with_suffix(".aiff")
        raw = folder / f"{i:02}-raw.wav"
        final = folder / f"{i:02}-voice.wav"
        subprocess.run(
            ["/usr/bin/say", "-v", "Tingting", "-r", "225", "-f", str(txt), "-o", str(aiff)],
            check=True,
        )
        subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", str(aiff), "-ar", "48000", "-ac", "1", str(raw)],
            check=True,
        )
        with wave.open(str(raw)) as audio:
            duration = audio.getnframes() / audio.getframerate()
        tempo = max(1, duration / (end - start - 0.8))
        subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", str(raw), "-af", f"atempo={tempo:.8f}", str(final)],
            check=True,
        )
        with wave.open(str(final)) as audio:
            fitted = audio.getnframes() / audio.getframerate()
        assert fitted <= end - start - 0.5
        result.append(
            dict(
                start=start,
                end=end,
                title=title,
                action=action,
                text=text,
                audio=str(final),
                audio_duration=fitted,
                tempo=tempo,
            )
        )
        print(i, title, round(fitted, 2), "s", flush=True)
    (OUT / "storyboard.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print("New narration and verified comparison ready", flush=True)


if __name__ == "__main__":
    prepare()
