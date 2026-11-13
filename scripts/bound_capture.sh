#!/bin/sh
set -eu
process=$1
start_ticks=$2
deadline=$3
executable=$4
log=$5
while [ -r "/proc/$process/stat" ]; do
    actual_ticks=$(awk '{print $22}' "/proc/$process/stat")
    [ "$actual_ticks" = "$start_ticks" ] || exit 0
    [ "$(readlink "/proc/$process/exe")" = "$executable" ] || exit 0
    read elapsed idle < /proc/uptime
    if [ "${elapsed%%.*}" -ge "$deadline" ]; then
        printf 'uptime=%s capture deadline reached; sending TERM to %s\n' "$elapsed" "$process" >> "$log"
        kill -TERM "$process"
        exit 0
    fi
    sleep 1
done
