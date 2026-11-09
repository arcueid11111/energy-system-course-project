"""Make a reviewable source archive from explicit project files only."""

import hashlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "gui.py",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "start.sh",
    "start.bat",
    "启动图形界面.command",
    ".gitignore",
]
FOLDERS = ["backend", "desktop", "scripts", "packaging", "examples", "tests", "docs", ".github", "resources", "cfd_runtime"]


def source_files():
    paths = [ROOT / name for name in FILES]
    paths += [
        p
        for name in FOLDERS
        for p in (ROOT / name).rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ]
    return sorted(p for p in paths if p.is_file())


if __name__ == "__main__":
    target = ROOT / "release" / "Flowstate-source.zip"
    target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in source_files():
            archive.write(file, Path("Flowstate-source") / file.relative_to(ROOT))
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
    print(
        f"{target.name}: {target.stat().st_size} bytes, SHA256 {hashlib.sha256(target.read_bytes()).hexdigest()}"
    )
