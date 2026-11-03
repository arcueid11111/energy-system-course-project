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
