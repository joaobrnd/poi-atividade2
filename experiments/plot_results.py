"""Plot demand against latency, throughput, CPU and memory.

One figure is written per service. Each scenario in the CSVs becomes a line,
which is the comparison the report needs: same demand, different provisioning.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

DEMAND_LABELS = {
    "dataset_mb": "Tamanho do conjunto de trabalho (MiB)",
    "items": "Itens do catálogo",
    "size_mb": "Tamanho do documento (MiB)",
}

PANELS = (
    ("latency_median_ms", "latency_p95_ms", "Tempo de resposta (ms)", "mediana", "p95"),
    ("rps", None, "Vazão (req/s)", "req/s", None),
    ("system_cpu_percent_avg", None, "Utilização de CPU (%)", "CPU do sistema", None),
    ("rss_mb_avg", None, "Memória residente (MiB)", "RSS médio", None),
)


def load_rows(results_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(results_dir.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            rows.extend(csv.DictReader(handle))
    return rows


def _numbers(rows: list[dict[str, str]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        raw = row.get(key, "")
        values.append(float("nan") if raw == "" else float(raw))
    return values


def plot(results_dir: Path, output_dir: Path) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = load_rows(results_dir)
    if not rows:
        raise SystemExit(f"Nenhum CSV em {results_dir}. Rode experiments/run_load.py antes.")

    by_service: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_service[row["service"]].append(row)

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for service, service_rows in by_service.items():
        scenarios: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in service_rows:
            scenarios[row["scenario"]].append(row)

        demand_unit = service_rows[0].get("demand_unit", "demand")
        fig, axes = plt.subplots(2, 2, figsize=(11, 8))
        fig.suptitle(f"Serviço {service}: demanda, provisionamento e desempenho")

        for axis, (primary, secondary, title, primary_name, secondary_name) in zip(
            axes.ravel(), PANELS
        ):
            for scenario, scenario_rows in scenarios.items():
                ordered = sorted(scenario_rows, key=lambda item: float(item["demand"]))
                demand = [float(item["demand"]) for item in ordered]
                (line,) = axis.plot(
                    demand,
                    _numbers(ordered, primary),
                    marker="o",
                    label=scenario if secondary is None else f"{scenario} ({primary_name})",
                )
                if secondary is not None:
                    axis.plot(
                        demand,
                        _numbers(ordered, secondary),
                        marker="o",
                        linestyle="--",
                        color=line.get_color(),
                        label=f"{scenario} ({secondary_name})",
                    )
            axis.set_title(title)
            axis.set_xlabel(DEMAND_LABELS.get(demand_unit, demand_unit))
            axis.set_ylabel(title)
            axis.grid(True, alpha=0.3)
            axis.legend(fontsize=8)

        fig.tight_layout()
        destination = output_dir / f"{service}_resumo.png"
        fig.savefig(destination, dpi=140)
        plt.close(fig)
        written.append(destination)
    return written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gera os gráficos da coleta de carga.")
    parser.add_argument("--results-dir", type=Path, default=ROOT / "experiments" / "results")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "experiments" / "plots")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for path in plot(args.results_dir, args.output_dir):
        print(path)


if __name__ == "__main__":
    main()
