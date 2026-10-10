from fastapi.testclient import TestClient

from services.cpu_service.app import app as cpu_app
from services.io_service.app import app as io_app
from services.memory_service.app import app as memory_app


def test_cpu_service_recommendation() -> None:
    response = TestClient(cpu_app).post(
        "/recommend",
        json={"items": 500, "dimensions": 8, "top_k": 3, "iterations": 1},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["workload"]["operations"] == 500 * 8
    assert len(body["recommendations"]) == 3


def test_memory_service_scan() -> None:
    client = TestClient(memory_app)
    response = client.get(
        "/cache/scan",
        params={"dataset_mb": 8, "passes": 1, "mode": "read"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["workload"]["bytes_touched"] == 8 * 1024 * 1024
    assert body["checksum"] > 0


def test_io_service_transient_workload() -> None:
    response = TestClient(io_app).post(
        "/documents/workload",
        params={"size_mb": 1, "repetitions": 1},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["workload"]["bytes_written"] == 1024 * 1024
    assert body["temporary_files_removed"] is True

