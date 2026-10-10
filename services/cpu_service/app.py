"""CPU-bound product recommendation service.

The endpoint deliberately performs the scoring loop in Python instead of
delegating the whole operation to a vectorized NumPy call. This makes the
computational demand visible in a small VM and keeps the workload tunable by
the number of catalog items, vector dimensions and scoring iterations.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from heapq import nlargest
from threading import Lock
from time import perf_counter
from typing import Any

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel, Field


DEFAULT_ITEMS = int(os.getenv("CPU_DEFAULT_ITEMS", "5000"))
DEFAULT_DIMENSIONS = int(os.getenv("CPU_DEFAULT_DIMENSIONS", "32"))
MAX_ITEMS = int(os.getenv("CPU_MAX_ITEMS", "100000"))
MAX_DIMENSIONS = int(os.getenv("CPU_MAX_DIMENSIONS", "256"))
MAX_CACHED_DATASETS = int(os.getenv("CPU_MAX_CACHED_DATASETS", "2"))


class RecommendRequest(BaseModel):
    """Controls the amount of work performed by one recommendation request."""

    items: int = Field(
        default=DEFAULT_ITEMS,
        ge=500,
        le=MAX_ITEMS,
        description="Number of synthetic products in the catalog.",
    )
    dimensions: int = Field(
        default=DEFAULT_DIMENSIONS,
        ge=8,
        le=MAX_DIMENSIONS,
        description="Number of features used by each product vector.",
    )
    top_k: int = Field(default=10, ge=1, le=100)
    iterations: int = Field(
        default=1,
        ge=1,
        le=20,
        description="Repeats the scoring workload without rebuilding the catalog.",
    )
    seed: int = Field(default=42, ge=0, le=2**31 - 1)


class CatalogCache:
    """Small bounded cache so dataset generation does not pollute measurements.

    The first request for a configuration pays the synthetic data generation
    cost. Repeated experiment requests with the same configuration reuse the
    catalog and measure the recommendation workload itself. The cache is
    bounded because the endpoint intentionally accepts large datasets.
    """

    def __init__(self, max_entries: int) -> None:
        self._max_entries = max(1, max_entries)
        self._entries: OrderedDict[tuple[int, int, int], np.ndarray] = OrderedDict()
        self._lock = Lock()

    def get_or_create(
        self, items: int, dimensions: int, seed: int
    ) -> tuple[np.ndarray, bool, float]:
        key = (items, dimensions, seed)
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return cached, True, 0.0

            started = perf_counter()
            rng = np.random.default_rng(seed)
            catalog = rng.random((items, dimensions), dtype=np.float32)
            generation_ms = (perf_counter() - started) * 1000

            self._entries[key] = catalog
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return catalog, False, generation_ms

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "cached_datasets": len(self._entries),
                "max_cached_datasets": self._max_entries,
                "configurations": [
                    {
                        "items": key[0],
                        "dimensions": key[1],
                        "seed": key[2],
                    }
                    for key in self._entries
                ],
            }


cache = CatalogCache(MAX_CACHED_DATASETS)
app = FastAPI(
    title="CPU-bound Recommendation Service",
    version="1.0.0",
    description="Synthetic product recommendation workload for provisioning experiments.",
)


def _score_catalog(
    catalog: np.ndarray,
    query_vector: np.ndarray,
    iterations: int,
    top_k: int,
) -> list[tuple[float, int]]:
    """Score every product with an intentionally explicit CPU workload."""

    scores: list[tuple[float, int]] = []
    item_count, dimensions = catalog.shape

    for _ in range(iterations):
        current_scores: list[tuple[float, int]] = []
        for item_id in range(item_count):
            product = catalog[item_id]
            score = 0.0
            for component in range(dimensions):
                score += float(product[component]) * float(query_vector[component])
            current_scores.append((score, item_id))
        scores = current_scores

    return nlargest(top_k, scores, key=lambda item: item[0])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "cpu-recommendation"}


@app.get("/config")
def config() -> dict[str, Any]:
    return {
        "defaults": {
            "items": DEFAULT_ITEMS,
            "dimensions": DEFAULT_DIMENSIONS,
        },
        "limits": {
            "items": {"min": 500, "max": MAX_ITEMS},
            "dimensions": {"min": 8, "max": MAX_DIMENSIONS},
        },
        "cache": cache.status(),
    }


@app.post("/recommend")
def recommend(payload: RecommendRequest) -> dict[str, Any]:
    catalog, cache_hit, generation_ms = cache.get_or_create(
        payload.items, payload.dimensions, payload.seed
    )

    query_rng = np.random.default_rng(payload.seed + 1)
    query_vector = query_rng.random(payload.dimensions, dtype=np.float32)
    query_norm = float(np.linalg.norm(query_vector))
    query_vector /= query_norm

    started = perf_counter()
    top_products = _score_catalog(
        catalog=catalog,
        query_vector=query_vector,
        iterations=payload.iterations,
        top_k=payload.top_k,
    )
    compute_ms = (perf_counter() - started) * 1000

    return {
        "workload": {
            "items": payload.items,
            "dimensions": payload.dimensions,
            "iterations": payload.iterations,
            "operations": payload.items * payload.dimensions * payload.iterations,
        },
        "timing_ms": {
            "catalog_generation": round(generation_ms, 3),
            "scoring": round(compute_ms, 3),
        },
        "dataset_cache_hit": cache_hit,
        "recommendations": [
            {"product_id": item_id, "score": round(score, 8)}
            for score, item_id in top_products
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8001")),
    )

