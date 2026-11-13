# EchoTrace 接入 Echo-Mate：暂停检查点

范围更新：0.2 开发版已按用户要求移除脚踏、外接暂停开关和 HX711。下文涉及这些功能的待办仅为历史现场记录，不能作为当前开发要求；当前任务见 `roadmap.md`。

这是此前按用户要求在额度接近 10% 时保存的历史检查点。用户随后明确要求“忽略10%额度限制，继续工作”；以下旧 PID 和暂停描述不能代表当前状态，续做时先核对实板。

## 目标与已确定的规则

- 原版 Echo-Mate DeskBot 的聊天、AI 相机、游戏、设置、电机等功能保留；EchoTrace 是桌面抽屉中的“记忆”应用。
- 用户选定“录制优先”：录制时进入原聊天或 AI 相机，应提示先结束录制；原应用占用声画时，EchoTrace 不开始录制。
- 返回首页不结束 EchoTrace 会话；采集由后台服务维持。两板物理接法暂时维持 Echo USB → WSL，鲁班猫经 SSH 转发；用户尚未要求切换 USB 直连。

## 源码与构建

- 实际较新的 DeskBot 在 WSL `/home/guan/Echo-Mate/Demo/DeskBot_demo`，Mac 同名副本较旧。WSL 原工程六个相关文件的哈希已再次与 `integration/deskbot/baseline.json` 比对，完全一致，未被覆盖。
- `integration/deskbot/0001-echotrace-app.patch` 已针对 WSL 原工程通过 `patch --dry-run -p1`。补丁仅在设置 `ECHOTRACE_APP_ROOT` 时加入第 13 个页面；未设置时原构建不变。隔离构建目录是 WSL `/home/guan/EchoTrace/build/deskbot-integration/DeskBot_demo/build-echotrace`。
- 集成页源码在 `native/lvgl/deskbot/`；采集和原聊天/AI 相机使用 `native/src/media_lease.c` 的 `/run/echotrace-media.lock`。UI socket 发送使用 `MSG_NOSIGNAL`，避免断链时 SIGPIPE 杀死整个桌面。
- 首次实板截图发现集成页中文字成为方框、状态栏对比度低。已对所有页面子控件显式应用中文字库、调整状态字色、给设备冲突消息框应用中文字库，重新交叉编译成功。新版 GUI SHA-256：`024ad1e8cc34fd48a076a9f92c5e196669b09f490f455f6d0dba6de0708e05f0`。
- 采集服务交叉编译成功，SHA-256：`de12452cd627fe30bc4574c9ce5e101c6a83416a926b647addc39f37cec5ac46`。媒体锁测试在 WSL 主机和 RV1106 实板均通过，板端返回码 0。

## 当前开发板状态（最后核对于本轮暂停前）

- Echo SSH：`ssh -J wsl root@172.32.0.93`；用户已提供 root 密码，但密码未写入工程。开发用临时 askpass 工具在 `/private/tmp/echotrace_ssh_command.py` 和 `/private/tmp/echotrace_askpass.py`，凭据只经终端输入。
- 新采集服务：`/root/echotrace/echotrace-edge-integrated`，当时 PID 2562，PID 文件 `/run/echotrace-capture-manual.pid`，Socket `/run/echotrace-sd.sock`，SD 数据 `/mnt/sdcard/echotrace`。旧服务二进制 `/root/echotrace/bin/echotrace-edge` 仍保留。
- 集成桌面：`/root/echotrace/deskbot-app/bin/main`，当时 PID 2670，PID 文件 `/run/echotrace-deskbot.pid`。旧独立界面 `echotrace-terminal` 已停用，但二进制和 `S96echotrace-terminal` 仍保留。没有配置开机自启。
- 更换 GUI 前首页已通过 framebuffer 读取确认。新版刚运行时 MemAvailable 约 29MiB；用户打开新版“记忆”页后约 28.28MiB。此数值是空闲状态，不代表录制压力验收。
- Mac 本地转发 `127.0.0.1:18084` 到鲁班猫 Hub 已恢复，Echo 本地 `127.0.0.1:18081` 反向转发也已恢复；最后核对 `/api/status` 显示设备连接，Echo `state.json` 的 `connected` 为 true。SSH 转发会随会话结束而失效，继续时须重新检查。
- 最近一次查询中枢时 capture_state 为 `stopped`。之后用户按请求打开“记忆”页，但未被要求点击开始；继续时仍须重新核对会话状态。

## 尚未完成的验证

1. 用户已回复“已打开”新版“记忆”页。刚才的原始 framebuffer 已复制到 Mac：`/private/tmp/echotrace-deskbot-app-v2.fb`，320×240、RGB565LE、153600 字节；尚未转换和检查。可执行：

   ```sh
   ffmpeg -hide_banner -loglevel error -y -f rawvideo \
     -pixel_format rgb565le -video_size 320x240 \
     -i /private/tmp/echotrace-deskbot-app-v2.fb -frames:v 1 \
     -vf scale=960:720 /private/tmp/echotrace-deskbot-app-v2.png
   ```

2. 若字形仍异常，继续修正并在隔离构建中迭代；不要改 WSL 原工程。若正常，做一次短会话，测录制时内存与摄像头/麦克风冲突提示；核对返回首页后录制继续、结束后原聊天/AI 相机可再次进入。
3. 补充集成后的实板验证记录和部署快照。现有 `docs/deployment-current.json` 是更早的独立终端快照，不能当作当前 PID。
4. 正式 Buildroot 包目前仍安装独立终端。集成应用尚未纳入可开机自启的完整固件；制作镜像时必须改为只启动一个显示进程。HX711、脚踏、暂停开关仍未实体接线，旧电机引脚不释放。

## 已更新文档

- `README.md` 已注明 EchoTrace 作为 DeskBot 应用。
- `integration/deskbot/README.md` 说明隔离构建、当前开发安装、使用和恢复。
- `docs/deployment.md`、`docs/software-guide.md`、`docs/EchoTrace_软件架构与使用手册.html` 已加入集成状态；HTML 仍需最终浏览器排版核对。

当前进度可以直接从本检查点继续，无需重做原项目拆解或整套构建。

## 2026-11-13 05:00 自动续做后的状态

- 已查看此前保存的新版屏幕 `/private/tmp/echotrace-deskbot-app-v2.png`：中文字形与状态栏对比度修复成功，按钮和标签可读。项目名称过长时被截断，底部提示横向溢出；已在本地 `native/lvgl/deskbot/echotrace_app.c` 加入 UTF-8 安全的短项目名显示、缩短提示。这部分**尚未同步到 WSL、编译或上板**。
- 续做时发现集成 DeskBot 已退出，旧 PID 2670 是僵尸进程、退出码 11。随后另一个 `main`（PID 2871）曾触发 RV1106 内核故障：`rknpu_mem_create_ioctl` 申请 order-5 页面失败，之后在 `dma_buf_file_release` 出现 Oops，内核提示需要重启。完整板端日志已保存为 `docs/kernel-oops-2026-11-13.log`。这可能与原 AI 相机的 NPU 分配失败路径有关，但不能仅凭日志断定两次 `main` 退出的直接原因相同。
- 源码核对发现原 `yolov5_demo/cpp/AIcamera_c_interface.cc` 调用 `init_yolov5_model` 后未检查返回值，就解引用 `input_mems[0]`。对应模型初始化代码在 NPU 内存分配失败时也未先检查空指针。继续开发时应在**隔离副本**补齐失败路径和资源释放，编译后再做受控实板验证；不要修改 WSL 原工程。
- 故障前从 SD 复制的 `state.json` 显示顶层 `state=stopped`、`playback_active=false`；其中嵌套会话对象的旧 `state=recording` 不是当前录制状态。已保存内核日志并执行 `sync; reboot`。板端重启后 uptime 约 26 秒、SD 正常挂载、未见新 Oops。
- 为避免在故障路径未修复前再次启动原 AI 相机，重启后**暂时恢复稳定的独立 EchoTrace 终端**，并重启带共享锁的采集服务。最后核对：采集 PID 472，`/root/echotrace/echotrace-edge-integrated`；终端 PID 480，`/root/echotrace/bin/echotrace-terminal`；`MemAvailable` 约 31.4MiB。集成 DeskBot 暂时没有运行，原二进制仍在 `/root/echotrace/deskbot-app/bin/main`。这些 PID 会变化，续做前重新核对。
- Mac 的 `18084` 转发已断开，当前采集端为离线状态，SD 原件仍可本地记录。不要把离线状态当作直接 USB 双板连接已通过。

**下一步顺序：**先处理原 AI 相机 NPU 分配失败时的空指针和清理问题，并为隔离构建记录原始文件哈希；再验证集成桌面正常页面切换与短录制资源占用。只有在原聊天和 AI 相机安全退出、录制冲突提示都通过后，才把集成 DeskBot 恢复为板端常用界面。用户要求额度剩余约 10% 时停止，续做时先用 `mcp__codex_app__get_usage_limits` 检查；本次 05:00 续做时二级窗口已用 88%、约余 12%，因此已保存现场并暂停。

## 2026-11-13 10:00 后续实施记录

- 用户已取消 10% 额度暂停要求。本节更新了上面历史快照，旧的“下一步顺序”不再是当前待办。
- 在 WSL 隔离构建中新增 `0002-ai-camera-backend.patch` 和 `0003-ai-camera-page.patch`。模型分配、张量绑定、摄像头打开及推理失败均有错误出口；清理张量内存先于销毁 RKNN 上下文，移除了 `rknn_destroy_mem` 后的重复 `free`；检测文字改为有界写入。页面增加连续内存预检和错误提示。原 WSL DeskBot 与 YOLO 九个文件的 SHA-256 仍与 `baseline.json` 相同。
- `tests/yolo_failure_test.cpp` 通过 RKNN mock 注入分配和绑定失败；`tests/run_yolo_failure_test.sh` 在 WSL 以 ASan/UBSan 与泄漏检测运行，退出码 0。RV1106 交叉构建成功。此测试不代表内核 DMA/Oops 已修复。
- 将桌面新二进制部署到 `/root/echotrace/deskbot-app/bin/main`，SHA-256 为 `5dea9ab42d914f71f9f933e3ce44456cf08964b7ef5e1999e881bdb9aef6662d`；旧版保留在同目录的 `main.before-camera-fix`。原独立终端和启动脚本保留，用于回退。集成桌面与采集服务已重新启动，最近核对的 PID 分别为 1035 与 472；两者都须在实际操作前重新核对。
- Mac `127.0.0.1:18084` 正向转发和 Echo `127.0.0.1:18081` 反向转发已恢复；Hub `/api/status` 显示 Echo 在线，Echo SD 上的 `state.json` 显示 `connected=true`。这些 SSH 转发只在其会话存活期间有效，仍非双板 USB 直连。
- 用户打开新版“记忆”页后读取实板 framebuffer，保存为 `/private/tmp/echotrace-fb-open.png`。中文字形、项目下拉、按钮、状态栏和底部提示可读。UI 仍将过长的历史项目名省略显示，完整名称在索引中保留。
- 用户点击“开始记忆”后，Hub 显示 `capture_state=recording`、活动会话 `54450e0b-0703-4f72-bac8-bacad40b8815`。当时 Echo `MemAvailable` 约 23.95 MiB。固件使用释放型 CMA；此前实测已确认 `CmaFree=0` 不是耗尽指标，不能据此判断媒体内存状态。
- 本次会话由 Hub 正常结束，来源时长约 405 秒。Hub 时间线有 15 个锚点、80 个媒体分段、0 个缺口；其中 77 个仍是源端原件，3 个已按需缓存，不能把“远端”状态当作媒体丢失。停止后声画锁可重新获取，`MemAvailable` 约 26.0 MiB，`CmaAllocated=12kB`、`CmaReleased=67572kB`，未见新的内核 Oops。录制时 shell 对共享锁的非阻塞获取返回 1，停止后返回 0。
- Echo `state.json` 曾报 `media request queue full`。从源码可见源端补传队列上限为 32，Hub 对这类 `Conflict` 会延后重试；仍需核对该次补传最终完成情况。用户尚未反馈从首页点击原聊天时的实际提示，页面级拦截不能仅凭锁试验宣称已验证。
- 之后修复了后台 `media.request` 覆盖小屏通用错误栏的问题。RV1106 交叉编译通过，更新后的采集二进制 SHA-256 为 `b52fb95ac1c40c2e540d2924517fded9fb4615b4f811fcb9fd17d95b756c5bd4`，已在会话结束后替换并启动；旧版备份为 `echotrace-edge-integrated.before-quiet-errors`。最近核对采集 PID 1307、集成 DeskBot PID 1035、Hub 在线、Echo 状态 `stopped` 且错误栏为空。旧 PID 仍可能变化。
- 用户表示当前不方便操作屏幕，要求自行查看。已读实板 framebuffer，画面停留“记忆·已停止·在线”。原桌面通过 `/dev/input/event0` 接收实体触摸，系统没有 `/dev/uinput` 或可用虚拟输入模块；未远程伪造点击。原聊天、AI 相机的页面级操作仍待实板交互验证。
- 新增 `integration/deskbot/build_integrated.sh`，在 WSL 全新目录核对九个基线哈希、复制原工程、应用三个补丁、运行故障注入测试并交叉编译，退出码 0。新旧隔离目录的 DeskBot 源码一致、二进制同为 9,374,092 字节，SHA 不同；当前声明源码可重新构建，不声明位级可重现。
- Buildroot 外部包新增可选 `BR2_PACKAGE_ECHOTRACE_DESKBOT`，选择集成桌面时不构建独立终端，只安装集成桌面和 `S96echotrace-deskbot`；默认配置保留独立终端。两种 Make 安装分支通过 dry run，Kconfig 解析退出码 0，`ECHO_BUILD_LVGL=OFF` 的 RV1106 交叉编译通过。尚未构建/刷写完整固件。
- 新启动脚本在实板开发路径通过 `status`、重复 `start` 和 `restart`。GUI PID 从 1035 变为 1426，采集 PID 保持 1307；重启后 framebuffer 显示首页，普通可用内存约 29.28 MiB，未见新内核 Oops。该脚本只是开发路径实测，完整开机自启仍未验收。
- 后续发现中枢旧日志反复出现 `cannot reconcile another source clock`，导致 Echo WebSocket 连接循环断开，媒体和 AI 任务进度停滞。根因是 SD 日志包含旧 `boot_id` 会话的历史 `paused` 重放；三个抽查会话在 Hub 已停止。Hub `session.reconcile` 现忽略旧启动周期非结束状态、已停止会话历史非结束状态和来源时间倒退的非结束状态，同时允许旧会话的 `stopped` 关闭中断会话。
- 新增 `test_old_boot_reconcile_replay_keeps_connection`。主机完整 `test_integration.py` 9 项通过，鲁班猫上该用例通过。中枢进程从 217437 更新为 845013，旧源码备份为 `/root/echotrace/src/echotrace/app.py.before-replay-fix`；新日志短时观察没有协议拒绝。先前排队的视频已到 `ready`，新会话锚点 `41097817-f20f-4d0b-a4a5-c769de69eb71-4` 成功提取 1920×1080 JPEG。最近 200 个任务中，完成数从 165 到 173，待处理从 35 到 26；应继续观察长期连接与补传。原始 SD MP4 可完整解码 250 帧/10 秒，但镜头主要拍到天花板，不能用于工作台质量评测。
- 随后确认全库任务暂时清空时，这次录制 15 个 `scene` 锚点都无描述任务，原因是队列满时省略的自动任务没有恢复路径。已添加空闲时的有界补建，主机 16 项记忆测试与 9 项集成测试通过，RK 上新增用例通过。更新中枢至 PID 856862 后，本次 15/15 个画面描述均生成，带会话范围的文字查询返回证据；新日志 4 分 30 秒观察内 0 次协议拒绝。模型文本中部分复述提示，且机位不是工作台，详情见 `docs/deskbot-integration-test-2026-11-13.md`。
