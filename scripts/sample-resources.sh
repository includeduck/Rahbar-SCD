#!/usr/bin/env bash
# Measure CPU and memory of every container in the namespace while something runs (Assignment
# 03, F5). Every INTERVAL seconds it appends `kubectl top pod --containers` (metrics-server:
# a rolling average of about a minute, working-set memory) to a TSV, until the named Job
# finishes or DURATION seconds pass. It also records each running container's cgroup
# memory.peak (the true high-water mark since the container started, page cache included),
# which catches the short spikes that a one-minute average smooths away. memory.peak is read
# every minute (a Job's pod cannot be read once it has finished); the summary keeps the max.
#
#   scripts/sample-resources.sh <label> [job-name] [duration-s] [interval-s]
#   python scripts/summarize_resources.py eval/results/part-f/top-<label>.tsv
#
# Needs metrics-server (minikube addons enable metrics-server).
set -euo pipefail

LABEL=${1:?label}
JOB=${2:-}
DURATION=${3:-3600}
INTERVAL=${4:-15}
NS=${NS:-rahbar}
OUT_DIR=${OUT_DIR:-eval/results/part-f}
TSV="$OUT_DIR/top-$LABEL.tsv"
PEAK="$OUT_DIR/peak-$LABEL.tsv"
mkdir -p "$OUT_DIR"
[ -s "$TSV" ] || printf 'time\tpod\tcontainer\tcpu_m\tmem_mi\n' > "$TSV"
[ -s "$PEAK" ] || printf 'time\tpod\tcontainer\tmemory_peak_mi\tcpu_seconds_total\n' > "$PEAK"

job_done() {
  [ -n "$JOB" ] && kubectl -n "$NS" get job "$JOB" -o jsonpath='{.status.conditions[*].type}' \
    2>/dev/null | grep -qE 'Complete|Failed'
}

record_peaks() {
  kubectl -n "$NS" get pods --field-selector=status.phase=Running \
    -o jsonpath='{range .items[*]}{.metadata.name}{" "}{range .spec.containers[*]}{.name}{" "}{end}{"\n"}{end}' |
  while read -r pod containers; do
    for c in $containers; do
      vals=$(kubectl -n "$NS" exec "$pod" -c "$c" -- sh -c \
        'cat /sys/fs/cgroup/memory.peak; grep usage_usec /sys/fs/cgroup/cpu.stat' 2>/dev/null \
        < /dev/null) || continue
      peak=$(echo "$vals" | head -1)
      usec=$(echo "$vals" | awk '/usage_usec/ {print $2}')
      printf '%s\t%s\t%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$pod" "$c" \
        "$((peak / 1048576))" "$((usec / 1000000))" >> "$PEAK"
    done
  done
}

end=$(( $(date +%s) + DURATION ))
n=0
while [ "$(date +%s)" -lt "$end" ]; do
  now=$(date -u +%FT%TZ)
  kubectl top pod -n "$NS" --containers --no-headers 2>/dev/null |
    awk -v t="$now" 'BEGIN{OFS="\t"} {cpu=$3; sub("m","",cpu); mem=$4; sub("Mi","",mem); print t,$1,$2,cpu,mem}' \
    >> "$TSV" || true
  if [ $((n % 4)) -eq 0 ]; then record_peaks; fi
  n=$((n + 1))
  if job_done; then
    echo "job $JOB finished"
    break
  fi
  sleep "$INTERVAL"
done
record_peaks
echo "samples: $TSV ($(($(wc -l < "$TSV") - 1)) rows), peaks: $PEAK"
