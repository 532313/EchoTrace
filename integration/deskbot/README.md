# EchoTrace 作为 Echo-Mate 桌面应用

当前安装和启动请优先阅读 [dev2 安装记录](../../docs/deployment-dev2.md)，日常使用见 [小屏操作](../../docs/small-screen.md)。本文下半部分保留 2026-11-13 的历史安装路径与旧链路。

## 集成边界

实际桌面源码是 WSL 的 `/home/guan/Echo-Mate/Demo/DeskBot_demo`。`baseline.json` 记录了打补丁前九个文件的 SHA-256（其中三个来自相邻的 `yolov5_demo`）；Mac 上的同名副本较旧，不能直接覆盖 WSL 版本。

`0001-echotrace-app.patch` 只在设置 `ECHOTRACE_APP_ROOT` 时生效：保留原有 12 个页面及其位置，在抽屉末尾增加“记忆”，并从原聊天和 AI 相机入口协调声画设备。未设置该选项时，原桌面构建行为保持原样。原电机功能及引脚不作修改，记忆 App 使用触屏与语音操作。

`0004-app-launch.patch` 增加可选的 `ECHOTRACE_OPEN_APP` 启动目标，默认仍进入原首页。

`0002-ai-camera-backend.patch` 和 `0003-ai-camera-page.patch` 修复隔离构建中的原 AI 相机失败路径。之前上板遇到 NPU 连续内存申请失败，随后出现内核 Oops；完整日志在 `docs/kernel-oops-2026-11-13.log`。补丁检查模型和缓冲分配结果，按 RKNN API 要求先释放张量内存再销毁上下文，避免重复释放，并限制检测文字长度。页面在启动前检查 `/proc/buddyinfo`，启动或推理失败时提示并退出。内核驱动故障是否完全消除仍须实板复测，用户态检查不能替代驱动修复。

EchoTrace 页面通过本机 Unix socket 与常驻 `echotrace-edge` 通信。离开页面不会结束录制；停止会话才释放相机和麦克风。录制或证据音频播放时，进入原聊天、AI 相机会提示先结束；原聊天或 AI 相机占用设备时，EchoTrace 拒绝开始录制。CameraPage 当前只是原工程占位页面，因此没有加设备锁。

## 隔离构建

保留原工程源码和 `bin/main`。推荐在 WSL 用脚本建立隔离副本、核对九个原文件哈希、打补丁、执行相机失败路径测试并交叉编译。输出路径必须不存在；脚本不会清理或覆盖原工程：

```sh
sh /home/guan/EchoTrace/integration/deskbot/build_integrated.sh \
  /home/guan/EchoTrace/build/deskbot-integration-new
```

2026-11-13 已用全新目录实际跑通，输出位于 `/home/guan/EchoTrace/build/deskbot-integration-rebuild-20261113/`。两份隔离构建的 DeskBot 源码逐文件相同，二进制大小均为 9,374,092 字节；二进制 SHA-256 不同，因此当前只声明**可从固定源码重新构建**，不声明位级可重复构建。下面是脚本执行的主要步骤，便于审查或手动复现：

```sh
src=/home/guan/Echo-Mate/Demo
work=/home/guan/EchoTrace/build/deskbot-integration
mkdir -p "$work"
rsync -a --exclude '/build/' --exclude '/bin/' \
  "$src/DeskBot_demo/" "$work/DeskBot_demo/"
rsync -a --exclude '/model/' --exclude '/cpp/3rdparty/' \
  --exclude '/cpp/lib/' "$src/yolov5_demo/" "$work/yolov5_demo/"
ln -s "$src/AIChat_demo" "$work/AIChat_demo"
ln -s "$src/yolov5_demo/model" "$work/yolov5_demo/model"
ln -s "$src/yolov5_demo/cpp/3rdparty" "$work/yolov5_demo/cpp/3rdparty"
ln -s "$src/yolov5_demo/cpp/lib" "$work/yolov5_demo/cpp/lib"
patch -d "$work/DeskBot_demo" -p1 < /home/guan/EchoTrace/integration/deskbot/0001-echotrace-app.patch
patch -d "$work" -p1 < /home/guan/EchoTrace/integration/deskbot/0002-ai-camera-backend.patch
patch -d "$work/DeskBot_demo" -p1 < /home/guan/EchoTrace/integration/deskbot/0003-ai-camera-page.patch
patch -d "$work/DeskBot_demo" -p1 < /home/guan/EchoTrace/integration/deskbot/0004-app-launch.patch
cd "$work/DeskBot_demo"
cmake -S . -B build-echotrace -DTARGET_ARM=ON \
  -DECHOTRACE_APP_ROOT=/home/guan/EchoTrace -DCMAKE_BUILD_TYPE=Release
cmake --build build-echotrace -j4
cp /home/guan/Echo-Mate/Demo/DeskBot_demo/bin/IU.jpg build-echotrace/bin/IU.jpg
```

在已经建好的隔离目录上增量开发时，不要重复执行 `rsync` 和 `patch`。相机失败路径测试命令：

```sh
sh /home/guan/EchoTrace/integration/deskbot/tests/run_yolo_failure_test.sh \
  /home/guan/EchoTrace/build/deskbot-integration/yolov5_demo/cpp
```

测试用 RKNN mock 注入分配失败、设置内存失败和错误的张量数量；在 WSL 主机上使用 ASan/UBSan 检查清理路径。它不能模拟 RV1106 内核 DMA 分配行为。

集成构建产物位于 `build-echotrace/bin`。保留其中的 `main`、`system_para.conf`、`gaode_adcode.json`、`cacert.pem`、`IU.jpg`、`model/`、`third_party/` 和 `lib/`；程序使用相对路径读取原桌面资源。EchoTrace 需要预先生成的 `build/vendor/fonts/echotrace_font.c`，缺失时 CMake 明确报错。

采集服务需用同一份源码重新交叉编译，以启用 `/run/echotrace-media.lock`；单独替换桌面会失去双向资源保护。锁测试 `native/tests/media_lease_test.c` 覆盖两个进程争用和释放后重获。

## 当前开发板状态

2026-11-13 的开发安装位于 `/root/echotrace/deskbot-app/bin/main`，采集服务位于 `/root/echotrace/echotrace-edge-integrated`，原二进制仍在 `/root/echotrace/bin/`。上一版桌面备份是 `/root/echotrace/deskbot-app/bin/main.before-camera-fix`。桌面 PID 文件是 `/run/echotrace-deskbot.pid`，采集 PID 文件是 `/run/echotrace-capture-manual.pid`，控制 socket 是 `/run/echotrace-sd.sock`。当前没有启用开机自启；重启后仍需按实际服务配置恢复。

原独立终端 `S96echotrace-terminal` 与集成 DeskBot 都独占 `/dev/fb0`，只能启动其中一个。Buildroot 外部包现提供可选的 `BR2_PACKAGE_ECHOTRACE_DESKBOT` 模式：从上述隔离构建产物安装原桌面和记忆页，只安装 `S96echotrace-deskbot`；默认模式仍只安装独立终端。脚本在开发板上完成状态检查、重复启动与一次重启验证；完整镜像和开机启动仍未实测。制作正式固件时还须检查板型原有桌面启动项，避免两个显示进程同时自启。

重启开发板后，先确认采集服务已经启动，并停用旧的独立终端；然后在 Echo 的 shell 中启动集成桌面：

```sh
cd /root/echotrace/deskbot-app/bin
export ECHOTRACE_SOCKET=/run/echotrace-sd.sock
export LD_LIBRARY_PATH=$PWD/lib:/root/echotrace/lib:/oem/usr/lib:/usr/lib:/lib
start-stop-daemon -S -b -m -p /run/echotrace-deskbot.pid \
  -x /root/echotrace/deskbot-app/bin/main
```

先检查 PID 文件和 `/proc/<pid>/exe`，避免重复启动。当前采集程序是 `/root/echotrace/echotrace-edge-integrated`，仍使用原来的 SD 数据、配置、socket 和 Hub 地址；完整启动参数可由 `ps -o pid,args` 核对。

使用时从 Echo-Mate 首页打开底部抽屉，向下滚到“记忆”。选择项目后开始、标记、暂停或结束。回到首页后录制仍持续，状态可重新进入“记忆”查看。当前 dev2 已支持小屏项目管理、历史、语音与文字查询、声画回放、笔记整理、对照和 SD 导出；Web/Qt 可选。当前链路经 WSL 与 Mac 转发，断开后小屏会显示离线，但 SD 原件记录继续；双板 USB 直连尚未验证。

## 恢复原显示

仅在录制已结束且需回退桌面时，在 Echo 上执行：

```sh
start-stop-daemon -K -p /run/echotrace-deskbot.pid \
  -x /root/echotrace/deskbot-app/bin/main -s TERM
rm -f /run/echotrace-deskbot.pid
ECHOTRACE_PREFIX=/root/echotrace \
ECHOTRACE_RUN_DIR=/run/echotrace-terminal \
ECHOTRACE_SOCKET=/run/echotrace-sd.sock \
  /root/echotrace/S96echotrace-terminal start
```

此操作只恢复旧的独立显示进程，不删除集成程序、原始录像或 SD 索引。
