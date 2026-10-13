"""Memory-bound synthetic catalog/cache service.

The service owns one resident NumPy byte buffer. A request can replace the
buffer with a configured size and then scan it one or more times. NumPy keeps
the scan in native code, making the dominant cost memory traversal rather
than Python object overhead.
"""

from __future__ import annotations

import gc
import os
from threading import Lock
from time import perf_counter
from typing import Any, Literal

import numpy as np
from fastapi import FastAPI, HTTPException, Query


DEFAULT_DATASET_MB = int(os.getenv("MEMORY_DEFAULT_MB", "64"))
# High enough for a working set to exceed the RAM of the smaller VM scenarios.
MAX_DATASET_MB = int(os.getenv("MEMORY_MAX_MB", "2048"))
MIN_DATASET_MB = 8


class MemoryDatasetManager:
    """Maintains a single bounded resident working set."""

    def __init__(self) -> None:
        self._data: np.ndarray | None = None
        self._size_mb: int | None = None
        self._seed: int | None = None
        self._lock = Lock()

    def load(self, size_mb: int, seed: int) -> tuple[np.ndarray, bool, float]:
        with self._lock:
            if (
                self._data is not None
                and self._size_mb == size_mb
                and self._seed == seed
            ):
                return self._data, True, 0.0

            # Release the old working set before allocating the new one. This
            # avoids briefly requiring the old and new datasets simultaneously.
            self._data = None
            gc.collect()

            started = perf_counter()
            data = np.empty(size_mb * 1024 * 1024, dtype=np.uint8)
            # fill() commits all pages and creates deterministic synthetic data.
            data.fill(seed % 251)
            load_ms = (perf_counter() - started) * 1000

            self._data = data
            self._size_mb = size_mb
            self._seed = seed
            return data, False, load_ms

    def reset(self) -> None:
        with self._lock:
            self._data = None
            self._size_mb = None
            self._seed = None
            gc.collect()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "resident_dataset_mb": self._size_mb,
                "seed": self._seed,
                "allocated": self._data is not None,
            }


manager = MemoryDatasetManager()
app = FastAPI(
    title="Memory-bound Catalog Service",
    version="1.0.0",
    description="Synthetic in-memory working set for provisioning experiments.",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "memory-catalog"}


@app.get("/config")
def config() -> dict[str, Any]:
    return {
        "defaults": {"dataset_mb": DEFAULT_DATASET_MB},
        "limits": {"dataset_mb": {"min": MIN_DATASET_MB, "max": MAX_DATASET_MB}},
        "cache": manager.status(),
    }


@app.get("/cache/status")
def cache_status() -> dict[str, Any]:
    return manager.status()


@app.post("/cache/reset")
def reset_cache() -> dict[str, str]:
    manager.reset()
    return {"status": "reset"}


@app.get("/cache/scan")
def scan_cache(
    dataset_mb: int = Query(
        default=DEFAULT_DATASET_MB,
        ge=MIN_DATASET_MB,
        le=MAX_DATASET_MB,
        description="Resident working-set size in MiB.",
    ),
    passes: int = Query(default=3, ge=1, le=50),
    mode: Literal["read", "read_write"] = Query(default="read"),
    seed: int = Query(default=42, ge=0, le=2**31 - 1),
) -> dict[str, Any]:
    try:
        data, cache_hit, load_ms = manager.load(dataset_mb, seed)
    except MemoryError:
        raise HTTPException(
            status_code=503,
            detail="not enough memory to allocate the requested dataset",
        ) from None

    started = perf_counter()
    checksum = 0
    for pass_number in range(passes):
        if mode == "read_write":
            # The in-place operation touches every byte while keeping the
            # working set resident. The final sum gives a cheap correctness
            # signal without returning the dataset over HTTP.
            np.bitwise_xor(data, np.uint8((seed + pass_number) % 256), out=data)
        checksum = int(np.sum(data, dtype=np.uint64))
    scan_ms = (perf_counter() - started) * 1000
    scans_per_pass = 2 if mode == "read_write" else 1

    return {
        "workload": {
            "dataset_mb": dataset_mb,
            "passes": passes,
            "mode": mode,
            "bytes_touched": dataset_mb * 1024 * 1024 * passes * scans_per_pass,
        },
        "timing_ms": {
            "dataset_load": round(load_ms, 3),
            "scan": round(scan_ms, 3),
        },
        "dataset_cache_hit": cache_hit,
        "checksum": checksum,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8002")),
    )
