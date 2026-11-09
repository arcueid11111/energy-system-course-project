FLOWSTATE v2：本机三维流体与弹性瞬变工作台。

新增 OpenFOAM Foundation 14 旋转/两相汽蚀/CAD 流域任务、真实 VTK 场回放与切片/矢量/流线、壁面流体力与频谱、汽蚀扬程扫描与 NPSH3、三维线性声学直管水锤基准、管网弹性 MOC、波形双向耦合、网格/步长校核和实测 CSV 比较。所有计算在使用者电脑完成，无远程计算服务。

下载 Windows x64、Apple Silicon macOS 或 Linux x64 应用。Intel Mac 不构建。OpenFOAM 功能需另装本机 OpenFOAM 14，或从本发行导入对应 CPU 架构的离线 Docker 镜像；普通管网、声学基准和场文件查看不需要 OpenFOAM。

数值基准和真实求解器短时输出不等于实机验证。周期叶轮只代表扇区载荷；未稳定汽蚀扫描、未收敛耦合、粗网格非线性水锤误差均明确报告。尚无真实设备 CAD/仪器数据，未完成整机经验校准、空蚀侵蚀率或结构强度验证。详见 docs/PHYSICS.md、docs/VERIFICATION.md、docs/CFD_INSTALL.md。
