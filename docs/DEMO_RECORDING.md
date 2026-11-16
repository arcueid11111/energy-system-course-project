# 可选演示制作工具

这些脚本用于制作原生 Qt 窗口的实操视频，与应用运行、测试和打包分开。安装 `requirements-demo.txt` 后才需要媒体依赖。中文旁白使用 macOS 的 `/usr/bin/say` 和 Tingting 语音，因此当前制作流程适用于 Mac。

`prepare_live_cases.py` 定义三维泵网与 600 rpm 叶轮扇区案例，生成预检结果；`record_live_cases.py` 通过原生控件新建并提交水力、批量、CFD 和瞬变任务，保留任务 ID、参数哈希和场文件路径。`verify_live_cases.py` 核对记录与成片，`finalize_live_demo.py` 生成交付副本。其余 customer/demo 脚本保留为旧版制作工具，依赖对应的历史素材。

制作前需按本机环境修改 `prepare_live_cases.py` 中的 Docker 路径和 context，准备本机 OpenFOAM 14。首次准备与正式录制都会真实计算 CFD，运行时间取决于本机性能。请先阅读各脚本的命令行选项与输入路径，再运行。

录制输入、求解目录、音频和视频保存在项目下的 `演示视频/`，不纳入仓库和源码发行包。制作脚本不属于图形应用的启动依赖。
