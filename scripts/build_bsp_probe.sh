#!/bin/sh
# Compile the patched ARM driver and disabled peripheral DTS in an isolated copy.
# This does not build a flashable image, modify the SDK or install a module.
set -eu
[ "$#" -eq 2 ] || { echo 'usage: build_bsp_probe.sh RV1106_SDK NEW_WORK_DIRECTORY' >&2; exit 2; }
sdk=$(realpath "$1")
work=$(realpath -m "$2")
project=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
case "$work/" in "$sdk/"*) echo 'work directory must be outside the original SDK' >&2; exit 1 ;; esac
[ ! -e "$work" ] || { echo 'work directory must be new; existing results are preserved' >&2; exit 1; }
kernel=$sdk/sysdrv/source/kernel
prefix=$sdk/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin/arm-rockchip830-linux-uclibcgnueabihf-
[ -x "${prefix}gcc" ] && [ -r "$kernel/.config" ]
expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_sha256"])' "$project/bsp/rv1106/hx711-baseline.json")
actual=$(sha256sum "$kernel/drivers/iio/adc/hx711.c" | awk '{print $1}')
[ "$actual" = "$expected" ] || { echo 'HX711 baseline differs; review before compiling' >&2; exit 1; }
mkdir -p "$work/kernel" "$work/output"
cp "$kernel/.config" "$work/base.config"
readlink "$sdk/.BoardConfig.mk" > "$work/sdk-selected-board.txt"
sha256sum "$kernel/Makefile" "$kernel/.config" "$kernel/drivers/iio/adc/hx711.c" \
    "$kernel/arch/arm/boot/dts/rv1106g-echo-mate.dts" \
    "$kernel/arch/arm/boot/dts/rv1106-echo-mate-ipc.dtsi" \
    "$sdk/.BoardConfig.mk" \
    "$sdk/project/cfg/BoardConfig_IPC/BoardConfig-SPI_NAND-Buildroot-RV1106_Echo_Mate-DeskMate.mk" \
    "$sdk/sysdrv/source/buildroot/buildroot-2023.02.6/configs/echo_mate_defconfig" \
    > "$work/sdk-inputs.sha256"
"${prefix}gcc" --version > "$work/compiler.txt"
rsync -a --exclude=.git --exclude='*.o' --exclude='*.cmd' --exclude='*.a' \
    --exclude='*.ko' --exclude='*.dtb' --exclude='vmlinux*' "$kernel/" "$work/kernel/"
cd "$work/kernel"
make ARCH=arm CROSS_COMPILE="$prefix" mrproper
patch -p1 --fuzz=0 < "$project/bsp/rv1106/0001-hx711-reject-error-samples.patch"
case "$(realpath arch/arm/boot/dts/rv1106g-echo-mate.dts)" in
    "$work/kernel/"*) ;;
    *) echo 'board DTS resolves outside the isolated copy' >&2; exit 1 ;;
esac
cp "$project/bsp/rv1106/echotrace-peripherals.dtsi" arch/arm/boot/dts/
printf '\n#include "echotrace-peripherals.dtsi"\n' >> arch/arm/boot/dts/rv1106g-echo-mate.dts
scripts/kconfig/merge_config.sh -m -O "$work/output" "$work/base.config" "$project/bsp/rv1106/kernel.config"
make O="$work/output" ARCH=arm CROSS_COMPILE="$prefix" olddefconfig prepare modules_prepare
while IFS= read -r option; do
    case "$option" in CONFIG_*=*)
        grep -qxF "$option" "$work/output/.config" || {
            echo "requested kernel option was not retained: $option" >&2; exit 1;
        } ;;
    esac
done < "$project/bsp/rv1106/kernel.config"
make O="$work/output" ARCH=arm CROSS_COMPILE="$prefix" -j4 \
    drivers/iio/adc/hx711.o rv1106g-echo-mate.dtb
readelf -h "$work/output/drivers/iio/adc/hx711.o" > "$work/hx711-elf.txt"
sha256sum "$work/output/drivers/iio/adc/hx711.o" \
    "$work/output/arch/arm/boot/dts/rv1106g-echo-mate.dtb" > "$work/artifacts.sha256"
sha256sum -c "$work/sdk-inputs.sha256"
printf 'Compile probe completed. Nothing has been flashed or loaded. Results: %s\n' "$work"
