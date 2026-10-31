"""Phase 0: health, index, and local web security."""
import pytest
from fastapi.testclient import TestClient

from app.main import app

GOOD_HEADERS = {"X-Forge-Request": "1"}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8000")


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_index_is_not_cached(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert "FORGE" in resp.text


def test_localhost_host_allowed() -> None:
    resp = TestClient(app, base_url="http://localhost:8000").get("/api/health")
    assert resp.status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "192.168.1.5:8000", "127.0.0.1:9000", "localhost"])
def test_bad_host_rejected(client: TestClient, host: str) -> None:
    resp = client.get("/api/health", headers={"Host": host})
    assert resp.status_code == 400


def test_post_without_custom_header_rejected(client: TestClient) -> None:
    assert client.post("/api/health").status_code == 403


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://127.0.0.1:9000"])
def test_post_with_foreign_origin_rejected(client: TestClient, origin: str) -> None:
    resp = client.post("/api/health", headers={**GOOD_HEADERS, "Origin": origin})
    assert resp.status_code == 403


def test_post_with_header_and_local_origin_passes_middleware(client: TestClient) -> None:
    resp = client.post("/api/health", headers={**GOOD_HEADERS, "Origin": "http://localhost:8000"})
    assert resp.status_code == 405  # no POST route exists; the middleware let it through


def test_no_cors_headers(client: TestClient) -> None:
    resp = client.options(
        "/api/health",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert not any(name.lower().startswith("access-control-") for name in resp.headers)


def test_api_docs_disabled(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
