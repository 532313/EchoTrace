# RV1106 BSP 隔离编译记录

2026-11-12，在 WSL 使用匹配 SDK 的 GCC 8.3.0，完成 Linux 5.10.110 的 HX711 修复对象和候选外设 DTB 编译。这里没有构建完整固件、刷写设备或加载新模块，也没有接入真实称重传感器。

结果见 [机器记录](bsp-validation.json) 和 [完整构建日志](build-bsp-rv1106.log)。

| 项目 | 结果 |
|---|---|
| 驱动 | `drivers/iio/adc/hx711.o`，209,820 字节，ELF32 / ARM / EABI5 |
| 驱动修复 | 读取负错误码时不提交样本，统一解锁、通知触发完成；实际内核头文件下编译通过 |
| DTB | `rv1106g-echo-mate.dtb`，37,633 字节；包含外设源码片段后生成 |
| 配置 | HX711=m、IIO_HRTIMER_TRIGGER=m，IIO 缓冲与触发等要求全部保留，逐项核对 `.config` |
| 原 SDK | 原内核 Makefile、`.config`、HX711、两种 BoardConfig 和 Buildroot defconfig 摘要在构建后核对一致；原板级 DTS 与 Mac 基线摘要也一致 |
| 实板内存 | 运行中 FDT 的首个 memory 范围为 134,217,728 字节，即 128MiB；不能继续使用作者通用介绍中的 256MiB 估算本板资源 |

这个 SDK 的 DTC 使用 `-Wnode_disabled -Wnode_empty`。其 `scripts/dtc/checks.c` 中 `fixup_node_disabled()` 会把非 ok/okay 节点标为可省略。因此，本次 DTB 不包含禁用的 `echotrace-weight` 和 `echotrace-keys`，保留了尚未被设备使用的 pinctrl 分组。接线核实后，需要在源码中明确启用再编译；当前编译结果没有启用候选 GPIO。

构建日志含老内核证书工具与 OpenSSL 3 的弃用警告。DTB 反编译还报告原板树已有的相机地址、时钟、GPIO 等警告；与 SDK 原 DTB 使用相同工具比较，警告列表一致。本次没有执行 `dtbs_check`，不宣称整个板级设备树通过了绑定 schema 检查。

## 复现命令

在 Linux/WSL 的融合项目目录执行，工作目录必须是原 SDK 之外的**新目录**：

```sh
scripts/build_bsp_probe.sh /home/guan/Echo-Mate/SDK/rv1106-sdk \
  /home/guan/EchoTrace/build/bsp-rv1106-new-run
```

脚本检查原始 HX711 字节摘要，复制内核源码到新目录，清理副本中的旧构建文件，在副本应用补丁和 DTS，合并配置，再执行 `olddefconfig prepare modules_prepare` 及指定对象/DTB 构建。失败结果保留在工作目录，既不清理原 SDK，也不覆盖已有测试目录。需要 `rsync`、`make`、`patch`、匹配交叉工具链及内核宿主构建依赖。

产物保留在 WSL `/home/guan/EchoTrace/build/bsp-rv1106-20261112/output`。源码包包含脚本、补丁、配置和报告，不夹带整份内核副本。

## 启动介质差异

WSL SDK 的 `.BoardConfig.mk` 当前指向 **SD_CARD** 配置，其 rootfs 使用 ext4。实板根目录实际来自 `ubi0:rootfs`、UBIFS，SD 卡 `/mnt/sdcard` 用于存放记录。

SDK 已有 **SPI_NAND** 配置，它的 7 个分区大小逐一匹配 `/proc/mtd`：256KiB env、256KiB idblock、512KiB U-Boot、4MiB boot、30MiB OEM、10MiB userdata、210MiB rootfs。可作为后续整机镜像构建的候选基线；不能仅凭分区一致就认定当前固件来自这份 SDK 或可以直接刷写。

本次没有改动 SDK 的默认板型链接。完整镜像仍需要固定启动配置、整合 Buildroot 包与 SD 挂载、验证启动服务、保存恢复通路，并在实际硬件接线核对后另行验收。
