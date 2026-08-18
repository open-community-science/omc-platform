#!/usr/bin/env bash
# Watch the space the pipeline results land in, and say so before it runs out.
#
# arbutus keeps every run's published results under /data/results — the clusters
# hold the raw reads, arbutus holds what comes back — so /data is what fills as
# runs are queued, at roughly 2.4 GB per run. There is no mail on this host, so
# this records state rather than sending it: a log to read and a JSON file
# anything can poll.
set -uo pipefail

LOG=/data/logs/disk-monitor.log
STATUS=/data/logs/disk-status.json
WARN=80      # percent used on /data
CRIT=90
ROOT_WARN=85 # percent used on /, which is small and separate

mkdir -p "$(dirname "$LOG")"

read -r _ size used avail pct _ < <(df -P /data | tail -1)
pct=${pct%\%}
read -r _ rsize rused ravail rpct _ < <(df -P / | tail -1)
rpct=${rpct%\%}

now=$(date -Iseconds)
avail_g=$(( avail / 1024 / 1024 ))
# Results have averaged ~2.4 GB per completed run; the estimate is a guide to how
# many more will fit, not a promise.
runs_left=$(( avail_g * 10 / 24 ))

level=ok
[ "$pct" -ge "$WARN" ] && level=warn
[ "$pct" -ge "$CRIT" ] && level=critical
[ "$rpct" -ge "$ROOT_WARN" ] && level="${level/ok/warn}"

printf '{"checked":"%s","data_pct":%s,"data_avail_gb":%s,"root_pct":%s,"est_runs_left":%s,"level":"%s"}\n' \
    "$now" "$pct" "$avail_g" "$rpct" "$runs_left" "$level" > "$STATUS"

line="$now /data ${pct}% used, ${avail_g}G free (~${runs_left} runs) · / ${rpct}% used"
if [ "$level" = ok ]; then
    echo "$line" >> "$LOG"
else
    echo "ALERT[$level] $line" >> "$LOG"
    # Biggest consumers, so the alert says what to look at rather than only that
    # something is wrong.
    du -sh /data/* 2>/dev/null | sort -rh | head -5 | sed "s/^/    /" >> "$LOG"
fi

# Keep the log from becoming the problem it is watching for.
if [ "$(wc -l < "$LOG")" -gt 20000 ]; then
    tail -5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
