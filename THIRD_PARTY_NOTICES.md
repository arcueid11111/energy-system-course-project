# Third-party components

The MIT license in this repository applies to FLOWSTATE's original code. It does
not replace the licenses of bundled dependencies. Distribution archives include
the license/notice files extracted from installed dependency distributions.

| Component | Purpose | Upstream |
|---|---|---|
| Qt / PySide6 Essentials / Shiboken | Desktop widgets and rendering | https://doc.qt.io/qtforpython-6/ |
| WNTR / EPANET | Hydraulic network solving | https://github.com/USEPA/WNTR |
| NumPy / SciPy | Numerical calculations and calibration | https://numpy.org/ / https://scipy.org/ |
| NetworkX | Connectivity checks | https://networkx.org/ |
| Pydantic | Engineering-document validation | https://docs.pydantic.dev/ |
| ezdxf | CAD centreline parsing | https://ezdxf.readthedocs.io/ |
| PyInstaller | Executable packaging | https://pyinstaller.org/ |

Qt libraries are dynamically linked in a one-folder distribution. See the upstream
Qt project for corresponding source and licensing information, and the license
texts shipped in the application's `licenses` folder.


v2 adds VTK (BSD-style license) for field I/O and Gmsh 4.15.2 (GPL v2 or later, with its upstream exceptions) for STEP geometry. The application bundles their native libraries and extracted license texts; the original FLOWSTATE code remains available in the source archive under MIT, which is compatible with GPL distribution. Gmsh corresponding source: https://gmsh.info/src/gmsh-4.15.2-source.tgz . The release includes that source archive alongside the desktop distributions; see upstream https://gmsh.info/ for build instructions and license terms. No additional restriction is imposed on replacing or modifying the distributed libraries.

The pinned OpenFOAM tutorial input dictionaries in `resources/cfd/openfoam14` and their adaptations are GPL v3 or later; see `resources/cfd/OPENFOAM-COPYING.txt` and `PROVENANCE.md`. OpenFOAM itself is a separate local process/container, GPL v3 or later, from https://github.com/OpenFOAM/OpenFOAM-14 . The image installs the official signed package; its installed build/version is recorded by each case log. Corresponding OpenFOAM source and Debian build recipes are available from https://dl.openfoam.org/ and the upstream repository. Ubuntu packages in the separately distributed image retain their package-specific licenses and `/usr/share/doc/*/copyright` notices.
