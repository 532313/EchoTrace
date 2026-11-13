# 小屏 UI 与 30fps 回放

适用版本：`0.2.0.dev3`。日常操作见 [小屏说明](small-screen.md)，本页说明最终画质、测量范围和当前设备的更新回退。

## 使用入口

从 Echo-Mate 首页抽屉进入“记忆”。四个页签保留原功能，界面统一为浅色卡片、绿色主要操作、红色停止操作；录制页固定预览位置，历史列表以项目、时长和步骤数量呈现。

在“历史”或查询结果中打开步骤，底部固定“回放 / 改笔记 / 更多”。“更多”提供刷新、画面对照和 SD 导出，不需要滚过长笔记寻找按钮。

点“回放”进入独立播放器。左边是当前时间和总时长，右边是实际渲染帧率，下方有可拖动进度条和较大的重播、暂停、停止按钮。返回可继续阅读原步骤。

## 最终配置

| 项目 | 配置 |
|---|---|
| 小屏回放 | 160×90 RGB565，30fps 输出 |
| 原片 | 原分辨率、帧率和文件保留；本轮使用的原片为 1920×1080、约 25fps |
| 原片低于 30fps 时 | 重复部分画面配到 30fps 输出，不增加原始细节 |
| 本地缓冲 | 最多 6 帧，开始前缓冲 4 帧，约 100ms |
| 声画调度 | RK 提前发送画面，音频按播放时钟启动；源端时间不改变 |
| 显示刷新 | 固定节拍提交，局部绘制；动态时长、帧率和进度条靠近画面，减少 LCD 重传行数 |

ST7789V 的驱动按整行更新。240×136 的候选配置中，软件渲染约 30fps，但 LCD 实际更新约 24–25fps。因此最终保留 160×90 小屏画质，优先完成实际 30fps 更新；在 Web/Qt 中仍可查看原片。

## 回放链路

```text
RK：读取缓存原片 → FFmpeg 解码、缩放、30fps 调度
   → 协议 v3 二进制帧包（流 UUID＋来源时间＋RGB565）
Echo：WebSocket 收帧 → 非阻塞 Unix 数据报
   → GUI 有界帧缓存 → 固定节拍绘制 → framebuffer → SPI LCD
```

视频不再经过 Base64、逐帧临时文件和 150ms 状态轮询。低频状态与画面分离；跳转建立新流 UUID，旧流帧不会串入新位置。缓冲溢出或处理迟到时丢弃显示帧，原始证据保留。

沿用 [LVGL 局部绘制接口](https://docs.lvgl.io/9.2/porting/display.html)，帧率转换使用 [FFmpeg fps 滤镜](https://ffmpeg.org/ffmpeg-filters.html#fps-1)。原 Echo-Mate 源码仍通过基线检查，在隔离副本中应用补丁。

## 实板结果与范围

2026-11-14，在当前 Echo→WSL→Mac SSH 转发→RK 接法下，使用一段已有操作录像完成短时回放。测量代码提交为 `cb27b0e74016dfafc0742bc5945256e05fcf5e6e`，数据见 [结果摘要](ui-playback-30fps.json)。

| 指标 | 结果 |
|---|---:|
| 帧接收 / 转交 GUI | 300 / 300 |
| 传输丢帧、后台迟到帧 | 0 / 0 |
| GUI 渲染平均值，约 6 秒窗口 | 30.13fps |
| GUI 最后一个播放窗口 | 29.99fps |
| LCD 实际更新，保留的约 5.60 秒窗口 | 30.02fps |
| LCD 日志中的帧率中位数 | 30fps |

GUI 数据来自绘制完成事件，LCD 数据来自 FBTFT 的实际更新日志。LCD 统计使用保留日志中请求后第 3.39–8.98 秒的 169 次更新，避免把 30fps 参数当作实测值。原片约 25fps，以上是输出和更新频率。

本轮只完成与改动相关的编译、功能检查和短片段观察，没有重复开展长期压力实验，也未重新测量声画同步精度。测量期间临时静音，结束后已恢复音量 25；LCD 只临时启用更新时间日志，结束后恢复为 0。

## 当前安装和后续更新

日常启动、网络和原数据路径沿用 [dev2 安装](deployment-dev2.md)，新安装清单是两端各自的 `/root/echotrace/installed-dev3.manifest.json`。它记录最终安装提交和产物摘要；提交中后续仅有说明更新时，不会把它写成重新完成了性能测量。

当前 WSL 桌面输出为：

```text
/home/guan/EchoTrace-git/build/deskbot-app-dev3/DeskBot_demo/build-echotrace/bin/main
```

修改 `integration/deskbot/*.patch` 时使用新的空输出目录执行隔离构建；只改页面源码时可以增量构建。升级时同步更新 RK 后台、Echo 采集端和集成桌面，不能只更换 GUI。

`S96echotrace-deskbot` 默认把 ST7789V 的调试值设置为 0。原厂值 7 会展开为 `0xffffffff`，逐次打印 SPI 数据，影响刷新。需要采集短时间的 LCD 更新日志时，在 Echo 上使用 `32`，完成后恢复 `0`：

```sh
# [Echo｜任意目录] 仅修改运行中的调试日志开关
echo 32 > /sys/class/graphics/fb0/debug
# 操作一次短回放后读取；不要清空已有内核日志
dmesg | tail -n 100
echo 0 > /sys/class/graphics/fb0/debug
```

## 回退到 dev2

先结束录制、语音输入和回放。Echo 的上一版程序在 SD 备份中，旧桌面启动脚本与旧安装清单的摘要一致。RK 的 SQLite 备份在 `/root/echotrace/install-backups/pre-dev3-20261114/`。本次没有数据库格式迁移，不覆盖当前数据库和新录像。

**[Echo｜任意目录]**

```sh
/etc/init.d/S99z_echotrace stop
cp /mnt/sdcard/echotrace/install-backups/pre-dev3-20261114/edge /root/echotrace/echotrace-edge-integrated
cp /mnt/sdcard/echotrace/install-backups/pre-dev3-20261114/deskbot /root/echotrace/deskbot-app/bin/main
cp /mnt/sdcard/echotrace/install-backups/pre-dev3-20261114/S96echotrace-deskbot /root/echotrace/S96echotrace-deskbot
chmod 755 /root/echotrace/echotrace-edge-integrated /root/echotrace/deskbot-app/bin/main /root/echotrace/S96echotrace-deskbot
```

**[RK｜/root/EchoTrace-git]** 确认源码工作区干净，再切换固定标签：

```sh
systemctl stop echotrace-memory.service
cd /root/EchoTrace-git
git status --short
git switch --detach v0.2.0.dev2
systemctl start echotrace-memory.service
```

最后 **[Echo｜任意目录]** 执行 `/etc/init.d/S99z_echotrace start`。继续使用当前专用令牌、数据目录和 18091 转发，不切回更早的旧链路。
