# EchoTrace

面向嵌入式开发、拆装和制作的离线过程记忆机器人。Echo-Mate 保存操作现场，鲁班猫 3 建立可检索的时间线；每个结果都能回到原始记录。

当前源码处于 **0.2.0.dev0 开发阶段**，目标是能在 Echo-Mate 小屏完成项目、录制、主动提问、历史与声画回看的完整 App。已有底层能力和完整小屏产品之间的缺口见 [开发任务](docs/roadmap.md)。Git 分支、提交、CI 与评审约定见 [开发流程](CONTRIBUTING.md)，版本、源码包和产物追溯见 [版本发布](docs/releases.md)。

本工程保留独立的采集与记忆服务，同时把 EchoTrace 接入原版 Echo-Mate DeskBot，成为抽屉里的“记忆”应用。原聊天、AI 相机、游戏、设置和电机功能保留；集成补丁仅应用于隔离构建副本，原工程源码与原可执行文件不被覆盖。接入和恢复步骤见 [DeskBot 集成说明](integration/deskbot/README.md)。

完整的软件分层、当前功能边界、真实工作台入口和操作流程见 [HTML 软件架构与使用手册](docs/EchoTrace_软件架构与使用手册.html)，也可阅读 [Markdown 版本](docs/software-guide.md)。

> 0.1 核心首版已在 RV1106 和 RK3576 上跑通真实采集、原件补传、离线模型和带证据的检索；新版一小时 SD 录制 0 次音频 XRUN，702 个原件全部巡检通过。整小时物理同步、外设接线和真实操作质量仍待验收，详见验证记录。

## 快速运行

需要 Python 3.9+、FFmpeg；C++ 构建需要 CMake 3.16+ 和支持 C++17 的编译器。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[ai]'
.venv/bin/python scripts/prepare_sources.py
.venv/bin/python scripts/fetch_build_dependencies.py
cmake -S . -B build/host -DCMAKE_BUILD_TYPE=Debug
cmake --build build/host -j4
.venv/bin/python -m echotrace demo --data runtime/demo
.venv/bin/python -m echotrace serve --data runtime/demo --port 18081
```

打开 <http://127.0.0.1:18081>。示例是 FFmpeg 生成的视频与人工编写的测试笔记，界面会显示“模拟素材”。它用于验证检索、关键帧、对比与导出，不用于计算产品准确率。

另外开一个终端，可运行明确标记的模拟采集端：

```sh
build/host/echotrace-edge --simulated --data runtime/simulated-edge \
  --socket /tmp/echotrace-edge.sock --hub ws://127.0.0.1:18081/ws/device
```

在界面创建项目并开始记录。模拟端也执行真实的分段文件写入、来源时间、持久日志、补传与校验流程。硬件版不带 `--simulated`，缺少硬件后端时会拒绝启动。

## 工程入口

| 路径 | 作用 |
|---|---|
| `src/echotrace/` | 记忆服务、SQLite/FTS5、混合检索、模型适配与 HTTP/WebSocket API |
| `src/echotrace/web/` | 本地浏览器工作台，不依赖在线 CDN |
| `native/src/capture_rv1106.cpp` | VI→VENC NV12 硬件编码、ALSA、RTSP 和画面变化候选 |
| `native/src/inputs_linux.cpp` | evdev 脚踏/暂停、IIO HX711、校准与稳定状态判断 |
| `native/src/edge_main.cpp` | 常驻采集服务、持久事件日志、命令确认、补传和本地控制口 |
| `native/qt/` | Qt 工作台，复用原安防工程的 FFmpeg 回放组件 |
| `native/lvgl/` | 独立终端，以及接入原 DeskBot 的 EchoTrace 应用页面 |
| `integration/deskbot/` | 原桌面基线哈希、可核对补丁、集成构建与恢复说明 |
| `native/vlm/` | 使用官方 RKNN/RKLLM API 的进程隔离推理程序 |
| `bsp/rv1106/` | HX711 修复补丁、候选设备树、配置片段、IIO 配置与启动服务 |
| `bsp/rk3576/` | 记忆服务模板 |
| `tests/` | 原始事实、协议、HTTP Range、驱动错误路径和原生端断线集成测试 |

构建会生成 `build/vendor/sources.lock.json`。本地原项目的部分 Git 对象不完整，因此复用以文件字节哈希为准；适配脚本遇到不匹配的代码片段会失败，防止静默套用错误接口。

当前实测：1080p/25fps 短时录像与 LVGL 并发时普通内存最低约 24.14MiB；RK3576 五组模型共存执行时最低可用内存约 1.41GiB；一万条模拟锚点的纯检索 P95 约 0.70 秒。以上不代表长时稳定性或检索准确率。

2026-11-13 已在 RV1106 实板启动集成 DeskBot 和带共享设备锁的采集服务；“记忆”页短录制、SD 原件抽查和共享锁持有/释放已通过。中枢已修复旧启动日志重放导致的断连，并会在 AI 队列清空后有上限地补建被拥堵跳过的自动画面描述。原聊天的页面级提示与 AI 相机真实启动仍待单独上板验证，见 [集成短测](docs/deskbot-integration-test-2026-11-13.md)。

## 验证与部署

```sh
ctest --test-dir build/host --output-on-failure
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
node --test tests/playback_web.test.cjs
```

- [实板状态与验证记录](docs/verification.md)
- [SD 卡接入与一小时记录](docs/sd-validation.md)
- [BSP 隔离编译与启动介质核对](docs/bsp-validation.md)
- [物理声画同步测量](docs/av-sync-measurement.md)
- [六下真实拍手短测](docs/physical-av-sync.md)
- [双板 USB 直连准备](docs/usb-direct-validation.md)
- [部署与恢复](docs/deployment.md)
- [架构、协议和事实来源](docs/architecture.md)
- [模型准备与版本固定](docs/models.md)
- [评测集与验收方法](docs/evaluation.md)

对外监听必须设置非空的 `ECHOTRACE_TOKEN`，HTTP 和 WebSocket 使用相同的 Bearer token。默认仅监听回环地址。程序运行时不联网下载模型；缺少或不匹配的模型会显示未就绪。

当前 Echo-Mate 固件的设备树配置为 128MiB 内存，其中约 66MiB 用于媒体 CMA；Linux 报告的普通内存约 54MiB。这里使用硬件绑定编码、限长队列、流式补传和按需加载媒体清单，不能套用“256MB 终端”的内存预算。

Qt 和浏览器均支持整个证据范围内的连续片段切换、暂停与跳转；缺失或未缓存的区间会明确暂停。新建 `demo` 数据目录会生成每段 4 秒的多片段素材，旧示例目录保持原样。在 RK3576 构建 `echotrace-playback-test` 后，Python 回归会自动执行真实解码、反向跳转、非零 PTS 与声音请求顺序测试。

浏览器控制回归需要 Node.js 18+；应用和设备运行不依赖 Node.js。
