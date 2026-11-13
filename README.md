# EchoTrace：从下载源码到上板运行

EchoTrace 是 Echo-Mate 桌面里的“记忆”App。RV1106 保存操作视频和声音，鲁班猫 3 负责离线识别、检索和证据回放。

**当前是开发版。** 小屏已有选择项目、开始、标记、暂停、继续和结束；新建项目、完整历史、文字检索、视频回放和导出目前在浏览器完成。完整小屏流程见 [开发任务](docs/roadmap.md)。原聊天等应用沿用原配置。

第一次按顺序读；以后直接跳到需要的章节：

1. [认清终端、目录和前提](#1-先认清环境)
2. [不接开发板，先跑演示](#2-先在电脑跑演示)
3. [编译 RV1106 采集服务和桌面 App](#3-在-wsl-编译-rv1106-程序)
4. [准备鲁班猫后台及模型](#4-在鲁班猫准备后台)
5. [打包、传输和安装](#5-打包并安装到-echo-mate)
6. [连通后台并使用](#6-建立连接并开始使用)
7. [日常启动、更新和回退](#7-日常启动更新和回退)
8. [遇到错误查这里](#8-常见问题)

代码框中只有命令，不需要输入终端提示符。每步看到预期结果再继续；报错先看第 8 节。

## 1. 先认清环境

### 1.1 四台“机器”，命令不能混着运行

| 本文标记 | 你操作的终端 | 负责什么 | 编译器 |
|---|---|---|---|
| **[Mac]** | Mac 的“终端” | 编辑源码、传文件、维持 SSH 转发 | 实板教程不在这里编译 ARM 程序 |
| **[WSL]** | 从 Mac 输入 ssh wsl 后的 Ubuntu 22.04 x86_64，Python 3.10 | 编译 RV1106、转换模型 | SDK 的 ARM GCC 8.3.0 |
| **[RK]** | 从 Mac 输入 ssh lubancat 后的鲁班猫终端 | 运行后台、编译 NPU 工作进程 | 板载 aarch64 GCC/G++ |
| **[Echo]** | 从 Mac 输入 ssh -J wsl root@172.32.0.93 后的终端 | 运行采集服务和桌面 | 不在这里编译 |

看提示符判断位置：**[WSL] 通常是 guan@GSY；[RK] 是鲁班猫；[Echo] 是 Echo-Mate。** 不确定时运行：

~~~sh
hostname
uname -m
pwd
~~~

WSL 应为 x86_64，RK 为 aarch64，Echo 为 armv7l。进入 SSH 后，输入 exit 回到 Mac。下面每个命令块前都写明位置。

**Windows 用户：** 进入 WSL 使用 Windows PowerShell 的 wsl.exe；进入 Ubuntu 后执行标为 [WSL] 的命令。不要在 Ubuntu 中运行 wsl -u 或 netsh，那是 Windows 命令。

### 1.2 先准备这些东西

| 必需项 | 本教程采用的位置或条件 |
|---|---|
| Echo-Mate | 已能启动原桌面的 RV1106 板；摄像头、麦克风、扬声器、触摸屏正常 |
| 鲁班猫 3 | RK3576、4GB，已安装匹配 BSP 的 Linux，SSH 可用 |
| SD 卡 | 已在 Echo 挂载到 /mnt/sdcard；本项目实机为 64GB FAT32 |
| Echo 原工程 | WSL 的 /home/guan/Echo-Mate，包含 Demo 和已准备的 SDK |
| SDK 编译产物 | Buildroot sysroot 已有 ALSA、SDL2、DRM 等头文件和库 |
| 安防原工程 | Mac 当前工作区中的“代码”目录，里面有 RV1106 和 RK3576 |
| 网络 | 下载依赖时需要联网；部署完成后的模型推理不使用云服务 |

**Git 仓库不包含 SDK、原工程、模型、录像和数据库。** 只有 EchoTrace 源码时，可以完成第 2 节电脑演示；实板编译还需要上表中的本地资产。原工程版本不同会触发基线校验，请先对照文件，不要跳过校验强行打补丁。

本教程从**原固件已正常启动、SDK 已准备好**开始，编译并部署应用，不刷整机固件。当前 SDK 默认 SD_CARD 配置与实板 SPI NAND 启动存在差异；完整 BSP 构建另见 [BSP 基线](docs/bsp-validation.md)。

### 1.3 确认三条 SSH 命令都能登录

**[Mac｜任意目录]** 逐条执行；每次确认登录成功后输入 exit，再执行下一条。

~~~sh
ssh wsl
ssh lubancat
ssh -J wsl root@172.32.0.93
~~~

密码只在终端提示时输入，输入时不显示字符是正常的。不要把密码写进 Git。

<details>
<summary>第一次使用，还没有 wsl、lubancat 这两个 SSH 名称？</summary>

**[Mac｜任意目录]** 打开自己的 SSH 配置：

~~~sh
mkdir -p ~/.ssh
nano ~/.ssh/config
~~~

添加下面两项，把三个大写值换成实际信息。WSL 的入口是电脑能够访问的 SSH 地址和端口，不是 Echo 的 USB 地址。

~~~sshconfig
Host wsl
    HostName WSL_SSH_ADDRESS
    Port WSL_SSH_PORT
    User guan

Host lubancat
    HostName RK_SSH_ADDRESS
    User root
~~~

按 Ctrl+O、回车保存，Ctrl+X 退出，再尝试登录。地址由对应机器的实际网络决定；本文不替你猜地址。WSL 的 SSH 服务、Windows 端口映射需要先可用，避免在尚未连通时进行后面的部署。

</details>

### 1.4 固定目录

后面的命令使用以下目录。WSL 用户名不是 guan 时，将涉及 /home/guan 的路径统一改成自己的路径。

| 位置 | 目录 | 内容 |
|---|---|---|
| Mac | 本 README 所在的 EchoTrace 目录 | 主源码 |
| WSL | /home/guan/EchoTrace-git | 本教程的源码及编译输出 |
| WSL | /home/guan/Echo-Mate | 原 Echo-Mate 和 SDK，保留原样 |
| RK | /root/EchoTrace-git | 本教程的后台源码、模型与数据 |
| Echo | /opt/echotrace | 本教程安装的应用 |
| Echo | /etc/echotrace | 本教程的配置与私有令牌 |
| Echo | /mnt/sdcard/echotrace-guide | 本教程的新录制数据 |

这里使用新数据目录，保留此前 /root/echotrace 和 /mnt/sdcard/echotrace 的开发安装与录像。**旧采集进程和旧桌面必须先停止，再启动新安装。**

## 2. 先在电脑跑演示

这一节不需要 SDK、模型或开发板。它让你先看到项目、时间线、搜索、视频和导出。

### 2.1 安装电脑端工具，下载源码

**[WSL｜任意目录]**

~~~sh
sudo apt-get update
sudo apt-get install -y git python3-venv python3-pip build-essential cmake ffmpeg
cd ~
git clone https://github.com/532313/EchoTrace.git EchoTrace-git
cd ~/EchoTrace-git
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -e .
.venv/bin/python -m echotrace --version
~~~

已有 ~/EchoTrace-git 时，跳过 git clone，直接 cd。最后应输出版本号。这里安装的是 Python 后台；尚未编译 RV1106 程序。

### 2.2 生成演示数据，启动网页服务

**[WSL｜~/EchoTrace-git]** 保留这个终端，让服务持续运行：

~~~sh
cd ~/EchoTrace-git
.venv/bin/python -m echotrace demo --data runtime/guide-demo
.venv/bin/python -m echotrace serve --data runtime/guide-demo --models runtime/no-models.json --port 18082
~~~

看到 Uvicorn running on http://127.0.0.1:18082 即启动成功。这个命令不会返回提示符；**这是服务正在运行，不是卡住。** 停止时按 Ctrl+C。

演示明确标为“模拟素材”：画面是测试图，笔记是预先编写的，声音是提示音，不是拍摄或识别结果。

**[Mac｜另开一个终端，任意目录]** 保留此终端：

~~~sh
ssh -N -o ExitOnForwardFailure=yes -L 18082:127.0.0.1:18082 wsl
~~~

**[Mac｜浏览器]** 打开 <http://127.0.0.1:18082>。如果你直接在 Windows 使用 WSL，通常可从 Windows 浏览器打开同一地址，无须 Mac 这条转发。

依次操作：选择示例项目 → 输入“为什么更换排线” → 点结果 → 播放 → 编辑笔记或导出。能看到文字、测试视频和证据说明，即完成演示。

### 2.3 可选：编译电脑版模拟采集端

**[WSL｜另开终端，~/EchoTrace-git]** 这里用 Ubuntu 自带 G++，编译结果只能在电脑运行：

~~~sh
cd ~/EchoTrace-git
.venv/bin/python scripts/fetch_build_dependencies.py
cmake -S . -B build/host -DCMAKE_BUILD_TYPE=Debug -DECHO_VENDOR_DIR="$PWD/build/portable-vendor"
cmake --build build/host --target echotrace-edge --parallel 4
build/host/echotrace-edge --version
build/host/echotrace-edge --simulated --data runtime/guide-simulator --socket /tmp/echotrace-guide.sock --hub ws://127.0.0.1:18082/ws/device
~~~

网页会出现在线模拟设备，此时可新建项目并点“开始记忆”。模拟端不调用相机。结束会话后，在采集端和后台终端分别按 Ctrl+C。后续实板使用 18081，和本节 18082 分开。

## 3. 在 WSL 编译 RV1106 程序

最终要编译两个程序：**echotrace-edge 是后台采集服务；main 是加入“记忆”App 的原桌面。**

直接做实板也先完成 2.1，准备源码、Python 环境和构建工具；2.2、2.3 的演示可跳过。

### 3.1 检查 SDK 和原工程

**[WSL｜~/EchoTrace-git]**

~~~sh
cd ~/EchoTrace-git
sudo apt-get install -y rsync patch pkg-config curl xz-utils
export ECHO_SDK="$HOME/Echo-Mate/SDK/rv1106-sdk"
export LUCKFOX_SDK_PATH="$ECHO_SDK"
export ET_SYSROOT="$ECHO_SDK/sysdrv/source/buildroot/buildroot-2023.02.6/output/host/arm-buildroot-linux-uclibcgnueabihf/sysroot"
export ET_CROSS="$ECHO_SDK/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin/arm-rockchip830-linux-uclibcgnueabihf-"
"$ET_CROSS"gcc --version
ls "$ET_SYSROOT/usr/include/alsa/asoundlib.h"
ls "$HOME/Echo-Mate/Demo/rkmpi_demos/include/rk_mpi_sys.h"
ls "$HOME/Echo-Mate/Demo/DeskBot_demo/toolchain.cmake"
~~~

应看到 GCC 8.3.0 和三个文件路径。缺文件就先停在这里：安装 Ubuntu 的 libasound2-dev 不能补齐 ARM sysroot。需要用匹配原固件的 SDK 构建产物，路径和版本以 [工具链文件](cmake/rv1106-toolchain.cmake) 为准。

上面的 export 只在当前终端有效；新开 WSL 终端时重新执行这四行。

### 3.2 从安防原工程准备复用模块

**[Mac｜当前主源码目录]** 当前工作区可直接复制以下命令：

~~~sh
cd "/Users/gsy/Desktop/嵌入式Linux智能安防监控系统/EchoTrace"
python3 scripts/prepare_sources.py --security "../代码" --echo "/Users/gsy/Documents/echo-mate/Echo-Mate"
tar -czf build/vendor-guide.tar.gz -C build vendor
scp build/vendor-guide.tar.gz wsl:EchoTrace-git/build/
~~~

脚本会将录像封装和播放器复制、适配到 build/vendor，原项目保持原样。另一台电脑上，修改 --security 和 --echo 后面的路径：

- --security 指向同时含 RV1106、RK3576 的“代码”目录。
- --echo 指向同时含 Demo、SDK、LICENSE 的 Echo-Mate 根目录。
- 输出 reused_files 和 output 表示成功；FileNotFoundError 表示原工程目录不对。

**[WSL｜~/EchoTrace-git]**

~~~sh
cd ~/EchoTrace-git
tar -xzf build/vendor-guide.tar.gz -C build
.venv/bin/python scripts/fetch_build_dependencies.py
ls build/vendor/recording/recording.cc
ls build/vendor/decoder/RecordingPlayer.cpp
~~~

最后两个文件必须存在。已有旧缓存也可复用，但本次 CMake 显式使用本仓库的 build/deps 和 build/vendor，避免编译到另一份源码。

### 3.3 准备中文字库

**[WSL｜~/EchoTrace-git]** 已有旧工程生成的字库时，复制一次即可：

~~~sh
cd ~/EchoTrace-git
mkdir -p build/vendor/fonts
cp /home/guan/EchoTrace/build/vendor/fonts/echotrace_font.c build/vendor/fonts/
~~~

没有该文件时，按下面的方法生成；两种方法选一种。

<details>
<summary>没有字库缓存：下载 Node.js 并生成字库</summary>

**[WSL｜~/EchoTrace-git，x86_64 Ubuntu]** Node.js 只用于生成字库，不安装到开发板。

~~~sh
cd ~/EchoTrace-git
mkdir -p build/tools
cd build/tools
curl -fLO https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz
curl -fLO https://nodejs.org/dist/v22.14.0/SHASUMS256.txt
sha256sum --check --ignore-missing SHASUMS256.txt
tar -xJf node-v22.14.0-linux-x64.tar.xz
cd ../..
export PATH="$PWD/build/tools/node-v22.14.0-linux-x64/bin:$PATH"
node --version
.venv/bin/python scripts/prepare_font.py
~~~

校验应显示 OK，Node 版本应为 v22.14.0，最后生成 build/vendor/fonts/echotrace_font.c。脚本固定字体 revision、SHA-256 和 lv_font_conv 版本。生成过程需要网络。

</details>

### 3.4 编译采集服务

**[WSL｜~/EchoTrace-git，沿用 3.1 的环境变量]**

~~~sh
cd ~/EchoTrace-git
cmake -S . -B build/rv1106 \
  -DCMAKE_TOOLCHAIN_FILE=cmake/rv1106-toolchain.cmake \
  -DECHO_RV1106=ON -DECHO_BUILD_LVGL=OFF \
  -DECHO_DEPS_DIR="$PWD/build/deps" \
  -DECHO_VENDOR_DIR="$PWD/build/vendor" \
  -DECHO_MEDIA_SOURCE_ROOT="$HOME/Echo-Mate/Demo/rkmpi_demos" \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build/rv1106 --target echotrace-edge --parallel 4
file build/rv1106/echotrace-edge
~~~

应出现 Built target echotrace-edge，file 应显示 ELF 32-bit、ARM。**不要在 WSL 执行这个 ARM 程序**，它稍后复制到 Echo 运行。

编译分工：CMake 生成构建规则；SDK 中的 arm-rockchip830-linux-uclibcgnueabihf-g++ 编译；Buildroot sysroot 提供 ARM 头文件和库；原项目提供 Rockchip 媒体接口。

### 3.5 编译带“记忆”的原桌面

**[WSL｜~/EchoTrace-git]** 这个脚本要求输出目录尚不存在：

~~~sh
cd ~/EchoTrace-git
export LUCKFOX_SDK_PATH="$HOME/Echo-Mate/SDK/rv1106-sdk"
sh integration/deskbot/build_integrated.sh "$PWD/build/deskbot-app"
file build/deskbot-app/DeskBot_demo/build-echotrace/bin/main
~~~

脚本依次校验原工程、建立隔离副本、应用三个补丁、编译。最后输出 main 的 SHA-256 和 isolated build 路径。原 Echo-Mate 源码不被修改。

| 编译输出 | 部署时的用途 |
|---|---|
| build/rv1106/echotrace-edge | 采集服务 |
| build/deskbot-app/DeskBot_demo/build-echotrace/bin/ | 整个目录都是桌面资源，不能只复制 main |
| build/deskbot-app/configure.log、build.log | 配置或编译失败时查看 |
| build/deskbot-app/yolo-failure-test.log | 原 AI 相机错误路径检查记录 |

再次编译同一副本时执行下面两行，**不要重跑首次建副本的脚本**：

~~~sh
# [WSL｜~/EchoTrace-git]
cd ~/EchoTrace-git
cmake --build build/deskbot-app/DeskBot_demo/build-echotrace --parallel 4
~~~

## 4. 在鲁班猫准备后台

### 4.1 安装依赖，取得相同源码

**[RK｜root 用户，/root]**

~~~sh
cd /root
apt-get update
apt-get install -y git python3-venv python3-pip build-essential cmake ffmpeg pkg-config qtbase5-dev libavformat-dev libavcodec-dev libavutil-dev libswscale-dev
git clone https://github.com/532313/EchoTrace.git EchoTrace-git
cd /root/EchoTrace-git
~~~

已有 /root/EchoTrace-git 时跳过 clone。

**[Mac｜任意目录]** 将 WSL 的提交号送到鲁班猫：

~~~sh
ssh wsl 'git -C /home/guan/EchoTrace-git rev-parse HEAD' > /private/tmp/echotrace-source.commit
scp /private/tmp/echotrace-source.commit lubancat:/tmp/echotrace-source.commit
~~~

**[RK｜/root/EchoTrace-git]** 工作区没有自己的未提交修改时执行：

~~~sh
cd /root/EchoTrace-git
git status --short
git fetch origin
git checkout --detach "$(cat /tmp/echotrace-source.commit)"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -e '.[ai]'
.venv/bin/python -m echotrace --version
~~~

git status 有输出时先保存自己的修改，再执行后续命令。两端使用同一提交，避免新版采集事件交给旧后台。Python 是后台主进程，ONNX Runtime 和 sherpa-onnx 在 RK 的 CPU 运行；这里不安装 PyTorch。

### 4.2 在 WSL 准备 CPU 模型

**[WSL｜~/EchoTrace-git]** 下载语音识别、VAD、中文检索原模型和 TTS：

~~~sh
cd ~/EchoTrace-git
.venv/bin/python scripts/fetch_models.py asr vad bge-source tts
python3 -m venv .convert-venv
.convert-venv/bin/python -m pip install --upgrade pip setuptools wheel
.convert-venv/bin/python -m pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu
.convert-venv/bin/python -m pip install transformers==5.14.1 numpy==2.2.6 onnx==1.19.1 onnxruntime==1.23.2
.convert-venv/bin/python scripts/export_bge.py
~~~

BGE 转换只在 x86_64 构建机做一次。应生成 models/assets/bge/model.int8.onnx 和 vocab.txt；下载脚本自动写 models/config.json，不需要手抄 JSON。

已经准备好旧工程模型时，也可以复制旧 models/assets 和 models/config.json 到新仓库，跳过下载与转换。配置中若有 vlm，必须继续完成下一节的工作进程编译，不能只复制配置。

Matcha 中文语音模型的 Baker 数据许可为非商业用途，当前用于个人学习。

### 4.3 可选：准备 NPU 画面描述

不做这一节，仍可录制、保存口述和按文字检索；自动画面描述需要下面两个模型和工作进程。

<details>
<summary>展开完整的 Qwen 转换与 RK3576 编译步骤</summary>

**[WSL｜~/EchoTrace-git，Python 3.10]** 语言和视觉转换环境必须分开；不能把所有依赖装进同一个虚拟环境。

~~~sh
cd ~/EchoTrace-git
python3 --version
.venv/bin/python scripts/fetch_qwen.py
mkdir -p build/wheels-convert
curl -fL -o build/wheels-convert/rkllm_toolkit-1.3.1-cp310-cp310-linux_x86_64.whl \
  https://raw.githubusercontent.com/airockchip/rknn-llm/f7390530443bf84f0394255a449d7cbe81e69d1c/rkllm-toolkit/packages/rkllm_toolkit-1.3.1-cp310-cp310-linux_x86_64.whl
python3 -m venv .rk-convert
.rk-convert/bin/python -m pip install --upgrade pip setuptools wheel
.rk-convert/bin/python -m pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cpu
.rk-convert/bin/python -m pip install --no-deps build/wheels-convert/rkllm_toolkit-1.3.1-cp310-cp310-linux_x86_64.whl
~~~

上面的工具包只匹配 CPython 3.10、Linux x86_64。继续安装固定依赖，再转换语言与视觉 ONNX：

~~~sh
# [WSL｜~/EchoTrace-git]
python3 - <<'PY'
from pathlib import Path
lines = Path("config/conversion/rkllm.txt").read_text().splitlines()
Path("build/rkllm-requirements.txt").write_text(
    "\n".join(x for x in lines if not x.startswith("rkllm-toolkit")) + "\n"
)
PY
BUILD_CUDA_EXT=0 .rk-convert/bin/python -m pip install --no-build-isolation -r build/rkllm-requirements.txt
.venv/bin/python scripts/convert_qwen.py language --python .rk-convert/bin/python
.venv/bin/python scripts/convert_qwen.py vision-onnx --python .rk-convert/bin/python
python3 -m venv .rknn-convert
.rknn-convert/bin/python -m pip install --upgrade pip setuptools wheel
.rknn-convert/bin/python -m pip install torch==2.4.0 torchvision==0.19.0 --index-url https://download.pytorch.org/whl/cpu
.rknn-convert/bin/python -m pip install -r config/conversion/rknn.txt
.venv/bin/python scripts/convert_qwen.py vision --python .rknn-convert/bin/python
~~~

应得到 models/assets/qwen/ 下的 .rkllm 和 .rknn 两个文件。转换很慢，看对应终端日志，不在 RV1106 上执行。

**[Mac｜主源码目录]** 将第 3.2 节已经准备的复用模块传给 RK：

~~~sh
cd "/Users/gsy/Desktop/嵌入式Linux智能安防监控系统/EchoTrace"
scp build/vendor-guide.tar.gz lubancat:/tmp/vendor-guide.tar.gz
~~~

**[RK｜/root/EchoTrace-git]** 这里使用鲁班猫本机 G++，编译 aarch64 工作进程：

~~~sh
cd /root/EchoTrace-git
mkdir -p build
tar -xzf /tmp/vendor-guide.tar.gz -C build
.venv/bin/python scripts/fetch_build_dependencies.py
.venv/bin/python scripts/fetch_rkllm.py
cmake -S . -B build/rk3576 -DECHO_BUILD_EDGE=OFF -DECHO_BUILD_VLM=ON -DECHO_BUILD_QT=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build/rk3576 --target echotrace-vlm echotrace-workbench --parallel 4
file build/rk3576/echotrace-vlm
~~~

应显示 ELF 64-bit、ARM aarch64。SDK 运行库在 build/rkllm/lib，保留这个目录；不要把它们复制覆盖系统库。

**[WSL｜~/EchoTrace-git]** 为生成的配置添加视觉模型；这段命令只更新 vlm 项：

~~~sh
cd ~/EchoTrace-git
.venv/bin/python - <<'PY'
import json
from pathlib import Path
path = Path("models/config.json")
config = json.loads(path.read_text())
config["vlm"] = {
    "worker": "../build/rk3576/echotrace-vlm",
    "vision": "assets/qwen/qwen3.5-0.8b-448-rk3576.rknn",
    "language": "assets/qwen/qwen3.5-0.8b-w4a16-rk3576.rkllm",
    "revision": "Qwen-2fc06364715b967f1860aea9cf38778875588b17-RKLLM-f7390530443bf84f0394255a449d7cbe81e69d1c"
}
path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
PY
~~~

下面第 4.4 节把模型传到 RK 后，再由 RK 校验工作进程和模型摘要。NPU 驱动与 Runtime 必须匹配，历史实机配置见 [模型记录](docs/models.md)。

</details>

### 4.4 将运行模型传到 RK，核对配置

**[WSL｜~/EchoTrace-git]** 只打包运行资产，不打包 PyTorch 环境、源权重和转换临时文件：

~~~sh
cd ~/EchoTrace-git
tar -czf build/models-guide.tar.gz \
  models/config.json models/assets/sensevoice models/assets/vad \
  models/assets/bge models/assets/tts
~~~

做了 4.3 的 VLM 时，改用下面这一条打包，包含 qwen：

~~~sh
# [WSL｜~/EchoTrace-git；仅完成 4.3 时执行，替代上一条 tar]
tar -czf build/models-guide.tar.gz \
  models/config.json models/assets/sensevoice models/assets/vad \
  models/assets/bge models/assets/tts models/assets/qwen
~~~

**[Mac｜任意目录]**

~~~sh
scp wsl:EchoTrace-git/build/models-guide.tar.gz /private/tmp/models-guide.tar.gz
scp /private/tmp/models-guide.tar.gz lubancat:/tmp/models-guide.tar.gz
~~~

**[RK｜/root/EchoTrace-git]**

~~~sh
cd /root/EchoTrace-git
tar -xzf /tmp/models-guide.tar.gz
.venv/bin/python scripts/seal_models.py
.venv/bin/python scripts/seal_models.py --check
.venv/bin/python -m echotrace doctor --data runtime/workbench --models models/config.json
~~~

seal 记录本次实际模型与工作进程的摘要，--check 检查其后是否变化。应输出 verified_files；doctor 应能找到 ffmpeg，模型状态为 files_present。files_present 只表示文件齐全，模型在后台成功执行后 verified_runtime 才会变为 true。

## 5. 打包并安装到 Echo-Mate

### 5.1 打包 ARM 应用

**[WSL｜~/EchoTrace-git]** 沿用 3.1 的四个环境变量；新开终端时先重新 export。每次从干净源码提交重新构建后打包。

~~~sh
cd ~/EchoTrace-git
mkdir -p build/rv1106-install/echotrace/bin build/rv1106-install/echotrace/lib build/rv1106-install/echotrace/deskbot/bin
cp build/rv1106/echotrace-edge build/rv1106-install/echotrace/bin/
cp -a build/deskbot-app/DeskBot_demo/build-echotrace/bin/. build/rv1106-install/echotrace/deskbot/bin/
cp -L "$HOME/Echo-Mate/Demo/rkmpi_demos/lib/librockit.so" build/rv1106-install/echotrace/lib/
cp -L "$HOME/Echo-Mate/Demo/rkmpi_demos/lib/librockchip_mpp.so.1" build/rv1106-install/echotrace/lib/
cp -L "$HOME/Echo-Mate/Demo/rkmpi_demos/lib/librkaiq.so" build/rv1106-install/echotrace/lib/
cp -L "$HOME/Echo-Mate/Demo/rkmpi_demos/lib/librga.so" build/rv1106-install/echotrace/lib/
for so in libasound.so.2 libstdc++.so.6 libgcc_s.so.1 \
    libSDL2-2.0.so.0 libdrm.so.2 libjson-c.so.5 libcurl.so.4 \
    libopus.so.0 libportaudio.so.2 libjsoncpp.so.25 libopenblas.so.0 \
    libssl.so.1.1 libcrypto.so.1.1 libz.so.1 libatomic.so.1; do
    source_lib="$ET_SYSROOT/usr/lib/$so"
    if [ ! -f "$source_lib" ]; then source_lib="$ET_SYSROOT/lib/$so"; fi
    cp -L "$source_lib" build/rv1106-install/echotrace/lib/
done
cp bsp/rv1106/S95echotrace bsp/rv1106/S96echotrace-deskbot build/rv1106-install/echotrace/
cp config/edge.example.json build/rv1106-install/echotrace/edge.json
git rev-parse HEAD > build/rv1106-install/echotrace/SOURCE_COMMIT
.venv/bin/python -m echotrace --version > build/rv1106-install/echotrace/VERSION
~~~

这些库从同一 ARM sysroot 复制，覆盖采集服务和原桌面的依赖。任何 cp 报错都先修正路径，不继续打包；不复制或替换系统的 libc 和加载器。

将这次安装的启动脚本改为现有 SSH 链路和新的数据目录。只改暂存文件，不改仓库脚本：

~~~sh
# [WSL｜~/EchoTrace-git]
python3 - <<'PY'
from pathlib import Path
p = Path("build/rv1106-install/echotrace/S95echotrace")
text = p.read_text()
text = text.replace("ws://192.168.77.1:18081/ws/device", "ws://127.0.0.1:18081/ws/device")
text = text.replace("/mnt/sdcard/echotrace", "/mnt/sdcard/echotrace-guide")
p.write_text(text)
PY
"$ET_CROSS"strip --strip-unneeded build/rv1106-install/echotrace/bin/echotrace-edge build/rv1106-install/echotrace/deskbot/bin/main build/rv1106-install/echotrace/lib/*.so*
.venv/bin/python scripts/release.py artifact --target rv1106 --component app \
  --file build/rv1106-install/echotrace/bin/echotrace-edge \
  --file build/rv1106-install/echotrace/deskbot/bin/main \
  --toolchain 'GCC 8.3.0 / uClibc / Echo RV1106 SDK' \
  --output build/rv1106-install/echotrace/artifacts.manifest.json
tar -hczf build/echotrace-rv1106.tar.gz -C build/rv1106-install echotrace
sha256sum build/echotrace-rv1106.tar.gz
du -sh build/rv1106-install/echotrace
~~~

strip 去掉调试符号以缩小体积；artifacts.manifest.json 记录版本、提交和**实际安装的两个程序**的 SHA-256。再次打包前，将 build/rv1106-install 和旧 tar.gz 改名归档，然后从 5.1 重新创建暂存目录。不要把带 dirty 标记的构建作为正式安装版本。

### 5.2 先结束旧录制，停止旧程序

**[Echo｜root 用户]** 先从屏幕或旧工作台点击“结束记忆”。然后读取状态，必须为 stopped：

~~~sh
cat /mnt/sdcard/echotrace/state.json
~~~

首次安装且没有该文件时跳过。已有本项目开发安装时，停止文档记录的两个旧进程：

~~~sh
# [Echo｜任意目录；仅旧开发安装存在时执行]
start-stop-daemon -K -p /run/echotrace-deskbot.pid -x /root/echotrace/deskbot-app/bin/main -s TERM
start-stop-daemon -K -p /run/echotrace-capture-manual.pid -x /root/echotrace/echotrace-edge-integrated -s TERM
~~~

如果运行的是旧独立终端或其他原桌面，先用其启动脚本正常停止。**同一时间只能有一个桌面占用 /dev/fb0，一个采集进程占用相机和声卡。** 不用 killall main，以免停止其他程序。

### 5.3 传安装包

**[Echo｜任意目录]** 确认 SD 已挂载，创建配置目录：

~~~sh
grep ' /mnt/sdcard ' /proc/mounts
df -h /mnt/sdcard /
mkdir -p /etc/echotrace
~~~

第一行必须有挂载记录。根分区可用空间须大于 5.1 输出的安装目录大小；安装包先放 SD，不放 Echo 的小容量 /tmp。

**[Mac｜任意目录]**

~~~sh
scp wsl:EchoTrace-git/build/echotrace-rv1106.tar.gz /private/tmp/echotrace-rv1106.tar.gz
shasum -a 256 /private/tmp/echotrace-rv1106.tar.gz
scp -O -J wsl /private/tmp/echotrace-rv1106.tar.gz root@172.32.0.93:/mnt/sdcard/
~~~

Mac 的摘要应与 5.1 相同。-O 使用传统 SCP，兼容 Echo 上没有 SFTP 子系统的精简系统。

### 5.4 安装并保存上一版

**[Echo｜任意目录]** 如果已经运行 /opt/echotrace 版本，先执行第 7.3 节停止命令。

~~~sh
sha256sum /mnt/sdcard/echotrace-rv1106.tar.gz
mkdir -p /opt
ET_PREVIOUS=
if [ -d /opt/echotrace ]; then
    ET_PREVIOUS="/opt/echotrace.before-$(date +%Y%m%d-%H%M%S)"
    mv /opt/echotrace "$ET_PREVIOUS"
fi
tar -xzf /mnt/sdcard/echotrace-rv1106.tar.gz -C /opt
if [ -n "${ET_PREVIOUS:-}" ] && [ -f "$ET_PREVIOUS/deskbot/bin/system_para.conf" ]; then
    cp -p "$ET_PREVIOUS/deskbot/bin/system_para.conf" /opt/echotrace/deskbot/bin/system_para.conf
elif [ -f /root/echotrace/deskbot-app/bin/system_para.conf ]; then
    cp -p /root/echotrace/deskbot-app/bin/system_para.conf /opt/echotrace/deskbot/bin/system_para.conf
fi
chmod +x /opt/echotrace/bin/echotrace-edge /opt/echotrace/deskbot/bin/main /opt/echotrace/S95echotrace /opt/echotrace/S96echotrace-deskbot
if [ -f /etc/echotrace/edge.json ]; then
    cp -p /etc/echotrace/edge.json /etc/echotrace/edge.json.before-guide
fi
cp /opt/echotrace/edge.json /etc/echotrace/edge.json
if [ -f /etc/echotrace/token ]; then
    cp -p /etc/echotrace/token /etc/echotrace/token.before-guide
fi
export LD_LIBRARY_PATH=/opt/echotrace/lib:/oem/usr/lib:/usr/lib:/lib
/opt/echotrace/bin/echotrace-edge --version
cat /opt/echotrace/SOURCE_COMMIT
~~~

三个设备上的 SHA-256 应一致。最后应显示版本与源码提交。上面的复制会保留旧桌面设置；如果原桌面装在其他位置，改成其实际 system_para.conf 路径。出现缺库错误时先查第 8 节，不继续启动桌面。

### 5.5 确认配置

**[Echo｜/etc/echotrace/edge.json]** 本教程使用默认配置，无须手改：

| 字段 | 默认值 | 含义 |
|---|---|---|
| device_id | echo-mate | 设备名称 |
| alsa | hw:0,0 | 声卡及设备编号 |
| capture_channels | 2 | 硬件按双声道采集 |
| microphone_channel | 0 | 取左声道保存为单声道 |
| capture_latency_us | 500000 | 采集缓冲延迟，单位微秒 |
| iq_directory | /etc/iqfiles | 原固件摄像头 ISP 参数目录 |
| reserve_mb | 256 | 剩余空间低于预留值时停止新增记录 |
| token_file | /etc/echotrace/token | 与 RK 相同的私有令牌文件 |

声卡不同时，先运行 arecord -l，再修改 alsa；不要凭猜测换通道。摄像头驱动、ISP、ALSA、屏幕和触摸沿用原 BSP。当前产品不需要脚踏、外接暂停开关或称重模块。

## 6. 建立连接并开始使用

本节保持当前接线：**Echo → USB → WSL；Mac 分别 SSH 连接 WSL 和 RK。** 不是已经完成的双板 USB 直连。

### 6.1 启动 RK 后台

**[RK｜/root/EchoTrace-git]** 先检查端口：

~~~sh
ss -lntp 'sport = :18081'
~~~

如果已有旧 Hub，先正常结束其会话，找到输出中的 PID，用 kill -TERM 实际PID 停止，再确认端口空闲。同一数据目录不可启动两个 Hub。本教程的新数据库在 runtime/workbench，不切换或删除旧数据库。

**[RK｜/root/EchoTrace-git]** 创建私有令牌并启动：

~~~sh
cd /root/EchoTrace-git
umask 077
mkdir -p runtime
if [ ! -s runtime/hub.token ]; then
    .venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))' > runtime/hub.token
fi
nohup env ECHOTRACE_TOKEN="$(cat runtime/hub.token)" .venv/bin/python -m echotrace serve \
  --data runtime/workbench --models models/config.json --host 127.0.0.1 --port 18081 \
  > runtime/hub.log 2>&1 &
echo "$!" > runtime/hub.pid
sleep 2
kill -0 "$(cat runtime/hub.pid)"
curl --fail -H "Authorization: Bearer $(cat runtime/hub.token)" http://127.0.0.1:18081/api/status
~~~

kill 没有输出且 curl 返回 JSON，即后台已启动。若退出，运行 tail -n 40 runtime/hub.log。这里的 kill -0 只检查进程，不停止它。

### 6.2 将同一份令牌放到 Echo

**[Mac｜任意目录]**

~~~sh
umask 077
scp lubancat:EchoTrace-git/runtime/hub.token /private/tmp/echotrace-guide.token
scp -O -J wsl /private/tmp/echotrace-guide.token root@172.32.0.93:/etc/echotrace/token
~~~

**[Echo｜任意目录]**

~~~sh
chmod 600 /etc/echotrace/token
~~~

两端必须使用同一个令牌；不把令牌写在 README、截图或提交记录里。浏览器稍后也要填这一份令牌。

### 6.3 打开两条转发，保持两个 Mac 终端不关闭

**[Mac｜终端 A，任意目录]** RK 后台 → Mac 18084：

~~~sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L 18084:127.0.0.1:18081 lubancat
~~~

**[Mac｜终端 B，任意目录]** Echo 本地 18081 → Mac 18084：

~~~sh
ssh -N -J wsl -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -R 127.0.0.1:18081:127.0.0.1:18084 root@172.32.0.93
~~~

命令持续运行、没有输出是正常的。链路为：

~~~text
Echo 的 127.0.0.1:18081 → SSH → Mac 的 127.0.0.1:18084 → SSH → RK 的 127.0.0.1:18081
~~~

三个 localhost 分属不同机器。关闭任一转发终端，Echo 就会离线；已开始的录制仍保存到 SD。

### 6.4 启动 Echo 采集服务和桌面

**[Echo｜任意目录]**

~~~sh
sh /opt/echotrace/S95echotrace start
sh /opt/echotrace/S96echotrace-deskbot start
sh /opt/echotrace/S96echotrace-deskbot status
cat /mnt/sdcard/echotrace-guide/state.json
~~~

应看到原桌面；打开底部抽屉，选择“记忆”。状态应为“已停止 · 在线”，state.json 中 connected 为 true。

| 程序 | PID 文件 | 本地控制口 |
|---|---|---|
| 采集服务 | /run/echotrace/capture.pid | /run/echotrace/control.sock |
| 集成桌面 | /run/echotrace/deskbot.pid | 使用同一个控制口 |

这次只手动启动，没有把脚本复制到 /etc/init.d。整机开机自启尚未作为完成品交付，重启后按第 7.1 节启动。

### 6.5 真正录一次，查一次

**[Mac｜浏览器]** 打开 <http://127.0.0.1:18084>。左下角点齿轮，填写 6.2 的同一令牌；网页提示未授权时也从这里填写。

**[Mac｜任意目录]** 用下面的命令把令牌放到剪贴板，再到网页输入框按 Command+V 粘贴：

~~~sh
pbcopy < /private/tmp/echotrace-guide.token
~~~

1. 点“＋ 新建项目”，输入“我的接线记录”。
2. **[Echo 屏幕]** 进入“记忆”，选择该项目，点“开始记忆”。
3. 操作时点“记下这一步”，口述“更换排线是因为接触不良”。
4. 可点“暂停 / 继续”；返回原桌面仍继续记录。录制时进入原聊天或 AI 相机会受设备锁限制。
5. 点“结束记忆”。**[浏览器]** 选择这次会话，等待语音转写和关键帧任务完成。
6. 输入“为什么更换排线”，点“查找记忆”；点结果进入证据。
7. 原片未缓存时点“获取原始片段”，就绪后播放；可编辑笔记、选两步对照或导出。

录制中也可说“查一下，为什么更换排线”。它经过短段闭合、识别和检索后返回，**不是即时语音助手**。未录制时的主动语音提问、小屏完整历史与视频回放仍待开发。

视频是 MP4，声音是单独的 WAV；直接打开 MP4 没有声音是正常的，工作台会按时间联合播放。导出 ZIP 是选定步骤的证据包，不是整个数据库备份。

### 6.6 可选：在鲁班猫显示器打开 Qt 工作台

完成 4.3 的 Qt 编译，并连接显示器、进入鲁班猫图形桌面后，在**[RK 图形桌面的终端｜/root/EchoTrace-git]**执行：

~~~sh
cd /root/EchoTrace-git
ECHOTRACE_TOKEN="$(cat runtime/hub.token)" build/rk3576/echotrace-workbench --hub http://127.0.0.1:18081
~~~

它连接已有 Hub，不额外启动后台。纯 SSH 终端没有图形会话时使用浏览器，不手填 DISPLAY 值。

## 7. 日常启动、更新和回退

### 7.1 下次开机怎么用

按顺序做：

1. **[RK]** 运行 6.1；已有后台时不重复启动。
2. **[Mac]** 打开 6.3 的两个 SSH 转发终端。
3. **[Echo]** 运行 6.4 的两个 start 命令。
4. **[Mac 浏览器]** 打开 18084，填写令牌；**[Echo 屏幕]** 打开“记忆”。

原件在 Echo 的 /mnt/sdcard/echotrace-guide；索引、缓存和导出在 RK 的 /root/EchoTrace-git/runtime/workbench。停止程序或升级时保留这两个目录。

### 7.2 改代码后怎么编译、更新

**[WSL｜~/EchoTrace-git]** 更新到已合并提交后，重复构建已有目录：

~~~sh
cd ~/EchoTrace-git
git switch main
git pull --ff-only
cmake --build build/rv1106 --target echotrace-edge --parallel 4
cmake --build build/deskbot-app/DeskBot_demo/build-echotrace --parallel 4
~~~

只改 native/lvgl/deskbot 中的页面代码，可增量编译；修改了 integration/deskbot/*.patch 时，必须换一个新输出目录重建隔离桌面，并在打包命令中使用新目录。

然后：第 5.1 节重新打包 → 保存旧包与清单 → **先更新 RK 中枢，再更新 Echo** → 按 6.1～6.4 启动。RK checkout 与 WSL 相同的提交，重新安装 Python 包；工作进程变化时重新编译并运行 seal_models.py。

RK 的 seal 会更新 models/models.lock.json；若它阻止更新源码，先将该文件与 models/config.json 复制到 runtime 中归档，查看 git diff 确认是本机生成的摘要，再恢复仓库基线。不要删除模型和录制数据来解决 Git 冲突。

### 7.3 正常停止

先点“结束记忆”，等状态为“已停止”。

**[Echo｜任意目录]**

~~~sh
sh /opt/echotrace/S96echotrace-deskbot stop
sh /opt/echotrace/S95echotrace stop
sync
~~~

**[RK｜/root/EchoTrace-git]** 确认 PID 对应本教程的后台后停止：

~~~sh
cd /root/EchoTrace-git
ps -p "$(cat runtime/hub.pid)" -o pid,args
kill -TERM "$(cat runtime/hub.pid)"
~~~

最后在 **[Mac]** 的两个转发终端按 Ctrl+C。关机或取 SD 前先完成以上停止步骤。

### 7.4 新版本出问题怎么回退

**[Echo｜任意目录]** 先按 7.3 停止，再找到 5.4 留下的备份：

~~~sh
ls -d /opt/echotrace.before-*
~~~

把下面的 BACKUP_DIRECTORY 换成上面输出的完整目录，FAILED_DIRECTORY 换成一个尚不存在的完整目录，例如 /opt/echotrace.failed-20261113：

~~~sh
# [Echo｜任意目录；替换两个大写路径后执行]
mv /opt/echotrace FAILED_DIRECTORY
mv BACKUP_DIRECTORY /opt/echotrace
sh /opt/echotrace/S95echotrace start
sh /opt/echotrace/S96echotrace-deskbot start
~~~

配置也改过时，从部署前备份恢复 /etc/echotrace/edge.json；更换令牌前同样备份并成对恢复两端令牌。数据库有格式变化时使用对应迁移和备份，不只回退二进制。

首次采用本教程安装、没有 /opt 备份时，原来的 /root/echotrace 开发安装仍在；恢复步骤见 [原开发安装说明](integration/deskbot/README.md)。

### 7.5 开发时如何提交

**[Mac｜主源码目录]** 每个功能一个短期分支；不要提交录像、模型和凭据：

~~~sh
sh scripts/setup_dev.sh
git switch main
git pull --ff-only
git switch -c feat/ET-002-projects
~~~

改完后用 git diff 查看，git add 明确的源码路径，git commit 提交，推送该分支并创建 PR。main 的必需检查通过后合并。完整规范见 [CONTRIBUTING.md](CONTRIBUTING.md)，版本包与标签见 [发布流程](docs/releases.md)。

## 8. 常见问题

| 现象 | 在哪里查、怎么处理 |
|---|---|
| wsl -u、netsh 提示找不到 | 这两类命令在 Windows PowerShell 运行；Ubuntu 中直接使用 Linux 命令 |
| Could not resolve hostname wsl/lubancat | [Mac] 按 1.3 设置 SSH 名称；先能登录，再继续 |
| pip 提示 externally-managed-environment | 使用 .venv/bin/python -m pip，不用系统 pip 或 sudo pip |
| 找不到 arm-rockchip…gcc | [WSL] 重设 3.1 的 ECHO_SDK，确认 SDK 工具链已解压 |
| cannot find -lasound、找不到 SDL2/DRM | 缺的是 ARM sysroot 的库；检查原 SDK 构建产物，不用 x86 库凑数 |
| Run …fetch_build_dependencies.py | [对应构建机] 在仓库根目录运行该脚本，确认 build/deps 已生成 |
| source changed、upstream source changed | 原工程与固定基线不符；核对 integration/deskbot/baseline.json，逐项适配 |
| refusing to overwrite existing work directory | 新输出目录用于首次隔离构建；旧目录增量编译使用 3.5 最后两行 |
| npx: command not found、中文方框 | 按 3.3 生成字库，然后重新编译桌面；只重启不改变已编译字体 |
| Exec format error | 把 ARM 程序放到 WSL/Mac 跑了，或用了错误架构的库；用 file 核对 |
| scp 提示 SFTP 不可用 | 发往 Echo 时加 -O；Mac→WSL/RK 不需要 |
| 文件存在却提示 not found | 可能缺 ELF 加载器或动态库；确认用了匹配固件的 ARM/uClibc 工具链 |
| error while loading shared libraries | [Echo] 设置 5.4 的 LD_LIBRARY_PATH；检查桌面整个 bin、媒体库及 sysroot 库是否传全 |
| Address already in use、remote port forwarding failed | 相应机器的端口被旧服务/旧转发占用；确认所属进程，停止旧实例后重试 |
| 网页 401、WebSocket 403 | 浏览器和 Echo 使用同一份 RK 令牌，且 token 文件非空 |
| 小屏“采集服务未连接” | [Echo] 检查 /run/echotrace/control.sock；先启动 S95，再启动 S96 |
| 小屏“离线” | [Mac] 检查两个转发终端；[RK] 检查 runtime/hub.log；核对两端令牌 |
| 项目下拉框为空 | 先在浏览器新建项目，并保持设备在线同步项目列表 |
| model not ready、模型任务阻塞 | [RK] 运行 4.4 的 seal --check 和 doctor；VLM 还需对应 NPU 驱动与工作进程 |
| 回放暂时没有原片 | Echo 必须在线提供原件；点“获取原始片段”，等待补传，保留 SD |
| 双击 MP4 没声音 | 声音在 WAV 中，使用工作台联合回放 |
| requires mounted SD storage | [Echo] 用 /proc/mounts 核对 /mnt/sdcard；未挂载不启动采集 |

## 9. 代码与补充资料

| 路径 | 作用 |
|---|---|
| src/echotrace/ | Python 后台、SQLite、检索、模型适配与 API |
| src/echotrace/web/ | 离线浏览器工作台 |
| native/src/ | RV1106 采集、控制、持久日志、补传、共享设备锁 |
| native/lvgl/deskbot/ | 接入原桌面的“记忆”页面 |
| native/vlm/、native/qt/ | RK3576 的 NPU 工作进程和可选 Qt 工作台 |
| integration/deskbot/ | 原工程基线、三个补丁、隔离构建脚本 |
| bsp/、cmake/ | 系统服务、Buildroot 接入与工具链配置 |
| tests/ | 已有功能检查；日常改动运行相关部分即可 |

- [HTML 架构与使用手册](docs/EchoTrace_软件架构与使用手册.html) / [Markdown 手册](docs/software-guide.md)
- [当前功能开发任务](docs/roadmap.md)
- [部署与恢复背景](docs/deployment.md)
- [模型版本与部署记录](docs/models.md)
- [SD 记录](docs/sd-validation.md)、[声画同步](docs/physical-av-sync.md)、[实板记录](docs/verification.md)
- [未来双板 USB 直连](docs/usb-direct-validation.md)：尚未按本教程实际切线，不作为现有 SSH 链路的已完成能力
