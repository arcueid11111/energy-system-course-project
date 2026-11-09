# 本机求解与离线部署

下载应用：Windows x64、Apple Silicon Mac、Linux x64。没有 Intel Mac 包。原有网络设计/求解、一维弹性瞬变、内置三维声学基准、真实场文件查看均可独立运行；OpenFOAM 的叶轮/两相/CAD 自动网格任务需要同一电脑上的 OpenFOAM 14 环境。

## 桌面使用

启动 `gui.py` 或解压后的 Flowstate 应用，打开“三维物理与瞬变”。选择算例，填写转速、流量、绝压、网格和时间参数，检查本机求解器后提交任务。完整 JSON 提供管壁、汽蚀核、探针、CAD 分区和高级参数；上方表单同名字段会覆盖 JSON。任务选项 JSON 设置扫描压力、收敛容差、停泵/关阀事件和耦合条件。

所有任务在“统一任务调度”查看日志、暂停排队、取消及读取结果；物理页也有历史列表。时刻滑条、压力/速度/蒸汽率、XYZ 切片、矢量和流线直接读取求解器网格场。载荷、扬程、流量、探针压力可回放及 FFT。字段不可用时明确提示，不生成替代速度场。色标是当前帧当前切片范围，跨帧比较须核对数值范围。

“加载真实场示例”读取随包附带的 480 单元、0.002 s 启动算例，仅证明真实文件读取与交互，不是稳定泵性能。

## 方案 A：本机 Docker 镜像

本机安装 Docker Desktop / Docker Engine；Apple Silicon 也可使用 Colima。界面 Docker context 留空沿用当前 context，或填写 `colima-flowstate-cfd`。不用启动服务器或开放端口。

发行页若包含对应架构镜像包，先解压 gzip，再导入：

```sh
gunzip Flowstate-openfoam14-arm64.tar.gz
docker load --input Flowstate-openfoam14-arm64.tar
```

x64 Windows/Linux 选择 amd64 镜像；Mac M 系列选择 arm64。镜像名均为 `flowstate-openfoam:14`。镜像及 Docker 本体可在另一台联网电脑下载后移动到离线计算机。GUI 运行容器时禁网、只挂载当前算例目录、不映射服务端口。

也可联网一次从官方签名软件源构建（不需要安装 Python）：

```sh
docker build -t flowstate-openfoam:14 cfd_runtime
```

源码用户还可以 `python scripts/cfd_runtime.py build --context colima-flowstate-cfd`；`probe` 检查版本，`save --archive image.tar` 导出离线镜像，`load --archive image.tar` 导入。脚本不改变全局 Docker context。

镜像构建会访问官方 Ubuntu/OpenFOAM 软件源；实际求解不访问网络。若采用 Colima，启动独立 profile，例如 `colima start flowstate-cfd --cpus 2 --memory 3 --disk 20 --vm-type vz --activate=false`。不同 Colima 版本/本机 DNS 的网络问题应在该 profile 内诊断，不需要修改系统全局 DNS。

## 方案 B：原生 Linux / Windows WSL

按 [OpenFOAM Foundation 官方下载说明](https://openfoam.org/download/)安装 **14**。在 GUI 中选 native 或 wsl；bashrc 默认为 `/opt/openfoam14/etc/bashrc`，WSL 发行版默认 Ubuntu，可在完整参数 JSON 修改。仅支持 Foundation 14 字典，OpenCFD v24xx/v25xx 不是同一版本。

## 源码运行

Python 3.12，建议在项目目录建虚拟环境：

```sh
python -m venv .venv
# macOS/Linux
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python gui.py
# Windows 使用 .venv\Scripts\python.exe
```

Linux Gmsh 需要 libGLU；例如 `sudo apt install libglu1-mesa libegl1 libgl1 libxkbcommon0`。所有 GUI 检查均可 offscreen，无需打开实际窗口：`python gui.py --self-check build/check.json`。

CLI 与 GUI 共享实现：

```sh
python scripts/physics.py cfd --project examples/cfd/water_hammer.json
python scripts/physics.py cfd --project examples/cfd/impeller.json
python scripts/physics.py transient --network examples/editable_network.json
python scripts/physics.py coupled --project examples/cfd/coupled-project.json --network examples/cfd/coupled-network.json --options examples/cfd/coupled-options.json
```

耦合示例中的时间窗口必须与项目时间一致；使用对应的 `coupled-project.json`。完整的真实设备运行态需要准备匹配检查点，见 PHYSICS.md。

## 数据迁移

每个 CFD 任务有独立算例目录，保留 manifest 输入哈希、0/constant/system、求解器日志、VTK 场、postProcessing 和 result.json。JSON 导出不是完整场数据包。迁移到另一台电脑时复制**整个算例目录**，在 GUI 选择该目录里的 result.json；原绝对路径不存在时会使用 JSON 所在目录。SQLite 中的历史索引属于本机；JSON 和算例目录可以脱离数据库读取。

高分辨率工程会消耗大量 CPU/内存/磁盘。默认 Docker 上限 2 CPU/3 GB；可在 runtime 参数中调整。GUI 只对显示抽样，原始场保留。中止任务保留不完整目录供排查，不把它计作有效完成结果。
