import math
import os

from experiments.metrics import (
    mean,
    parse_cpu_ticks,
    percentile,
    pid_listening_on,
    process_tree_rss_kb,
    read_system_times,
    service_pids,
)
from experiments.run_load import build_call


def test_percentile_interpolates_even_sized_samples() -> None:
    assert percentile([10, 20, 30, 40], 50) == 25
    assert percentile([10, 20, 30, 40], 95) == 38.5
    assert math.isnan(percentile([], 50))
    assert mean([10, 20]) == 15


def test_parse_cpu_ticks_ignores_spaces_inside_comm() -> None:
    stat = "12 (python worker) S 1 12 12 0 0 0 0 0 0 0 10 15 20 25"
    assert parse_cpu_ticks(stat) == (10, 15)


def test_proc_samples_see_this_process() -> None:
    total, idle, _iowait = read_system_times()
    assert total > idle >= 0
    rss = process_tree_rss_kb(service_pids(os.getpid()))
    assert rss > 0


def test_build_call_keeps_memory_reads_deterministic() -> None:
    method, url, body = build_call(
        service="memory",
        base_url="http://127.0.0.1:8002/",
        demand=256,
        passes=2,
        dimensions=32,
        iterations=1,
        sync=True,
    )
    assert method == "GET"
    assert body is None
    assert url == "http://127.0.0.1:8002/cache/scan?dataset_mb=256&passes=2&mode=read&seed=42"


def test_build_call_posts_cpu_workload() -> None:
    method, url, body = build_call(
        service="cpu",
        base_url="http://127.0.0.1:8001",
        demand=5000,
        passes=1,
        dimensions=32,
        iterations=2,
        sync=True,
    )
    assert method == "POST"
    assert url == "http://127.0.0.1:8001/recommend"
    assert body == {
        "items": 5000,
        "dimensions": 32,
        "top_k": 10,
        "iterations": 2,
        "seed": 42,
    }


def test_pid_lookup_returns_none_for_a_closed_port() -> None:
    assert pid_listening_on(59999) is None
