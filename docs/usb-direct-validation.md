# 双板 USB 直连切换与验证

当前物理连接仍是 Echo→WSL，运行数据经桌面 SSH 转发到鲁班猫。下面是准备完成后的切换步骤，**尚未执行实际拔插或确认 USB 直连通过**。一小时录制期间保持原有接线。

## 已核实的信息

| 项目 | 实际读取结果 |
|---|---|
| 鲁班猫内核 | 6.1.99-rk3576；`CONFIG_USB_USBNET=y`、`CONFIG_USB_NET_CDCETHER=y`、`CONFIG_USB_NET_RNDIS_HOST=y` |
| 鲁班猫网络管理 | NetworkManager 1.42.4 正在运行；systemd-networkd 未运行 |
| 鲁班猫现有网络 | eth0 为 192.168.1.62/24，默认网关 192.168.1.1；保留现有 NAS、LAN 和 Tailscale 配置 |
| Echo USB 配置 | configfs gadget `rockchip`，VID:PID 为 `2207:0019`，序列号 `82712d0da3b8bdbb` |
| Echo 当前地址 | usb0：172.32.0.93；S90usb0config 在启动时设置此地址 |
| 私有令牌准备 | 已将板端同一令牌放入鲁班猫 `/root/echotrace/runtime/direct-usb/token` 与 `hub.env`，权限 0600；不进入源码包 |
| 鉴权预检 | 鲁班猫独立 loopback 实例：正确令牌 HTTP 200、WebSocket v3 hello 接受；无令牌和错误令牌 HTTP 401、WebSocket 403；测试实例已退出 |

鉴权结果见 [预检记录](usb-auth-preflight.json)。它没有使用真实 USB 传输。鲁班猫在预检时只有 eth0、tailscale0 等现有接口，不能预先假定新 USB 网卡叫 usb0 或某个固定名称。

## 切换前

1. 正常结束会话，确认终端为 stopped、媒体清单提交完毕且没有残留 `.part`；保存会话 ID、采集程序摘要和原件巡检结果。
2. 停止 Echo 采集与界面进程，等待退出并同步 SD。若该 USB 线同时供电，按实际供电条件正常关机后再换线，避免在 FAT32 写入期间断电。不要热拔 SD。
3. 确认鲁班猫通过 LAN/Tailscale 仍可 SSH；它的现有网口和默认路由保持原样。将数据线接到鲁班猫 USB Host，随后按实际供电方式启动 Echo。

## 识别接口和建立临时地址

先比较鲁班猫连接前后的 `ip -br link`、`lsusb`、`dmesg`。核对新增接口的 USB 父设备 VID/PID/序列号，以及 `/sys/class/net/实际接口/device/driver`，确认它确实属于此 Echo。只有完成核对后才填写 `USB_IF`，不能选择现有 eth0。

调试阶段先用 NetworkManager 建立**不保存**的专用连接；同时保留 Echo 现有 172.32.0.93 地址的访问路径。以下仅在新增接口已核实、且同名连接不存在时执行：

```sh
USB_IF=实际新增接口名
nmcli connection add type ethernet ifname "$USB_IF" con-name echotrace-usb-test \
  autoconnect no save no ipv4.method manual \
  ipv4.addresses '192.168.77.1/24,172.32.0.100/16' \
  ipv4.never-default yes ipv6.method disabled
nmcli --wait 10 connection up echotrace-usb-test
ping -c 3 172.32.0.93
ssh root@172.32.0.93
```

在 Echo 的现有 usb0 地址旁临时增加 `192.168.77.2/24`，保留 172.32.0.93；不要执行 flush 或更改默认路由。分别测试 Echo→192.168.77.1、鲁班猫→192.168.77.2。完成验收后再固化已实测的网络配置，不能同时让 networkd 接管该接口。

## 切换中枢与验证

停止旧中枢实例后再启动同一数据目录，避免两个进程同时编排相同 SQLite 数据库。私有环境文件已经准备好；只在 USB 地址存在、旧进程已退出时，使用该文件配置 `ECHOTRACE_TOKEN`，令中枢监听 **192.168.77.1:18081**。Echo 的 `--hub` 改为 `ws://192.168.77.1:18081/ws/device`，继续使用原 token 文件及 SD 数据目录。

先检查带令牌的状态接口、v3 设备握手和历史索引，随后创建明确标记的独立测试会话。不要在这个步骤自动开始或替换用户会话。

验收顺序：

1. 真实相机和麦克风录制至少一分钟，步骤标记返回成功，原始片段可下载并核验摘要。
2. 断开桌面 SSH 转发，确认设备仍连接、仍能登记媒体和查询；保留实际 socket 对端地址或抓包证据，证明媒体走的是 USB 私网。
3. 对于**有独立供电**的 Echo，可在录制期间断开 USB 数据连接约 30 秒，确认原件仍增长；重接后按事件/媒体 ID 补传，不出现重复记录。若 USB 同时供电，拔线属于断电测试，不能冒充单纯的网络断线。
4. 正常结束会话，巡检全量原件并与中枢清单比对；另行记录查询和媒体获取耗时。

临时连接可通过 `nmcli connection down echotrace-usb-test` 后删除同名连接回退。Echo 只删除本次添加的 192.168.77.2 地址，保留原始调试地址。回退不更改 NAS 网络或清理历史原件。
