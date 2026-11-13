# RV1106 BSP 基线与启动配置

当前功能使用 Echo-Mate 自带的摄像头、ISP/CIF、音频、LCD、触摸和 USB 网络。BSP 采用匹配的原厂 SDK；外接输入与称重相关实验已从当前源码移除，旧实验可以从 `v0.1.0-baseline.1` Git 标签查阅。

## 已读取的板端信息

- WSL SDK 内核为 Linux 5.10.110，RV1106 交叉工具链为 GCC 8.3.0 / uClibc。
- 运行中 FDT 的首个 memory 范围为 134,217,728 字节，即 128MiB。普通内存与媒体 CMA 应分别统计，不能按通用硬件介绍中的 256MiB 估算本板资源。
- 采集、显示、音频和 USB 配置应以当前实板及选定板型为基准，原电机和蓝牙引脚继续由原功能使用。

## 启动介质差异

此前读取的 WSL SDK `.BoardConfig.mk` 指向 **SD_CARD** 配置，rootfs 使用 ext4。实板根目录实际来自 `ubi0:rootfs`、UBIFS，SD 卡 `/mnt/sdcard` 用于存放记录。

SDK 已有 **SPI_NAND** 配置，其 7 个分区大小匹配已读取的 `/proc/mtd`：256KiB env、256KiB idblock、512KiB U-Boot、4MiB boot、30MiB OEM、10MiB userdata、210MiB rootfs。分区一致只能说明它是候选基线，不能直接认定与当前固件完全相同。

## 后续构建

从已提交的 EchoTrace 版本构建，使用 `bsp/buildroot-external` 安装采集服务、集成 DeskBot 与启动脚本。厂商 SDK 在独立工作目录选择匹配板型，构建信息按 [发布流程](releases.md) 保存产品版本、源码提交、工具链、运行时及产物摘要。

完整镜像需要固定启动配置、SD 挂载、唯一显示进程、系统启动顺序和恢复通路。当前已提供包与脚本，尚未构建和刷写完整产品固件。
