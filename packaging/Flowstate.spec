# PyInstaller spec: an offline, dynamically linked Qt application.
import os
import platform
import sys
from pathlib import Path
from importlib.metadata import distribution

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

gmsh_dist=distribution("gmsh")
gmsh_libs=[Path(gmsh_dist.locate_file(f)).resolve() for f in gmsh_dist.files or [] if str(f).endswith(("libgmsh.4.15.dylib","libgmsh.so.4.15","gmsh-4.15.dll"))]
assert gmsh_libs, "Missing Gmsh native library"

root = Path(SPECPATH).parent
wntr_root = Path(distribution('wntr').locate_file('wntr'))
if sys.platform == 'darwin':
    lib = 'darwin-arm/libepanet2.dylib' if platform.machine() == 'arm64' else 'darwin-x64/libepanet22.dylib'
elif sys.platform == 'win32':
    lib = 'windows-x64/epanet22.dll'
else:
    lib = 'linux-x64/libepanet22.so'
libpath = wntr_root / 'epanet' / 'libepanet' / lib
assert libpath.exists(), f'Missing EPANET solver library: {libpath}'
# Include only this architecture's EPANET 2.2 library, not other-platform or MSX binaries.
datas = [(str(root / 'examples'), 'examples'), (str(root / 'LICENSE'), '.'), (str(root / 'THIRD_PARTY_NOTICES.md'), '.')]
datas += [(str(root / 'resources'), 'resources'), (str(root / 'cfd_runtime'), 'cfd_runtime')]
datas += collect_data_files('wntr', includes=['**/*.json', '**/*.inp', '**/*.txt'])
datas += copy_metadata('wntr') + copy_metadata('ezdxf')
if (root / 'build' / 'licenses').exists():
    datas += [(str(root / 'build' / 'licenses'), 'licenses')]
a = Analysis(
    [str(root / 'gui.py')],
    pathex=[str(root)],
    binaries=[(str(libpath), str(Path('wntr/epanet/libepanet') / Path(lib).parent))] + [(str(p),'.') for p in gmsh_libs],
    datas=datas,
    hiddenimports=['scipy.special.cython_special', 'wntr.sim.aml._evaluator', 'wntr.sim.network_isolation._network_isolation'],
    hookspath=[],
    hooksconfig={'matplotlib': {'backends': ['Agg']}},
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'tkinter', 'pytest', 'fastapi', 'uvicorn', 'httpx', 'IPython', 'notebook'],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Flowstate', debug=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Flowstate')
if sys.platform == 'darwin':
    app = BUNDLE(coll, name='Flowstate.app', icon=None, bundle_identifier='org.flowstate.pumpnetwork', info_plist={'CFBundleShortVersionString':'2.0.1', 'NSHighResolutionCapable':True})
