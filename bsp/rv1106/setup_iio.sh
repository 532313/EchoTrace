#!/bin/sh
set -eu
# HX711 RATE is strapped LOW for 10 SPS. Configure only a verified HX711 device.
base=/sys/bus/iio/devices
device=
for path in "$base"/iio:device*; do
    [ -r "$path/name" ] || continue
    [ "$(cat "$path/name")" = hx711 ] && device=$path
done
[ -n "$device" ] || { echo 'HX711 not bound; keep tray events disabled' >&2; exit 1; }
[ -d /sys/kernel/config/iio ] || { echo 'Enable CONFIG_IIO_CONFIGFS and mount configfs first' >&2; exit 1; }
trigger=/sys/kernel/config/iio/triggers/hrtimer/echotrace-5hz
mkdir -p "$trigger"
found=
for path in "$base"/trigger*; do
    [ -r "$path/name" ] || continue
    [ "$(cat "$path/name")" = echotrace-5hz ] && found=$path
done
[ -n "$found" ] || exit 1
printf 5 > "$found/sampling_frequency"
printf 0 > "$device/buffer/enable"
for path in "$device"/scan_elements/*_en; do printf 0 > "$path"; done
printf 1 > "$device/scan_elements/in_voltage0_en"
printf 1 > "$device/scan_elements/in_timestamp_en"
# The native reader requires voltage0 u24/storage32 at byte0, padding4, timestamp s64 at byte8.
[ "$(cat "$device/scan_elements/in_voltage0_index")" = 0 ] || exit 1
[ "$(cat "$device/scan_elements/in_timestamp_index")" = 2 ] || exit 1
grep -Eq 'u24/32>>0' "$device/scan_elements/in_voltage0_type" || exit 1
grep -Eq 's64/64>>0' "$device/scan_elements/in_timestamp_type" || exit 1
printf echotrace-5hz > "$device/trigger/current_trigger"
printf 16 > "$device/buffer/length"
printf 1 > "$device/buffer/enable"
printf '%s\n' "/dev/$(basename "$device")"
