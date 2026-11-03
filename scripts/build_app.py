"""Build and self-test an application that includes its Python and solver runtime."""

import argparse
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def copy_licenses():
    target = ROOT / "build" / "licenses"
    target.mkdir(parents=True, exist_ok=True)
    for name in [
        "wntr",
        "PySide6-Essentials",
        "shiboken6",
        "numpy",
        "scipy",
        "pandas",
        "networkx",
        "pydantic",
        "ezdxf",
        "matplotlib",
        "six",
    ]:
        dist = importlib.metadata.distribution(name)
        for entry in dist.files or []:
            lower = str(entry).lower()
            if not any(part in lower for part in ("license", "licence", "copying", "notice")):
                continue
            source = Path(dist.locate_file(entry))
            if source.is_file() and source.stat().st_size < 2_000_000:
                destination = target / name / str(entry).replace("..", "_")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-check", action="store_true")
    args = parser.parse_args()
    copy_licenses()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            str(ROOT / "packaging" / "Flowstate.spec"),
        ],
        cwd=ROOT,
        check=True,
    )
    if sys.platform == "darwin":
        name = "macos-" + ("arm64" if platform.machine() == "arm64" else "x64")
        executable = ROOT / "dist" / "Flowstate.app" / "Contents" / "MacOS" / "Flowstate"
        bundle = ROOT / "dist" / "Flowstate.app"
        # Finder metadata inherited from a local source checkout must not invalidate
        # the ad-hoc bundle signature. This touches only our freshly built output.
        for attribute in ("com.apple.FinderInfo", "com.apple.ResourceFork"):
            subprocess.run(
                ["/usr/bin/xattr", "-r", "-d", attribute, str(bundle)], capture_output=True
            )
        subprocess.run(
            [
                "/usr/bin/codesign",
                "--force",
                "--deep",
                "--sign",
                "-",
                "--timestamp=none",
                str(bundle),
            ],
            check=True,
        )
        subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)], check=True
        )
    elif sys.platform == "win32":
        name = "windows-x64"
        executable = ROOT / "dist" / "Flowstate" / "Flowstate.exe"
    else:
        name = "linux-x64"
        executable = ROOT / "dist" / "Flowstate" / "Flowstate"
    if not args.no_check:
        report = ROOT / "build" / f"self-check-{name}.json"
        result = subprocess.run(
            [str(executable), "--self-check", str(report)],
            cwd=ROOT,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "MPLBACKEND": "Agg"},
            timeout=120,
        )
        if (
            result.returncode
            or not report.exists()
            or json.loads(report.read_text())["status"] != "passed"
        ):
            raise SystemExit(f"Frozen application self-check failed: {report}")
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    if sys.platform == "darwin":
        subprocess.run(
            [
                "/usr/bin/ditto",
                "-c",
                "-k",
                "--sequesterRsrc",
                "--keepParent",
                str(ROOT / "dist" / "Flowstate.app"),
                str(release / f"Flowstate-{name}.zip"),
            ],
            check=True,
        )
    elif sys.platform == "win32":
        shutil.make_archive(str(release / f"Flowstate-{name}"), "zip", ROOT / "dist", "Flowstate")
    else:
        with tarfile.open(release / f"Flowstate-{name}.tar.gz", "w:gz") as archive:
            archive.add(ROOT / "dist" / "Flowstate", arcname="Flowstate")
    print(f"Application archive ready: Flowstate-{name}")


if __name__ == "__main__":
    main()
