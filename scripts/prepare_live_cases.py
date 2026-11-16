"""Preflight the exact two cases used by the live, freshly solved GUI demo."""

import json
import math
from pathlib import Path
import sys
import time
import argparse
import subprocess
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.models import NetworkDocument, DesignNode, DesignLink, DesignSettings
from backend.physics import simulate_design
from backend.cfd.schemas import benchmark_project
from backend.cfd.jobs import execute_cfd

OUT = ROOT / "演示视频/双案例实操版"
DATA = OUT / "data"


def save(name, value):
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2))


def network():
    nodes = [
        DesignNode(id="R1", kind="reservoir", x=0, y=0, z=0, source_head_m=2),
        DesignNode(id="J1", x=12, y=0, z=1, elevation_m=1),
        DesignNode(id="J2", x=38, y=14, z=3, elevation_m=3, demand_m3h=3),
        DesignNode(id="J3", x=38, y=-12, z=6, elevation_m=6, demand_m3h=2),
        DesignNode(id="J4", x=62, y=-12, z=9, elevation_m=9, demand_m3h=4),
    ]
    pairs = [
        ("P1", "pump", "R1", "J1"),
        ("L1", "pipe", "J1", "J2"),
        ("L2", "pipe", "J1", "J3"),
        ("L3", "pipe", "J3", "J4"),
    ]
    lookup = {n.id: n for n in nodes}
    links = []
    for key, kind, start, end in pairs:
        a, b = lookup[start], lookup[end]
        links.append(
            DesignLink(
                id=key,
                kind=kind,
                start=start,
                end=end,
                length_m=math.dist((a.x, a.y, a.z), (b.x, b.y, b.z)),
                diameter_m=0.05,
            )
        )
    return NetworkDocument(
        name="案例一 · 多高程供水管网",
        nodes=nodes,
        links=links,
        settings=DesignSettings(required_pressure_m=15),
    )


def fluid_project():
    p = benchmark_project("impeller")
    p.name = "案例二 · 600rpm三维叶轮流场"
    p.source_note = "客户实操案例：30°叶轮扇区，600rpm，水，单相层流"
    p.mesh_scale = 0.5
    p.turbulence = "laminar"
    p.rpm = 600
    p.flow_m3h = 5
    p.inlet_absolute_pa = 101325
    p.end_time_s = 0.3
    p.delta_t_s = 0.0001
    p.write_interval_s = 0.01
    p.runtime.docker_command = "/opt/homebrew/bin/docker"
    p.runtime.docker_context = "colima-flowstate-cfd"
    return p


def voice():
    import imageio_ffmpeg

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    scenes = [
        (
            0,
            8,
            "两个案例，从建模一直看到结果",
            "intro",
            "这次用两个实操案例，带您从搭建三维管网，一直看到水力结果和三维流场。",
        ),
        (
            8,
            18,
            "案例一 / 从空白工程放置节点",
            "nodes",
            "先新建供水工程。放置水源、泵后节点，以及三个用水节点，分别填写位置、高程和用水量。",
        ),
        (
            18,
            30,
            "连接泵与管道，形成完整供水路径",
            "links",
            "接着连接一台泵和三段管道。三个用水节点高程分别是三米、六米和九米，总需水量为每小时九立方米。每条连接都进入同一个计算模型。",
        ),
        (
            30,
            38,
            "检查三维布局与运行参数",
            "geometry",
            "切换三维视角，检查供水路径和高程关系。本例采用五十毫米管径，并要求末端压力至少十五米。",
        ),
        (
            38,
            45,
            "提交水力求解，任务状态实时更新",
            "network_solve",
            "点击开始计算。软件将当前工程提交到本机队列，求解完成后直接打开结果。",
        ),
        (
            45,
            62,
            "带着看：末端压力、供水量与泵运行点",
            "network_results",
            "先看最高的九米用水点，压力约十九米，高于十五米要求。再看泵的运行点：每小时供水九立方米。全网一小时用电约一点二三千瓦时，供水、压力和能耗可以一起判断。",
        ),
        (
            62,
            76,
            "同一个工程，再比较两种转速",
            "comparison",
            "再批量计算额定转速和一点一倍速。两种方案的供水量相同，额定转速的用电约少百分之二十二，同时保持末端压力达标。这就是一次完整的运行方案比较。",
        ),
        (
            76,
            96,
            "案例二 / 设定三维叶轮流场",
            "fluid_setup",
            "第二个案例，实际设置三维叶轮工况。选择单相水流，转速六百转每分钟，整周流量每小时五立方米，入口绝压十万一千三百二十五帕。计算零点三秒，最大时间步万分之一秒，每百分之一秒输出一次流场。",
        ),
        (
            96,
            112,
            "启动OpenFOAM，生成网格并求解",
            "fluid_solve",
            "现在启动本机三维求解。队列中可以看到网格生成、质量检查、时间推进和结果导出。画面压缩了计算等待，接下来查看的就是这次任务产生的结果。",
        ),
        (
            112,
            146,
            "带着看：完整速度场、压力切片与流线",
            "fluid_results",
            "先回放零到零点三秒的速度场。颜色表示速度大小，箭头给出流动方向，右侧曲线同步显示变化。转动视角，可以观察不同位置的流速分布。再切换压力切片，并打开流线，沿着流动路径查看内部压力和速度变化。这里的网格、时间轴和工况参数，都对应刚才提交的叶轮任务。",
        ),
        (
            146,
            158,
            "从三维流场继续查看载荷与频谱",
            "loads",
            "这次计算还同时输出了壁面流体力和力矩。将时序与频谱放在一起，就能继续比较脉动幅值和频率特征，为运行工况分析提供更多依据。",
        ),
        (
            158,
            174,
            "回到刚才的管网，增加一次停泵分析",
            "trip",
            "回到第一个供水工程，再增加一次停泵事件。现在同时看最高用水点的压力和泵的转速响应。稳态运行结果和停泵后的变化，可以围绕同一套管网连续分析。",
        ),
        (
            174,
            180,
            "工程、任务与结果一并保存",
            "ending",
            "从三维建网，到本机求解，再到结果解读。FLOWSTATE，让工程方案真正算起来、看得见。",
        ),
    ]
    result = []
    folder = OUT / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    for i, (start, end, title, action, text) in enumerate(scenes):
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
            dict(
                start=start,
                end=end,
                title=title,
                action=action,
                text=text,
                audio=str(final),
                audio_duration=duration,
            )
        )
        print(i, title, round(duration, 2), flush=True)
    (OUT / "storyboard.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", action="store_true")
    args = parser.parse_args()
    if args.voice:
        voice()
        raise SystemExit(0)
    doc = network()
    save("network", doc.model_dump(mode="json"))
    result = simulate_design(doc)
    save("network_preflight", result)
    high = doc.model_copy(deep=True)
    high.links[0].speed_ratio = 1.1
    save("network_high_preflight", simulate_design(high))
    p = fluid_project()
    save("fluid_project", p.model_dump(mode="json"))
    print("NETWORK", result["summary"], result["points"][0]["min_pressure_m"], flush=True)
    t = time.monotonic()
    result = execute_cfd(p, OUT / "preflight_cfd", lambda f, m: print(round(f, 3), m, flush=True))
    save("fluid_preflight", result)
    print("CFD WALL SECONDS", time.monotonic() - t, result["summary"], flush=True)
