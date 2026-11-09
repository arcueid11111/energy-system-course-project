# 三维流体力学、脉动力与水锤（v2）

本版把三维泵内流动、管网瞬变和汽蚀扬程损失放进同一个本机任务队列。所有求解、保存、后处理均在使用者电脑完成。OpenFOAM 是本机求解器进程或本机容器，不是远程服务器。没有设备原始 CAD、泵试验台数据、材料和仪器标定资料时，不能宣称“真实设备数字孪生已验证”。

## 模型及对应输出

| 路径 | 方程与方法 | 输出及边界 |
|---|---|---|
| 原有网络 | EPANET 2.2，Darcy–Weisbach，压力驱动需水 | 稳态/准稳态节点、泵与管道工况；不用于水锤 |
| 单相旋转三维 | OpenFOAM Foundation 14 `incompressibleFluid`，运动网格、NCC 转静接口，层流或 k–ω SST URANS | 真实网格的 U、p，旋转壁面压力及黏性力、力矩；不可解析气泡微射流 |
| 两相旋转 / 节流 | `compressibleVoF`，液相 Tait 状态方程，蒸汽相，`compressible::VoFCavitation`/Schnerr–Sauer 相变源项 | U、绝压、蒸汽体积分数、两相压力载荷；核密度、核直径及相变系数需标定 |
| 三维直管水锤参考 | 自带三维线性声学有限体积，三分量速度、压力与声学 Riemann 面通量 | 小马赫数无摩阻直管内压力波与速度场；忽略对流、黏性、汽蚀，不替代完整 NS |
| 三维非线性水锤 | 可选 OpenFOAM 两相可压缩路径 | 保留真实计算与误差报告；当前粗网格基准峰值误差较大，不标记已验证 |
| 管网水锤参考 / 耦合 | 一维弹性特征线法 MOC，CFL≤1，管壁柔度、Darcy 摩阻、设备边界及水箱储水 | 压力波、泵停机惯性降速、阀门开闭；单相压力低于蒸汽压会报警，绝不强行截断压力 |

三维声学模型求解 `∂p'/∂t + K_eff div(u)=0`、`∂u/∂t + grad(p')/ρ=0`。网格由中心三棱柱和外圈六面体构成，截面为圆的多边形逼近。它确实更新三维单元面通量与三分量速度；对默认均匀初场、直管和均匀关阀边界，物理解本来就接近轴向一维传播。不能用它解释弯头二次流、叶轮涡旋或气泡坍塌。

波速：`a = [ρ(1/K + C D/(E e))]^-1/2`；`C` 是由约束条件确定的管壁柔度系数。`PipeWall.poisson` 为材料记录，当前不从泊松比自动推导约束系数，用户必须明确指定 `restraint_factor=C`。该等效柔度不是固体有限元流固耦合，不输出管壁位移或应力。快速关阀的无摩阻、首次反射前基准为 `Δp=ρ a Δu`，往返时间 `2L/a`。

## 几何及载荷范围

自带径向叶轮采用官方教程的 **30° 周期扇区**，转子网格实际运动。`flow_m3h` 为整周等效流量，扇区流量为它的 1/12；压力和速度不乘 12。力积分是扇区载荷，**不能把力矢量简单乘 12 当作整泵径向冲击力**。只有周期一致条件下绕旋转轴的力矩可乘 12 作整周等效。没有完整蜗壳就不能模拟真实蜗舌交互及真实整泵径向不平衡力。

载荷由 OpenFOAM `forces` 对指定壁面压力和黏性应力积分。提供 Fx/Fy/Fz、Mx/My/Mz，启动段舍弃后的均值、脉动 RMS、峰峰值、相对初值的冲量和 Hann 窗 FFT。FFT 的频率分辨率、Nyquist 频率和是否重新均匀采样均随结果保存。短时启动输出不能代替稳定叶频谱；叶频是 `rpm × 叶片数 / 60`，不代表 FFT 已解析出该频率。

直管快速关阀报告的力为出口封闭边界的压力合力，是理想阀面载荷。它不包含阀瓣接触冲击、阀杆结构动力学、支架应力或真实弯头反力。

## CAD 导入

原有“导入 CAD / DXF”读取二维或三维**管道中心线**，用于网络拓扑；这不能变成泵内 CFD 流体域。

新“导入 STEP 流体域”要求 CAD 中已经提取出一个封闭流体体积。界面列出每个面的编号、面心；用户指定 inlet/outlet/walls/rotor，并选择 m/mm。Gmsh 将指定面转换为分区 STL，VTK 检查合并表面是否闭合、是否非流形，以及流体种子点是否位于流体内部。默认包围盒中心仅是建议，必须核对，特别是叶轮实体占据中心的情况。

也可直接准备 `inlet.stl`、`outlet.stl`、`walls.stl`，旋转 CAD 另加 `rotor.stl`、封闭的 `rotatingInterface.stl`；后者定义转子体网格区域，不是叶片实体表面。高级 JSON 中设定 `cad_rotating`、`cad_cavitation`、`rotor_origin`、`rotor_axis`、`cad_bounds`、`fluid_seed`。STL 不能携带可靠单位，必须显式选择 m/mm。当前 CAD 自动网格没有自动边界层生成或 y+ 自适应，真实工程必须另做边界层及网格独立性校核。

## 汽蚀造成的扬程下降

扫描在相同 Q、rpm 下逐步降低入口**绝对压力**，读取两相求解结果。端口扬程来自求解器每个时间步的通量加权压力、速度平方；避免用可视化稀疏帧计算统计均值。`H = <p/(ρg)+u²/(2g)+z>_out - <...>_in`；含重力的两相路径使用 p_rgh 表示相应位能。标准泵性能定义假定端口主要为液相，端口有明显蒸汽含量时需采用两相混合物能量流定义进一步扩展。

`NPSHa=(p_abs-p_v)/(ρg)+u²/(2g)+z_s-z_datum`。扫描生成 `(Q,n,NPSHa,H/H0)` 图谱，同一 Q/n 至少 4 点，采用保形插值，不对汽蚀余量、流量或转速范围外外推。通过 `H/H0=.97` 求 NPSH3。**NPSH3 是 3% 扬程下降，不是汽蚀初生。**

扫描至少覆盖 3 转；舍弃前半段，对后半段再分两窗比较平均扬程，漂移超过 2% 或端口 Q 偏离目标超过 1% 时，`accepted_for_reduced_model=false`。这只是最低稳定性门槛，仍需更多转数、网格/步长收敛、空化核参数敏感性与实测标定。图谱不自动替换默认泵曲线。把经过审核的完整图谱放进瞬变设置的 `cavitation_maps.P1`，则运行中以实时入口压力、Q 和 n 反馈扬程下降；范围不足会停止并说明原因。

## 双向耦合

CFD 的两个压力端口由弹性管网节点绝压驱动；CFD 两端速度自由求解，**不同时强制指定端口压力和流量**。求得的入口/出口流量分别反馈 MOC，保留两相可压缩体积的储存差异。离线波形迭代保存每次界面流量和转速残差，采用松弛和加速；达到指定容差才标记 `converged=true`。未收敛任务也保留诊断数据，但不是已求得的耦合解。

选用 `motor_torque_nm=[[秒,力矩],...]` 时，用 `J dω/dt = T_motor + T_fluid - T_loss` 回馈转速，流体力矩按旋转轴投影。流体力矩的符号不强制取绝对值，反转会停止并要求四象限模型。没有电机力矩时是规定转速启动/边界耦合，不是自由停泵惯性耦合。

初场必须一致。`start_from_rest=true` 只允许所有水源/水箱初始水头一致且无节点需水的管网；转速从零按启动斜坡增长。运行态停泵耦合需要已稳定的整数转数 CFD 检查点，且初始流量、入口/出口绝压与网络工况匹配。检查点只接受相同网格和介质，不能拿任意旧流场凑初值。使用自带低扬程耦合网络，不能把默认 2900 rpm / 32 m 假设泵曲线直接当作 600 rpm 扇区 CFD 的曲线。

## 网格、时间步与实测验证

任务菜单提供三层网格和时间步收敛。报告相邻细化结果的扬程、峰值载荷、压力变化及单调性；非单调时不伪造 Richardson 外推/GCI。短时输出收敛不等于稳定工况的统计收敛，细化网格还应同步核对时长、转角步长和 CFL。

实测 CSV 必含 `seconds`，其余列支持 `pressure_pa`（绝压）、`flow_m3h`、`head_m`、`force_x_n`/y/z、`moment_x_nm`/y/z、`rpm`。填写仪器、测点、压力参考、采样率和来源。比较只在重叠物理时间上插值，不在时间外推、不自动平移峰值；输出 RMSE、偏差、最大误差和峰值时刻差。允许提供按单位定义的 RMSE 容差；用户声明来源不会自动变成独立认证的测量。

仍需用户提供：真实流体域 CAD（叶轮/蜗壳/间隙/吸排管）、壁厚与材料和约束、实际工况曲线、电机/转子惯量、同步压力/流量/转速/功率/载荷数据，以及传感器精度和试验程序。当前没有这些数据，不能完成实机校准、结构强度/寿命预测或空蚀侵蚀率验证。

## 文献与实现依据

- OpenFOAM Foundation 14 [官方版本说明](https://openfoam.org/version/14/)与[用户手册](https://doc.cfd.direct/openfoam/user-guide/contents)。本项目锁定 Foundation 分支，不能混用 OpenCFD 的求解器名称/字典。
- 模板和 Schnerr–Sauer 源项实现：[OpenFOAM-14 固定源码提交](https://github.com/OpenFOAM/OpenFOAM-14/tree/31554318f4674a8538e6e05bb899332151ef138f)。复制的输入模板范围、许可证见 resources/cfd/PROVENANCE.md。
- Schnerr, G.H.; Sauer, J. (2001), *Physical and Numerical Modeling of Unsteady Cavitation Dynamics*, ICMF 2001。这里使用 OpenFOAM 实现，系数并非已适配真实设备。
- Ghidaoui, M.S. et al. (2005), *A Review of Water Hammer Theory and Practice*, Applied Mechanics Reviews 58, 49–76, DOI 10.1115/1.1828050；[公开论文](https://hydraulics.unibs.it/hydraulics/wp-content/uploads/2012/04/A_review_of_water_hammer_theory_and_practice.pdf)。
- [COMSOL 官方水锤验证算例](https://doc.comsol.com/6.3/doc/com.comsol.help.models.pipe.water_hammer_verification/water_hammer_verification.html)：弹性波速、首次压力波与 Joukowsky 验证边界。此项目没有依赖 COMSOL。
- [KSB 汽蚀词典](https://www.ksb.com/en-global/centrifugal-pump-lexicon/article/cavitation-1117364)与[吸入特性](https://www.ksb.com/en-global/centrifugal-pump-lexicon/article/suction-characteristics-1117252)：NPSH3、初生及损伤判据的区别。
