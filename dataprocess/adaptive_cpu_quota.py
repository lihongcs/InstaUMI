#!/usr/bin/env python3
"""Use a target share of host CPU capacity available to one user cgroup.

The controller measures both host-wide busy time and the pipeline cgroup's
actual CPU time.  It therefore reacts to changing desktop/service load instead
of reserving a fixed number of CPUs.  Unrelated load is removed first, then the
pipeline receives the requested share of the remaining capacity.  Quota changes
are smoothed and bounded to avoid oscillation.  The original unit properties are
restored on exit.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path


UNIT_PATTERN = re.compile(r"^[A-Za-z0-9:_.@\-]+\.(?:scope|service)$")


@dataclass(frozen=True)
class HostSample:
    total_ticks: int
    idle_ticks: int
    cgroup_usage_usec: int
    monotonic: float


def read_pid_start_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    return int(fields[21])


def resolve_pid_cgroup(pid: int) -> tuple[str, Path]:
    lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
    unified = next((line.split(":", 2)[2] for line in lines if line.startswith("0::")), None)
    if not unified:
        raise RuntimeError(f"PID {pid} is not in a cgroup-v2 hierarchy")
    unit = Path(unified).name
    if not UNIT_PATTERN.fullmatch(unit):
        raise RuntimeError(f"cannot safely control non-systemd cgroup unit: {unit!r}")
    cgroup_dir = Path("/sys/fs/cgroup") / unified.lstrip("/")
    if not (cgroup_dir / "cpu.stat").is_file():
        raise RuntimeError(f"cgroup cpu.stat is unavailable: {cgroup_dir}")
    return unit, cgroup_dir


def read_host_ticks() -> tuple[int, int]:
    fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
    if not fields or fields[0] != "cpu" or len(fields) < 6:
        raise RuntimeError("unexpected /proc/stat CPU format")
    values = [int(value) for value in fields[1:9]]
    total = sum(values)
    idle = values[3] + values[4]
    return total, idle


def read_cgroup_usage_usec(cgroup_dir: Path) -> int:
    values = {}
    for line in (cgroup_dir / "cpu.stat").read_text(encoding="utf-8").splitlines():
        key, value = line.split(maxsplit=1)
        values[key] = int(value)
    if "usage_usec" not in values:
        raise RuntimeError(f"cgroup cpu.stat has no usage_usec: {cgroup_dir}")
    return values["usage_usec"]


def sample(cgroup_dir: Path) -> HostSample:
    total, idle = read_host_ticks()
    return HostSample(total, idle, read_cgroup_usage_usec(cgroup_dir), time.monotonic())


def utilization_between(before: HostSample, after: HostSample, cpu_count: int) -> tuple[float, float]:
    total_delta = after.total_ticks - before.total_ticks
    idle_delta = after.idle_ticks - before.idle_ticks
    elapsed = after.monotonic - before.monotonic
    usage_delta = after.cgroup_usage_usec - before.cgroup_usage_usec
    if total_delta <= 0 or elapsed <= 0.0 or usage_delta < 0:
        raise RuntimeError("non-monotonic CPU accounting sample")
    busy_fraction = 1.0 - max(0.0, min(1.0, idle_delta / total_delta))
    host_busy_cores = busy_fraction * cpu_count
    cgroup_busy_cores = max(0.0, min(float(cpu_count), usage_delta / (elapsed * 1e6)))
    return host_busy_cores, cgroup_busy_cores


def next_quota_cores(
    *,
    current_quota_cores: float,
    host_busy_cores: float,
    cgroup_busy_cores: float,
    capacity_cores: float,
    target_fraction: float,
    minimum_cores: float,
    maximum_cores: float,
    smoothing: float,
    max_step_cores: float,
) -> float:
    external_busy_cores = max(0.0, host_busy_cores - cgroup_busy_cores)
    available_cores = max(0.0, capacity_cores - external_busy_cores)
    raw = max(
        minimum_cores,
        min(maximum_cores, target_fraction * available_cores),
    )
    smoothed = current_quota_cores + smoothing * (raw - current_quota_cores)
    lower = current_quota_cores - max_step_cores
    upper = current_quota_cores + max_step_cores
    return max(minimum_cores, min(maximum_cores, max(lower, min(upper, smoothed))))


def systemctl_show(unit: str, prop: str) -> str:
    completed = subprocess.run(
        ["systemctl", "--user", "show", unit, f"--property={prop}", "--value"],
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def set_unit_properties(unit: str, quota_percent: int | None, weight: int) -> None:
    # systemctl resets a runtime property with an empty assignment.  Although
    # `systemctl show` renders the unlimited state as "infinity", passing that
    # word back to set-property is rejected by some Ubuntu systemd versions.
    quota = "" if quota_percent is None else f"{quota_percent}%"
    subprocess.run(
        [
            "systemctl",
            "--user",
            "set-property",
            unit,
            f"CPUQuota={quota}",
            f"CPUWeight={weight}",
        ],
        check=True,
        text=True,
        capture_output=True,
    )


def parse_existing_quota_cores(value: str, cpu_count: int) -> float:
    if value in {"", "infinity"}:
        return float(cpu_count)
    # systemd renders CPUQuotaPerSecUSec as a duration such as "23s".
    if value.endswith("ms"):
        return min(float(cpu_count), float(value[:-2]) / 1000.0)
    if value.endswith("s"):
        return min(float(cpu_count), float(value[:-1]))
    raise RuntimeError(f"unsupported CPUQuotaPerSecUSec value: {value!r}")


def run(args: argparse.Namespace) -> None:
    cpu_count = os.cpu_count() or 1
    if not 1.0 <= args.target_percent <= 100.0:
        raise ValueError("--target-percent must be in [1, 100]")
    if args.interval_sec < 1.0:
        raise ValueError("--interval-sec must be >= 1")
    if not 0.0 < args.smoothing <= 1.0:
        raise ValueError("--smoothing must be in (0, 1]")
    unit, cgroup_dir = resolve_pid_cgroup(args.pid)
    if args.unit is not None and args.unit != unit:
        raise RuntimeError(f"PID {args.pid} belongs to {unit}, not requested unit {args.unit}")
    start_ticks = read_pid_start_ticks(args.pid)
    original_quota = systemctl_show(unit, "CPUQuotaPerSecUSec")
    original_weight_text = systemctl_show(unit, "CPUWeight")
    original_weight = (
        int(original_weight_text)
        if original_weight_text.isdecimal()
        else 100
    )
    current_quota = parse_existing_quota_cores(original_quota, cpu_count)
    target_fraction = args.target_percent / 100.0
    target_cores = cpu_count * target_fraction
    maximum_cores = min(float(cpu_count), target_cores)
    minimum_cores = min(maximum_cores, max(1.0, args.minimum_cores))
    current_quota = max(minimum_cores, min(maximum_cores, current_quota))
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    # Apply an initial target-sized ceiling immediately.  Besides avoiding one
    # unbounded sampling interval, this is a synchronous permission/preflight
    # check so the wrapper can report an unavailable controller promptly.
    set_unit_properties(
        unit,
        max(1, int(round(current_quota * 100.0))),
        args.cpu_weight,
    )
    print(
        f"[cpu-adaptive] unit={unit} target_available={args.target_percent:.1f}% "
        f"cpus={cpu_count} interval_sec={args.interval_sec:g}",
        flush=True,
    )
    previous = sample(cgroup_dir)
    last_report = 0.0
    try:
        while not stop.wait(args.interval_sec):
            try:
                if read_pid_start_ticks(args.pid) != start_ticks:
                    break
            except (FileNotFoundError, ProcessLookupError):
                break
            current = sample(cgroup_dir)
            host_busy, group_busy = utilization_between(previous, current, cpu_count)
            previous = current
            desired = next_quota_cores(
                current_quota_cores=current_quota,
                host_busy_cores=host_busy,
                cgroup_busy_cores=group_busy,
                capacity_cores=float(cpu_count),
                target_fraction=target_fraction,
                minimum_cores=minimum_cores,
                maximum_cores=maximum_cores,
                smoothing=args.smoothing,
                max_step_cores=args.max_step_cores,
            )
            quota_percent = max(1, int(round(desired * 100.0)))
            if abs(desired - current_quota) >= args.change_threshold_cores:
                set_unit_properties(unit, quota_percent, args.cpu_weight)
                current_quota = desired
            now = time.monotonic()
            if now - last_report >= args.report_every_sec:
                external_busy = max(0.0, host_busy - group_busy)
                available = max(0.0, cpu_count - external_busy)
                print(
                    f"[cpu-adaptive] host={100.0 * host_busy / cpu_count:.1f}% "
                    f"pipeline_cores={group_busy:.2f} external_cores={external_busy:.2f} "
                    f"available_cores={available:.2f} quota_cores={current_quota:.2f}",
                    flush=True,
                )
                last_report = now
    finally:
        try:
            restore_quota = None if original_quota in {"", "infinity"} else int(
                round(parse_existing_quota_cores(original_quota, cpu_count) * 100.0)
            )
            set_unit_properties(unit, restore_quota, original_weight)
            print(f"[cpu-adaptive] restored unit={unit}", flush=True)
        except Exception as exc:
            print(f"[cpu-adaptive] warning: failed to restore {unit}: {exc}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True, help="PID whose systemd user scope is controlled")
    parser.add_argument("--unit", default=None, help="Optional expected unit name (fail closed on mismatch)")
    parser.add_argument(
        "--target-percent",
        type=float,
        default=95.0,
        help="Share of CPU capacity remaining after unrelated host load is deducted",
    )
    parser.add_argument("--interval-sec", type=float, default=5.0)
    parser.add_argument(
        "--minimum-cores",
        type=float,
        default=0.25,
        help="Minimum pipeline CPU allowance while unrelated host load is high",
    )
    parser.add_argument("--smoothing", type=float, default=0.5)
    parser.add_argument("--max-step-cores", type=float, default=2.0)
    parser.add_argument("--change-threshold-cores", type=float, default=0.10)
    parser.add_argument("--cpu-weight", type=int, default=50)
    parser.add_argument("--report-every-sec", type=float, default=30.0)
    args = parser.parse_args()
    try:
        run(args)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().replace("\n", " ")
        if len(detail) > 400:
            detail = detail[:397] + "..."
        print(
            "[cpu-adaptive:unavailable] systemd user scope cannot be adjusted; "
            "the data pipeline continues without a CPU ceiling"
            + (f"; detail={detail}" if detail else ""),
            flush=True,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        print(
            "[cpu-adaptive:unavailable] dynamic cgroup control is unavailable; "
            f"the data pipeline continues without a CPU ceiling; detail={exc}",
            flush=True,
        )


if __name__ == "__main__":
    main()
