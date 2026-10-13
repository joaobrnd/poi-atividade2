"""Linux resource samples used by the load runner.

The VM and the lab machines are Linux, so the counters come from /proc
instead of an extra dependency. CPU figures are interval rates; RSS is a
point sample summed across the service process and its children.
"""

from __future__ import annotations

import math
import os
from pathlib import Path


CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])


def percentile(values: list[float], pct: float) -> float:
    """Linear percentile. `pct` is in the range 0-100."""

    if not values:
        return math.nan
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def mean(values: list[float]) -> float:
    if not values:
        return math.nan
    return sum(values) / len(values)


def parse_cpu_ticks(stat_text: str) -> tuple[int, int]:
    """Return (utime, stime) from a /proc/<pid>/stat line.

    The command field is wrapped in parentheses and may contain spaces,
    so the fields after the last ')' are the stable ones.
    """

    after_comm = stat_text.rsplit(")", 1)[1].split()
    return int(after_comm[11]), int(after_comm[12])


def read_system_times() -> tuple[int, int, int]:
    """Return (total, not_executing, iowait) jiffies from the aggregate CPU line.

    `not_executing` is idle plus steal. Utilization is then
    total - not_executing - iowait, which counts only time spent on the CPU.
    """

    line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    parts = [int(value) for value in line.split()[1:9]]
    idle = parts[3]
    iowait = parts[4]
    steal = parts[7]
    return sum(parts), idle + steal, iowait


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def process_parents() -> dict[int, int]:
    """Map each visible pid to its parent."""

    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        text = _read_text(entry / "status")
        if text is None:
            continue
        for line in text.splitlines():
            if line.startswith("PPid:"):
                parents[int(entry.name)] = int(line.split()[1])
                break
    return parents


def _is_runtime_process(pid: int) -> bool:
    comm = _read_text(Path(f"/proc/{pid}/comm"))
    if comm is None:
        return False
    name = comm.strip().lower()
    return "python" in name or "uvicorn" in name


def service_pids(root_hint: int) -> list[int]:
    """Return the service process and its children.

    Uvicorn's master process may be the one that should be measured together
    with its workers. Climb from the socket owner to the nearest Python
    ancestor, then collect that subtree. A shell parent stops the climb.
    """

    parents = process_parents()
    root = root_hint
    seen = set()
    current = root_hint
    while current > 1 and current not in seen:
        seen.add(current)
        parent = parents.get(current)
        if parent is None or parent <= 1 or not _is_runtime_process(parent):
            break
        root = parent
        current = parent

    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        children.setdefault(parent, []).append(pid)

    ordered: list[int] = []
    stack = [root]
    visited: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in visited:
            continue
        visited.add(pid)
        ordered.append(pid)
        stack.extend(children.get(pid, []))
    return ordered


def process_tree_ticks(pids: list[int]) -> int:
    total = 0
    for pid in pids:
        text = _read_text(Path(f"/proc/{pid}/stat"))
        if text is None:
            continue
        utime, stime = parse_cpu_ticks(text)
        total += utime + stime
    return total


def process_tree_rss_kb(pids: list[int]) -> int:
    total = 0
    for pid in pids:
        text = _read_text(Path(f"/proc/{pid}/status"))
        if text is None:
            continue
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                total += int(line.split()[1])
                break
    return total


def listening_inodes(port: int) -> set[str]:
    """Socket inodes bound to `port` in the listening state."""

    hex_port = f"{port:04X}"
    inodes: set[str] = set()
    for proc_file in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        text = _read_text(proc_file)
        if text is None:
            continue
        for line in text.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            local_port = fields[1].rsplit(":", 1)[-1]
            if local_port.upper() == hex_port:
                inodes.add(fields[9])
    return inodes


def pid_for_inodes(inodes: set[str]) -> int | None:
    if not inodes:
        return None
    targets = {f"socket:[{inode}]" for inode in inodes}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target in targets:
                return int(entry.name)
    return None


def pid_listening_on(port: int) -> int | None:
    return pid_for_inodes(listening_inodes(port))
