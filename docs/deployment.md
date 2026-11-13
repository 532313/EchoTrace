# 部署与恢复

2026-11-13 更新：EchoTrace 已作为原 Echo-Mate DeskBot 抽屉中的“记忆”应用在 RV1106 实板启动。集成构建、共享声画锁、当前二进制路径和恢复命令见 [DeskBot 集成说明](../integration/deskbot/README.md)。下文 `S96echotrace-terminal` 是此前独立显示方式，不能与集成 DeskBot 同时运行。当前开发板没有配置集成桌面的开机自启；正式镜像的 Buildroot 包仍是独立终端模式，不能直接把它当作已完成的应用集成镜像。

## 1. 已识别的开发环境

- Mac 工作区：`/Users/gsy/Desktop/嵌入式Linux智能安防监控系统/EchoTrace`。
- `ssh lubancat`：LubanCat-3-v2，RK3576，4GB，Linux 6.1.99，NPU 驱动 0.9.8。工程部署在 `/root/echotrace`，没有替换现有内核、运行库或 NAS 服务。
- `ssh wsl`：x86_64 Linux，SDK 位于 `/home/guan/Echo-Mate/SDK/rv1106-sdk`；融合构建位于 `/home/guan/EchoTrace`。
- Echo：`ssh -J wsl root@172.32.0.93`，Linux 5.10.110。测试程序位于 `/root/echotrace`。密码不写入项目。

当前 Echo USB 连接在 WSL 端，板间测试可通过临时 SSH 转发完成。最终独立工作需要把数据线接到鲁班猫的 USB Host；不能把 SSH 隧道测试写成已经验证的板间直连。

## 2. RV1106 构建

在 Linux/WSL 上准备 `build/deps`、`build/vendor`，并生成完整中文字库。Mac 侧可以先运行 `scripts/package_sources.py`，传输受控的源码包；包中不包含录像、token、模型和虚拟环境。

```sh
export ECHO_SDK=/home/guan/Echo-Mate/SDK/rv1106-sdk
cmake -S . -B build/rv1106 \
  -DCMAKE_TOOLCHAIN_FILE=cmake/rv1106-toolchain.cmake \
  -DECHO_RV1106=ON -DECHO_BUILD_LVGL=ON \
  -DECHO_LVGL_ROOT=/home/guan/Echo-Mate/Demo/DeskBot_demo/lvgl \
  -DECHO_MEDIA_SOURCE_ROOT=/home/guan/Echo-Mate/Demo/rkmpi_demos \
  -DCMAKE_BUILD_TYPE=MinSizeRel
cmake --build build/rv1106 --target echotrace-edge echotrace-terminal -j4
```

使用与固件一致的 Buildroot sysroot。单独拷贝 x86_64 编译器到 Mac 不能执行。媒体库从原 SDK/工程查找，禁止把 RK3576 的 aarch64 库用于 RV1106。

可将 `bsp/buildroot-external` 作为 `BR2_EXTERNAL` 加载，在 menuconfig 中填写源码目录并启用 EchoTrace。默认模式仍构建独立 LVGL 终端，安装 `S95echotrace` 和 `S96echotrace-terminal`。若要把 EchoTrace 作为原桌面 App，先按 [DeskBot 集成说明](../integration/deskbot/README.md)在隔离目录完成构建，再启用 `BR2_PACKAGE_ECHOTRACE_DESKBOT`，将 `BR2_PACKAGE_ECHOTRACE_DESKBOT_BIN` 指向其 `build-echotrace/bin`。该模式只构建采集服务并安装集成桌面资源，启动脚本改为 `S96echotrace-deskbot`；两种显示脚本不会同时由此包安装。外部包排除模型、录像、虚拟环境、token 与宿主构建产物，仍保留所需的锁定依赖和字库。**整机镜像尚未构建或刷写验证**；安装后仍需设置私有 token、确认 SD 挂载、网络和原有桌面启动项，才能让开机服务正常运行。

复制到新目录后先运行 `echotrace-core-test`。原有相机、录音、GUI 服务需要逐一核对设备所有权；本次实板测试时没有正在运行的旧 GUI。融合终端不链接电机控制模块。

## 3. 存储卡和常驻服务

2026-11-12 已在 Echo-Mate 识别到 64GB SD 卡：`/dev/mmcblk1p1` 实际挂载于 `/mnt/sdcard`，FAT32，可用文件系统容量约 58.05GiB。保留卡上已有文件系统，没有格式化。约 10 秒的独立片段不会触及 FAT32 单文件 4GiB 上限。正式服务 `S95echotrace` 要求 `/mnt/sdcard` 是已挂载的文件系统，防止挂载失败后误写根分区。

按 8Mbit/s 视频加 PCM 预算约 3.7GB/小时；实际编码码率随画面变化，不能直接把该预算当作实测值。当前 FAT32 卡可以继续使用；本工程不自动分区或格式化任何设备。

新增的存储检查工具会核对块设备、挂载点、根分区隔离和只读状态，写入探针只创建并删除自己的随机命名文件。它不是全卡容量鉴定工具：

```sh
python3 scripts/check_storage.py --mount /mnt/sdcard --device /dev/mmcblk1p1 \
  --write-mib 16 --output /root/echotrace/storage-preflight.json
```

迁移必须先停止采集。`scripts/migrate_edge_data.py` 只接受 SD 上的新目标目录，逐文件校验，保留来源目录和原始日志字节，只改写私有清单中的绝对路径。本次已从 `/root/echotrace/smoke` 迁移到 `/mnt/sdcard/echotrace`，旧目录保留。日常数据根目录使用后者。

`scripts/start_sd_validation.py` 提供有时长上限的独立实板测试启动方式，`bound_capture.sh` 核对 PID、启动时间和可执行文件后，依据 `/proc/uptime` 的启动时钟发送 TERM。该安全时限包含系统挂起时间；媒体来源时间仍使用 `CLOCK_MONOTONIC`。网络断开不会取消本地录制和到期停止；它未替代正式开机部署。

早期独立终端验证的进程与摘要保存在 [历史部署快照](deployment-current.json)。2026-11-13 已切换为集成 DeskBot 开发安装；当前程序在 `/root/echotrace/deskbot-app/bin/main`，采集仍为 `/root/echotrace/echotrace-edge-integrated`。GUI PID 文件为 `/run/echotrace-deskbot.pid`，采集 PID 文件为 `/run/echotrace-capture-manual.pid`；控制 socket 为 `/run/echotrace-sd.sock`。实际 PID 和连接状态每次操作前须重新核对。没有启用集成桌面开机启动；当前连接仍经过 WSL 和 Mac SSH 转发，用户选择保持现有接法。最新短测见 [DeskBot 集成实板记录](deskbot-integration-test-2026-11-13.md)。

此前的独立终端模式可单独重启界面，且已在实板确认不改变采集 PID；下列命令**仅在集成 DeskBot 已停止时**用于回退：

```sh
ECHOTRACE_PREFIX=/root/echotrace ECHOTRACE_RUN_DIR=/run/echotrace-terminal \
  ECHOTRACE_SOCKET=/run/echotrace-sd.sock \
  /root/echotrace/S96echotrace-terminal restart
```

停止后可执行原件巡检。该工具检查所有清单和文件摘要、WAV 样本数、单轨分片 MP4 的样本表及未完成/漏登记文件；不声称完成全部视频解码或声学同步测量：

```sh
python3 scripts/audit_edge_session.py --data /mnt/sdcard/echotrace \
  --session SESSION_ID --verify-sha256 --output /root/echotrace/session-audit.json
```

复制 `config/edge.example.json`，设置采集设备、令牌文件和数据根目录。保持 token 文件权限 0600。安装服务时将程序放到 `/opt/echotrace/bin`；保留恢复用旧镜像与串口通路。

## 4. USB 网络

当前设备的实际识别信息、私有令牌预检和切换步骤见 [双板 USB 直连验证](usb-direct-validation.md)。已完成鉴权准备，物理直连仍未执行。

先在鲁班猫 `ip -br link` 核对新出现的 RNDIS 接口。在现有 USB 地址旁添加私有地址，不先删除当前调试地址：Echo `192.168.77.2/24`，鲁班猫 `192.168.77.1/24`。该网络不设置互联网默认路由。相互 ping 通后，启动中枢并将 edge 的 `--hub` 指向 `ws://192.168.77.1:18081/ws/device`。

对外监听必须配置 token。完成实测后再把网络配置纳入与实际发行版匹配的启动体系；不能在现有 NAS 上混用 NetworkManager 与 networkd 管理同一接口。

## 5. RK3576

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[ai]'
cmake -S . -B build/rk3576 -DECHO_BUILD_QT=ON -DECHO_BUILD_VLM=ON
cmake --build build/rk3576 -j4
.venv/bin/python scripts/seal_models.py --check
.venv/bin/python -m echotrace serve --data runtime/real --models models/config.json
build/rk3576/echotrace-workbench --hub http://127.0.0.1:18081
```

Qt 需要 Qt5 Widgets/Network 与 FFmpeg 开发包；NPU 推理需要 `scripts/fetch_rkllm.py` 提供的私有 SDK 目录。该目录通过 RPATH 使用，不覆盖 `/usr/lib/librknnrt.so`。

服务模板以独立账户运行，部署时配置相机/NPU/DMA 设备访问权限；开发目录的 root 短测不等于已经完成生产服务加固。没有自动启用开机启动。

## 6. BSP 与版本兼容

功能使用 Echo-Mate 自带的摄像头、麦克风、扬声器和触摸屏。标记及暂停/继续均由 App 操作，不需要外接输入设备。原电机和蓝牙引脚不重新分配。

当前 SDK 默认 SD_CARD 板型与实板 SPI NAND / UBIFS 启动存在差异，整机镜像构建前需固定匹配的启动配置，见 [BSP 基线](bsp-validation.md)。内核配置片段保留触摸事件和 USB 网络所需能力。

从 `0.2.0.dev1` 开始，新步骤事件使用 `mark`。部署时先更新 RK3576 中枢，再更新 RV1106 采集端；新中枢仍接受旧事件日志。已有配置中的旧外设字段会被忽略，推荐采用新的示例配置。安装版本和产物摘要按 [发布流程](releases.md) 记录。

## 7. 恢复

正常停止先关闭会话，等待 `.part` 完成。异常退出后，服务修复 JSONL 的截断尾部、恢复未完成任务，并将旧会话明确标为中断/结束；不伪造丢失的采样时间。原 MP4 分片恢复逻辑保留，无法恢复来源时间的孤立片段不自动包装成可信记忆。

USB 断开不终止原件记录。中枢重连按不可变 ID 补传；缓存损坏会被长度/摘要校验标记。存储低于保留空间时停止新增记录并报告故障，不自动覆盖尚未确认的历史。
