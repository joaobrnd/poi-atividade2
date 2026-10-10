"""IO-bound local document storage service.

The service writes and reads synthetic documents on the local filesystem. The
HTTP response contains metadata and a checksum rather than the document body,
so the experiment measures server-side disk work without turning the network
response itself into the dominant cost.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from time import perf_counter
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel


DEFAULT_SIZE_MB = int(os.getenv("IO_DEFAULT_SIZE_MB", "8"))
MAX_SIZE_MB = int(os.getenv("IO_MAX_SIZE_MB", "512"))
DATA_DIR = Path(os.getenv("IO_DATA_DIR", "/tmp/poi-io-service")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

CHUNK_SIZE = 1024 * 1024
DOCUMENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
PATTERN = hashlib.blake2b(
    b"poi-atividade2-synthetic-document", digest_size=64
).digest()


class DocumentResponse(BaseModel):
    id: str
    size_bytes: int
    checksum: str
    persisted: bool
    write_ms: float
    read_ms: float


app = FastAPI(
    title="IO-bound Document Service",
    version="1.0.0",
    description="Synthetic local document storage workload for provisioning experiments.",
)


def _path_for(document_id: str) -> Path:
    if not DOCUMENT_ID_PATTERN.fullmatch(document_id):
        raise HTTPException(status_code=400, detail="invalid document id")
    return DATA_DIR / f"{document_id}.bin"


def _write_synthetic(path: Path, size_bytes: int, sync: bool) -> float:
    started = perf_counter()
    remaining = size_bytes
    with path.open("wb") as output:
        while remaining:
            chunk_size = min(CHUNK_SIZE, remaining)
            repetitions = (chunk_size + len(PATTERN) - 1) // len(PATTERN)
            output.write((PATTERN * repetitions)[:chunk_size])
            remaining -= chunk_size
        if sync:
            output.flush()
            os.fsync(output.fileno())
    return (perf_counter() - started) * 1000


def _read_checksum(path: Path) -> tuple[str, int, float]:
    started = perf_counter()
    digest = hashlib.blake2b(digest_size=16)
    size_bytes = 0
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
            size_bytes += len(chunk)
    return digest.hexdigest(), size_bytes, (perf_counter() - started) * 1000


def _size_bytes(size_mb: int) -> int:
    return size_mb * 1024 * 1024


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "io-document"}


@app.get("/config")
def config() -> dict[str, Any]:
    return {
        "defaults": {"size_mb": DEFAULT_SIZE_MB},
        "limits": {"size_mb": {"min": 1, "max": MAX_SIZE_MB}},
        "data_dir": str(DATA_DIR),
    }


@app.post("/documents", response_model=DocumentResponse)
def create_document(
    size_mb: int = Query(default=DEFAULT_SIZE_MB, ge=1, le=MAX_SIZE_MB),
    sync: bool = Query(
        default=False,
        description="Call fsync after writing to force a storage flush.",
    ),
) -> DocumentResponse:
    document_id = uuid.uuid4().hex
    path = _path_for(document_id)
    write_ms = _write_synthetic(path, _size_bytes(size_mb), sync)
    checksum, size_bytes, read_ms = _read_checksum(path)
    return DocumentResponse(
        id=document_id,
        size_bytes=size_bytes,
        checksum=checksum,
        persisted=True,
        write_ms=round(write_ms, 3),
        read_ms=round(read_ms, 3),
    )


@app.get("/documents/{document_id}")
def read_document(document_id: str) -> dict[str, Any]:
    path = _path_for(document_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="document not found")
    checksum, size_bytes, read_ms = _read_checksum(path)
    return {
        "id": document_id,
        "size_bytes": size_bytes,
        "checksum": checksum,
        "read_ms": round(read_ms, 3),
    }


@app.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: str) -> Response:
    path = _path_for(document_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="document not found")
    path.unlink()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/documents/workload")
def transient_workload(
    size_mb: int = Query(default=DEFAULT_SIZE_MB, ge=1, le=MAX_SIZE_MB),
    repetitions: int = Query(default=1, ge=1, le=20),
    sync: bool = Query(default=False),
) -> dict[str, Any]:
    """Write, read and delete temporary documents for a repeatable IO test."""

    started = perf_counter()
    total_written = 0
    total_read = 0
    checksums: list[str] = []

    for _ in range(repetitions):
        document_id = uuid.uuid4().hex
        path = _path_for(document_id)
        try:
            total_written += _size_bytes(size_mb)
            _write_synthetic(path, _size_bytes(size_mb), sync)
            checksum, size_bytes, _ = _read_checksum(path)
            total_read += size_bytes
            checksums.append(checksum)
        finally:
            path.unlink(missing_ok=True)

    elapsed_ms = (perf_counter() - started) * 1000
    return {
        "workload": {
            "size_mb": size_mb,
            "repetitions": repetitions,
            "sync": sync,
            "bytes_written": total_written,
            "bytes_read": total_read,
        },
        "elapsed_ms": round(elapsed_ms, 3),
        "checksums": checksums,
        "temporary_files_removed": True,
    }


@app.get("/storage")
def storage_status() -> dict[str, Any]:
    files = list(DATA_DIR.glob("*.bin"))
    total_bytes = sum(path.stat().st_size for path in files)
    return {
        "data_dir": str(DATA_DIR),
        "documents": len(files),
        "total_bytes": total_bytes,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8003")),
    )

