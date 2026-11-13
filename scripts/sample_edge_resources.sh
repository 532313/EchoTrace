#!/bin/sh
set -eu
process=$1
samples=$2
interval=$3
data=$4
gui=${5:-}
start_ticks=$(awk '{print $22}' "/proc/$process/stat")
count=0
while [ "$count" -lt "$samples" ] && [ -r "/proc/$process/status" ]; do
    [ "$(awk '{print $22}' "/proc/$process/stat")" = "$start_ticks" ] || exit 0
    read elapsed idle < /proc/uptime
    printf '%s\t' "$elapsed"
    awk '/MemAvailable:|CmaAllocated:|CmaReleased:|AnonPages:|Dirty:|Writeback:|SUnreclaim:|SReclaimable:|KernelStack:|PageTables:/ {printf "%s=%s ",$1,$2}' /proc/meminfo
    awk '/VmRSS:/ {printf "VmRSS_kB=%s ",$2}' "/proc/$process/status"
    awk '/RssAnon:|RssFile:|VmHWM:/ {printf "capture_%s_kB=%s ",substr($1,1,length($1)-1),$2}' "/proc/$process/status"
    if [ -n "$gui" ] && [ -r "/proc/$gui/status" ]; then
        awk '/VmRSS:|RssAnon:|RssFile:/ {printf "gui_%s_kB=%s ",substr($1,1,length($1)-1),$2}' "/proc/$gui/status"
    fi
    awk '$1=="TCP:" {for(i=2;i<NF;i++)if($i=="mem")printf "tcp_mem_pages=%s ",$(i+1)}' /proc/net/sockstat
    awk '$1=="pgmajfault" || $1=="allocstall_normal" || $1=="oom_kill" {printf "vm_%s=%s ",$1,$2}' /proc/vmstat
    df -k "$data" | awk 'END {printf "available_kB=%s",$4}'
    printf '\t'
    if [ -f "$data/state.json" ]; then cat "$data/state.json"; else printf '{}\n'; fi
    count=$((count+1))
    sleep "$interval"
done
