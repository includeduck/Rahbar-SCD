"""Summarise scripts/sample-resources.sh output per workload (Assignment 03, F5).

    python scripts/summarize_resources.py eval/results/part-f/top-suite.tsv [more.tsv ...]

For each workload (pod name without its replica hash) and container: the number of samples,
CPU p50 / p95 / max in millicores and working-set memory p50 / max in MiB (kubectl top, a
rolling average of about a minute), and the largest cgroup memory.peak seen (the true
high-water mark, page cache included) from the matching peak-*.tsv. Standard library only.
"""

from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

HASHED = re.compile(r"^(?P<w>.+?)(-[a-z0-9]{8,10})?-[a-z0-9]{5}$")


def workload(pod: str) -> str:
    if re.match(r".+-\d+$", pod):                      # StatefulSet pod: chroma-0
        return pod.rsplit("-", 1)[0]
    m = HASHED.match(pod)
    return m.group("w") if m else pod


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def main(paths: list[str]) -> int:
    cpu: dict[tuple[str, str], list[float]] = defaultdict(list)
    mem: dict[tuple[str, str], list[float]] = defaultdict(list)
    peak: dict[tuple[str, str], float] = {}
    for p in map(Path, paths):
        for row in read(p):
            key = (workload(row["pod"]), row["container"])
            cpu[key].append(float(row["cpu_m"]))
            mem[key].append(float(row["mem_mi"]))
        for row in read(p.with_name(p.name.replace("top-", "peak-", 1))):
            key = (workload(row["pod"]), row["container"])
            peak[key] = max(peak.get(key, 0.0), float(row["memory_peak_mi"]))
    print("| workload | container | samples | CPU p50 / p95 / max (m) | memory p50 / max (Mi) "
          "| memory.peak (Mi) |")
    print("|---|---|---|---|---|---|")
    for key in sorted(set(cpu) | set(peak)):
        c, m = cpu.get(key, []), mem.get(key, [])
        cpu_s = f"{pct(c, 50):.0f} / {pct(c, 95):.0f} / {max(c):.0f}" if c else "-"
        mem_s = f"{pct(m, 50):.0f} / {max(m):.0f}" if m else "-"
        pk = f"{peak[key]:.0f}" if key in peak else "-"
        print(f"| {key[0]} | {key[1]} | {len(c)} | {cpu_s} | {mem_s} | {pk} |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
