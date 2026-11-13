# 0.2.0.dev2 实板安装与恢复

当前已更新到 `0.2.0.dev3`。运行路径和网络沿用本页，新的 UI、30fps 回放、安装清单和 dev2 回退步骤见 [新版回放说明](ui-playback-30fps.md)。以下保留 dev2 安装记录。

2026-11-14，完整小屏程序部署到 Echo-Mate，后台部署到鲁班猫 3。沿用现有接线、原模型、数据库和 SD 原件；没有刷写 BSP 或替换系统媒体库。小屏操作见 [使用说明](small-screen.md)。

## 当前安装

| 位置 | 实际路径或状态 |
|---|---|
| Mac 源码 | `/Users/gsy/Desktop/嵌入式Linux智能安防监控系统/EchoTrace` |
| WSL 构建副本 | `/home/guan/EchoTrace-git` |
| WSL 隔离桌面 | `/home/guan/EchoTrace-git/build/deskbot-app-dev2/DeskBot_demo/build-echotrace/bin/main` |
| Echo 采集程序 | `/root/echotrace/echotrace-edge-integrated` |
| Echo 桌面程序 | `/root/echotrace/deskbot-app/bin/main` |
| Echo 声画原件、事件 | `/mnt/sdcard/echotrace`，沿用旧目录 |
| Echo 服务配置 | `/etc/echotrace/edge.env`、`/root/echotrace/edge-dev2.json` |
| Echo 精确安装清单 | `/root/echotrace/installed-dev2.manifest.json`，记录提交、工具链和 SHA-256 |
| Echo 控制 socket | `/run/echotrace-sd.sock` |
| Echo PID 文件 | `/run/echotrace-capture-manual.pid`、`/run/echotrace-deskbot.pid` |
| RK 新 Git 源码 | `/root/EchoTrace-git` |
| RK 数据库、媒体缓存 | `/root/echotrace/runtime/real-smoke`，沿用旧目录 |
| RK 模型、Python 环境 | `/root/echotrace/models/config.json`、`/root/echotrace/.venv` |
| RK 安装清单 | `/root/echotrace/installed-dev2.manifest.json` |
| RK 后台服务 | `echotrace-memory.service`，已启用，监听 `127.0.0.1:18081` |

两端使用本次新生成的专用令牌，保存在各自 `/root/echotrace/token-dev2`；不读出、复制或覆盖原令牌。RK 的私有环境文件为 `/root/echotrace/hub-dev2.env`。令牌和环境文件不进入 Git。

这套安装与 README 中的 `/opt/echotrace` 新装教程是两个路径方案；在当前设备继续使用本页命令，不重复安装教程中的新数据库。更新流程仍使用功能分支、PR、CI、固定提交构建及产物清单。

## 每次开机后

鲁班猫的 Hub 已交给 systemd；Echo 安装了晚于原厂模块初始化的 `/etc/init.d/S99z_echotrace`。它调用原有采集、桌面启动脚本，默认先显示原桌面，不自动开始录制。缺卡时仍尝试启动桌面。已在当前运行系统执行启动入口；本轮未重启两块开发板，因此不声明整机启动过程已验证。

目前仍需要 Mac 和 WSL 维持网络。**[Mac｜两个终端分别运行]**，这两个命令保持运行：

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L 127.0.0.1:18094:127.0.0.1:18081 lubancat
```

```sh
ssh -N -J wsl -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -R 127.0.0.1:18091:127.0.0.1:18094 root@172.32.0.93
```

本轮已有这些端口转发时无需再启动。端口被占用时检查已有转发，不停止其他不明进程。重启或关闭 Mac 后需要重新建立。真实 Echo→RK USB 直连仍待按用户安排切换接线，不能把当前 SSH 通路当作已完成的独立双板连接。

需要手动恢复时：

**[RK｜任意目录]**

```sh
systemctl start echotrace-memory.service
systemctl status echotrace-memory.service --no-pager
```

**[Echo｜任意目录]**

```sh
/etc/init.d/S99z_echotrace start
/etc/init.d/S99z_echotrace status
```

只重启桌面：

```sh
# [Echo｜任意目录] 不结束后台正在进行的录制
sh /root/echotrace/S96echotrace-deskbot restart
```

如果只想启动时直接进入记忆：

```sh
# [Echo｜任意目录] 先结束回放或语音输入，再重启界面
sh /root/echotrace/S96echotrace-deskbot stop
ECHOTRACE_OPEN_APP=record sh /root/echotrace/S96echotrace-deskbot start
```

可选值为 `record`、`project`、`history`、`ask`、`anchor:步骤ID`；后者只打开已有步骤，不启动录制。默认不设置，沿用原首页。

## 备份及回退

Echo 的上一版程序和配置保存在 `/mnt/sdcard/echotrace/install-backups/pre-dev2-20261114/`。其 `manifest.json` 记录旧文件摘要；旧安装早于 Git 管理，无法确认源码提交，因此明确标为空，没有冒认某个标签。

RK 的旧源码和停止服务后的 SQLite 备份位于 `/root/echotrace/install-backups/pre-dev2-20261114/`。原模型和录像没有移动。回退前先结束录制、语音输入和回放，保留新增数据；本次没有数据库格式迁移，普通程序回退无需覆盖当前数据库。

**[Echo｜任意目录]** 恢复旧程序和旧配置，停用新自动启动入口：

```sh
/etc/init.d/S99z_echotrace stop
mv /etc/init.d/S99z_echotrace /root/echotrace/S99z_echotrace.disabled
cp /mnt/sdcard/echotrace/install-backups/pre-dev2-20261114/edge /root/echotrace/echotrace-edge-integrated
cp /mnt/sdcard/echotrace/install-backups/pre-dev2-20261114/deskbot /root/echotrace/deskbot-app/bin/main
chmod 755 /root/echotrace/echotrace-edge-integrated /root/echotrace/deskbot-app/bin/main
cp /mnt/sdcard/echotrace/install-backups/pre-dev2-20261114/edge-sd.json /root/echotrace/edge-sd.json
```

旧 Edge 使用原令牌及旧 `127.0.0.1:18081` 转发。恢复旧链路和手动启动参数见 [原集成安装记录](../integration/deskbot/README.md)；不要用新配置启动回退后的旧采集端。禁用时把入口移出 `/etc/init.d/S*`；实际 rcS 会遍历所有匹配文件，不能把备份留在该列表里。

**[RK｜任意目录]** 停用新服务，再恢复旧源码启动方式：

```sh
systemctl disable --now echotrace-memory.service
cd /root/echotrace
nohup .venv/bin/python -m echotrace serve --data runtime/real-smoke --port 18081 > runtime/hub-rollback.log 2>&1 &
```

旧源码仍保留在 `/root/echotrace/src`，新服务通过 `PYTHONPATH=/root/EchoTrace-git/src` 使用新源码；停用新服务后，以上命令即使用旧源码。需要恢复数据库时先另外备份当前数据库，再使用 SQLite 备份恢复；不要覆盖录制原件。

## 本轮完成范围

已完成固定提交的 ARM 采集及全新隔离 DeskBot 编译、两端安装和原数据沿用；实屏确认历史列表和原始步骤画面，原工程九个文件基线一致。功能级可移植检查与 PR CI 已通过。此记录不代表长期稳定性、真实声画同步精度、检索指标、原聊天远程服务或完整 BSP 镜像已验收。
