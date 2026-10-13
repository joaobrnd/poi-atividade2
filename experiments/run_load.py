"""Closed-loop load runner for one provisioning scenario.

The script does not change VirtualBox. CPU and RAM are whatever the machine
has at the moment; `--scenario` only labels the rows so several VM
configurations can be plotted together.

Each demand level is warmed up, then `concurrency` workers issue requests
for `duration` seconds. Latency is measured on the client. While that runs,
a sampler records system CPU, iowait and the resident memory of the service.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.metrics import (
    CLK_TCK,
    mean,
    percentile,
    pid_listening_on,
    process_tree_rss_kb,
    process_tree_ticks,
    read_system_times,
    service_pids,
)


PLAN_PATH = ROOT / "experiments" / "plan.json"
RESULTS_DIR = ROOT / "experiments" / "results"

SERVICES = {
    "memory": {
        "port": 8002,
        "health": "memory-catalog",
        "demand_unit": "dataset_mb",
    },
    "cpu": {
        "port": 8001,
        "health": "cpu-recommendation",
        "demand_unit": "items",
    },
    "io": {
        "port": 8003,
        "health": "io-document",
        "demand_unit": "size_mb",
    },
}

COLUMNS = [
    "started_at",
    "service",
    "scenario",
    "vcpus",
    "ram_gb",
    "demand",
    "demand_unit",
    "concurrency",
    "duration_s",
    "warmup",
    "requests_ok",
    "requests_error",
    "rps",
    "latency_min_ms",
    "latency_median_ms",
    "latency_mean_ms",
    "latency_p95_ms",
    "system_cpu_percent_avg",
    "system_iowait_percent_avg",
    "process_cpu_percent_avg",
    "rss_mb_avg",
    "rss_mb_max",
]


def load_plan(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def scenario_resources(plan: dict[str, Any], name: str) -> tuple[str, str]:
    for scenario in plan.get("scenarios", []):
        if scenario["name"] == name:
            return str(scenario["vcpus"]), str(scenario["ram_gb"])
    return "", ""


class ResourceMonitor:
    """Samples host CPU and the service RSS while a demand level runs."""

    def __init__(self, pid: int | None, interval: float = 0.5) -> None:
        self.pid = pid
        self.interval = interval
        self.system_cpu: list[float] = []
        self.iowait: list[float] = []
        self.process_cpu: list[float] = []
        self.rss_mb: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._system = read_system_times()
        self._ticks = self._process_ticks()
        self._clock = time.perf_counter()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> dict[str, float]:
        self._stop.set()
        self._thread.join(timeout=self.interval + 2)
        return {
            "system_cpu_percent_avg": mean(self.system_cpu),
            "system_iowait_percent_avg": mean(self.iowait),
            "process_cpu_percent_avg": mean(self.process_cpu),
            "rss_mb_avg": mean(self.rss_mb),
            "rss_mb_max": max(self.rss_mb) if self.rss_mb else math.nan,
        }

    def _process_ticks(self) -> int | None:
        if self.pid is None:
            return None
        pids = service_pids(self.pid)
        if not pids:
            return None
        return process_tree_ticks(pids)

    def _sample(self) -> None:
        now = time.perf_counter()
        system = read_system_times()
        wall = now - self._clock
        total_delta = system[0] - self._system[0]
        if wall > 0 and total_delta > 0:
            idle_delta = system[1] - self._system[1]
            iowait_delta = system[2] - self._system[2]
            busy = total_delta - idle_delta - iowait_delta
            self.system_cpu.append(100 * busy / total_delta)
            self.iowait.append(100 * iowait_delta / total_delta)

        ticks = self._process_ticks()
        if (
            ticks is not None
            and self._ticks is not None
            and wall > 0
            and ticks >= self._ticks
        ):
            cpu_seconds = (ticks - self._ticks) / CLK_TCK
            self.process_cpu.append(100 * cpu_seconds / wall)
            pids = service_pids(self.pid) if self.pid is not None else []
            rss_kb = process_tree_rss_kb(pids)
            if rss_kb > 0:
                self.rss_mb.append(rss_kb / 1024)

        self._system = system
        self._ticks = ticks
        self._clock = now

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self._sample()
        self._sample()


def build_call(
    service: str,
    base_url: str,
    demand: int,
    passes: int,
    dimensions: int,
    iterations: int,
    sync: bool,
) -> tuple[str, str, dict[str, Any] | None]:
    base = base_url.rstrip("/")
    if service == "memory":
        query = urllib.parse.urlencode(
            {
                "dataset_mb": demand,
                "passes": passes,
                "mode": "read",
                "seed": 42,
            }
        )
        return "GET", f"{base}/cache/scan?{query}", None
    if service == "cpu":
        return (
            "POST",
            f"{base}/recommend",
            {
                "items": demand,
                "dimensions": dimensions,
                "top_k": 10,
                "iterations": iterations,
                "seed": 42,
            },
        )
    if service == "io":
        query = urllib.parse.urlencode(
            {"size_mb": demand, "repetitions": passes, "sync": str(sync).lower()}
        )
        return "POST", f"{base}/documents/workload?{query}", None
    raise ValueError(f"unknown service: {service}")


def _fmt(value: float) -> str:
    if isinstance(value, float) and math.isnan(value):
        return ""
    return f"{value:.3f}"


def _port_of(base_url: str, service: str) -> int:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.port is not None:
        return parsed.port
    return int(SERVICES[service]["port"])


def _check_health(base_url: str, service: str, timeout: float) -> None:
    expected = SERVICES[service]["health"]
    url = base_url.rstrip("/") + "/health"
    try:
        response = httpx.get(url, timeout=timeout, trust_env=False)
    except httpx.HTTPError as exc:
        raise SystemExit(
            f"Não foi possível falar com {url}. Suba o serviço antes da carga. ({exc})"
        ) from exc
    if response.status_code != 200:
        raise SystemExit(f"{url} respondeu {response.status_code}: {response.text[:200]}")
    reported = response.json().get("service")
    if reported != expected:
        raise SystemExit(
            f"{url} é o serviço '{reported}', mas a carga foi pedida para '{service}'."
        )


def _warmup(
    client: httpx.Client,
    method: str,
    url: str,
    body: dict[str, Any] | None,
    repetitions: int,
) -> tuple[int, str]:
    failures = 0
    detail = ""
    for _ in range(repetitions):
        try:
            response = client.request(method, url, json=body)
        except httpx.HTTPError as exc:
            failures += 1
            detail = str(exc)
            continue
        if response.status_code != 200:
            failures += 1
            detail = f"{response.status_code} {response.text[:300]}"
    return failures, detail


def _run_closed_loop(
    method: str,
    url: str,
    body: dict[str, Any] | None,
    concurrency: int,
    duration: float,
    timeout: float,
) -> tuple[list[float], int, float]:
    latencies: list[float] = []
    errors = 0
    lock = threading.Lock()
    stop_at = time.perf_counter() + duration

    def worker() -> None:
        nonlocal errors
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            while time.perf_counter() < stop_at:
                started = time.perf_counter()
                try:
                    response = client.request(method, url, json=body)
                except httpx.HTTPError:
                    with lock:
                        errors += 1
                    continue
                elapsed_ms = (time.perf_counter() - started) * 1000
                with lock:
                    if response.status_code == 200:
                        latencies.append(elapsed_ms)
                    else:
                        errors += 1

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    opened = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return latencies, errors, time.perf_counter() - opened


def _row(
    *,
    service: str,
    scenario: str,
    vcpus: str,
    ram_gb: str,
    demand: int,
    concurrency: int,
    duration: float,
    warmup: int,
    latencies: list[float],
    errors: int,
    elapsed: float,
    samples: dict[str, float],
) -> dict[str, str]:
    ok = len(latencies)
    rps = ok / elapsed if elapsed > 0 else 0.0
    return {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "service": service,
        "scenario": scenario,
        "vcpus": vcpus,
        "ram_gb": ram_gb,
        "demand": str(demand),
        "demand_unit": str(SERVICES[service]["demand_unit"]),
        "concurrency": str(concurrency),
        "duration_s": f"{duration:.3f}",
        "warmup": str(warmup),
        "requests_ok": str(ok),
        "requests_error": str(errors),
        "rps": _fmt(rps),
        "latency_min_ms": _fmt(min(latencies) if latencies else math.nan),
        "latency_median_ms": _fmt(percentile(latencies, 50)),
        "latency_mean_ms": _fmt(mean(latencies)),
        "latency_p95_ms": _fmt(percentile(latencies, 95)),
        "system_cpu_percent_avg": _fmt(samples["system_cpu_percent_avg"]),
        "system_iowait_percent_avg": _fmt(samples["system_iowait_percent_avg"]),
        "process_cpu_percent_avg": _fmt(samples["process_cpu_percent_avg"]),
        "rss_mb_avg": _fmt(samples["rss_mb_avg"]),
        "rss_mb_max": _fmt(samples["rss_mb_max"]),
    }


def _print_row(row: dict[str, str]) -> None:
    print(
        "  demanda={demand}{unit}  ok={ok}  erros={errors}  "
        "rps={rps}  mediana={median} ms  p95={p95} ms  "
        "cpu={cpu}%  rss={rss} MiB".format(
            demand=row["demand"],
            unit=row["demand_unit"],
            ok=row["requests_ok"],
            errors=row["requests_error"],
            rps=row["rps"],
            median=row["latency_median_ms"] or "-",
            p95=row["latency_p95_ms"] or "-",
            cpu=row["system_cpu_percent_avg"] or "-",
            rss=row["rss_mb_avg"] or "-",
        )
    )


def run(args: argparse.Namespace) -> Path:
    plan = load_plan(args.plan)
    service = args.service or plan["service"]
    if service not in SERVICES:
        raise SystemExit(f"Serviço desconhecido: {service}. Use memory, cpu ou io.")

    base_url = args.base_url or plan.get("base_url") or f"http://127.0.0.1:{SERVICES[service]['port']}"
    demands = args.demands or [int(value) for value in plan["demands"]]
    concurrency = args.concurrency if args.concurrency is not None else int(plan["concurrency"])
    duration = args.duration if args.duration is not None else float(plan["duration_s"])
    warmup = args.warmup if args.warmup is not None else int(plan["warmup"])
    passes = args.passes if args.passes is not None else int(plan["passes"])
    timeout = args.timeout if args.timeout is not None else float(plan["timeout_s"])
    scenario = args.scenario
    vcpus, ram_gb = scenario_resources(plan, scenario)
    known = [item["name"] for item in plan.get("scenarios", [])]
    if scenario not in known:
        print(
            f"Aviso: '{scenario}' não é um dos cenários do plano ({', '.join(known)}). "
            "A coleta segue, mas esses números não entram como provisionamento da VM."
        )

    _check_health(base_url, service, timeout=5)
    pid = args.pid if args.pid is not None else pid_listening_on(_port_of(base_url, service))
    if pid is None:
        print("Aviso: não encontrei o processo na porta. Latência e vazão serão gravadas; CPU e RSS ficarão vazios.")
    else:
        print(f"Processo do serviço: pid {pid}")

    print(
        f"Serviço {service} | cenário {scenario} | demandas {demands} | "
        f"concorrência {concurrency} | {duration:.0f}s por nível"
    )

    results_dir = args.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)
    output = results_dir / f"{service}_{scenario}.csv"
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()

        for index, demand in enumerate(demands):
            method, url, body = build_call(
                service=service,
                base_url=base_url,
                demand=demand,
                passes=passes,
                dimensions=args.dimensions,
                iterations=args.iterations,
                sync=not args.no_sync,
            )
            print(f"Aquecimento da demanda {demand} ({warmup} requisições)...")
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                warmup_errors, detail = _warmup(client, method, url, body, warmup)
            if warmup_errors:
                print(f"  aquecimento falhou: {detail}")
                empty = {
                    "system_cpu_percent_avg": math.nan,
                    "system_iowait_percent_avg": math.nan,
                    "process_cpu_percent_avg": math.nan,
                    "rss_mb_avg": math.nan,
                    "rss_mb_max": math.nan,
                }
                row = _row(
                    service=service,
                    scenario=scenario,
                    vcpus=vcpus,
                    ram_gb=ram_gb,
                    demand=demand,
                    concurrency=concurrency,
                    duration=duration,
                    warmup=warmup,
                    latencies=[],
                    errors=warmup_errors,
                    elapsed=duration,
                    samples=empty,
                )
                writer.writerow(row)
                handle.flush()
                _print_row(row)
                continue

            monitor = ResourceMonitor(pid)
            monitor.start()
            latencies, errors, elapsed = _run_closed_loop(
                method, url, body, concurrency, duration, timeout
            )
            samples = monitor.stop()
            row = _row(
                service=service,
                scenario=scenario,
                vcpus=vcpus,
                ram_gb=ram_gb,
                demand=demand,
                concurrency=concurrency,
                duration=elapsed,
                warmup=warmup,
                latencies=latencies,
                errors=errors,
                elapsed=elapsed,
                samples=samples,
            )
            writer.writerow(row)
            handle.flush()
            _print_row(row)
            if index < len(demands) - 1:
                time.sleep(1)

    print(f"Resultados em {output}")
    return output


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Gera carga em um serviço e grava latência, vazão, CPU e memória."
    )
    parser.add_argument("--plan", type=Path, default=PLAN_PATH)
    parser.add_argument("--service", choices=sorted(SERVICES), help="Padrão: o serviço do plano (memory).")
    parser.add_argument(
        "--scenario",
        default="local",
        help="Rótulo do provisionamento atual. Na VM use um nome do plano, por exemplo 1vcpu-1gb.",
    )
    parser.add_argument("--base-url", help="Padrão: a URL do plano, ou a porta do serviço.")
    parser.add_argument(
        "--demands",
        type=lambda raw: [int(item) for item in raw.split(",") if item],
        help="Níveis de demanda separados por vírgula. Padrão: a lista do plano.",
    )
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--duration", type=float, help="Segundos de carga medida por nível, depois do aquecimento.")
    parser.add_argument("--warmup", type=int, help="Requisições de aquecimento por nível, fora da medição.")
    parser.add_argument(
        "--passes",
        type=int,
        help="Repetições internas de cada requisição (passes no serviço de memória, repetitions no de IO).",
    )
    parser.add_argument("--timeout", type=float, help="Timeout de uma requisição, em segundos.")
    parser.add_argument("--dimensions", type=int, default=32, help="Dimensões dos vetores no serviço de CPU.")
    parser.add_argument("--iterations", type=int, default=1, help="Iterações de scoring no serviço de CPU.")
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="No serviço de IO, não chama fsync. A coleta padrão força sync para encostar no disco.",
    )
    parser.add_argument("--pid", type=int, help="PID do serviço. Sem isso, a porta é usada para localizar o processo.")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    run(parse_args(argv))


if __name__ == "__main__":
    main()
