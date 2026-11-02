"""Tests del contrato HTTP y de sus límites de seguridad."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from shared.models import InventoryReport

MAX_BODY_BYTES = 4 * 1024 * 1024


@pytest.fixture
def payload():
    return {
        "server_ip": "127.0.0.1",
        "hostname": "test-host",
        "collected_at": "2026-11-01T12:00:00+00:00",
        "operating_system": {"name": "Windows", "version": "test-version"},
        "cpu": {
            "architecture": "AMD64",
            "model": "Test CPU",
            "physical_cores": 2,
            "logical_cores": 4,
            "frequency_mhz": 2400.0,
        },
        "processes": [{"pid": 12, "name": "test-process"}],
        "sessions": [
            {
                "username": "test-user",
                "terminal": None,
                "started_at": "2026-11-01T10:00:00+00:00",
            }
        ],
        "warnings": [],
    }


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("INVENTORY_DATA_DIR", str(tmp_path))
    with TestClient(create_app(), raise_server_exceptions=False) as http_client:
        yield http_client


def test_health_is_public_and_contains_no_configuration(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_report_is_saved_and_can_be_queried(client, payload, tmp_path):
    response = client.post("/reports", json=payload)
    assert response.status_code == 200
    receipt = response.json()
    received_at = datetime.fromisoformat(receipt["received_at"].replace("Z", "+00:00"))
    assert received_at.utcoffset() is not None
    assert receipt["server_ip"] == payload["server_ip"]
    assert receipt["filename"] == f"127.0.0.1_{received_at.date().isoformat()}.json"
    assert (tmp_path / receipt["filename"]).is_file()

    response = client.get("/reports", params={"ip": "127.0.0.1"})
    assert response.status_code == 200
    report = response.json()
    expected = InventoryReport.model_validate(payload).model_dump(mode="json")
    for field in (
        "server_ip",
        "hostname",
        "operating_system",
        "cpu",
        "processes",
        "sessions",
        "warnings",
    ):
        assert report[field] == expected[field]
    assert report["received_at"] == receipt["received_at"]


def test_unknown_ip_returns_404(client):
    response = client.get("/reports", params={"ip": "192.0.2.55"})
    assert response.status_code == 404


@pytest.mark.parametrize("ip", ["../private", "192.0.2.1/../../private", "not-an-ip", "::1"])
def test_query_rejects_invalid_or_unsupported_ip(client, ip, tmp_path):
    response = client.get("/reports", params={"ip": ip})
    assert response.status_code == 422
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("ip", ["../../private", "256.0.0.1", "::1", 2130706433])
def test_report_rejects_invalid_ip_before_writing(client, payload, ip, tmp_path):
    payload["server_ip"] = ip
    response = client.post("/reports", json=payload)
    assert response.status_code == 422
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("location", ["report", "operating_system", "cpu", "process", "session"])
def test_unknown_fields_are_rejected_without_echoing_input(client, payload, location):
    marker = "SENSITIVE_VALUE_MUST_NOT_BE_REFLECTED"
    target = {
        "report": payload,
        "operating_system": payload["operating_system"],
        "cpu": payload["cpu"],
        "process": payload["processes"][0],
        "session": payload["sessions"][0],
    }[location]
    target["unexpected"] = marker
    response = client.post("/reports", json=payload)
    assert response.status_code == 422
    assert marker not in response.text


@pytest.mark.parametrize("location", ["collected_at", "started_at"])
def test_timestamps_require_timezone(client, payload, location):
    if location == "collected_at":
        payload[location] = "2026-11-01T12:00:00"
    else:
        payload["sessions"][0][location] = "2026-11-01T12:00:00"
    response = client.post("/reports", json=payload)
    assert response.status_code == 422


def test_malformed_json_is_rejected_without_echoing_body(client):
    marker = "PRIVATE_MALFORMED_JSON_VALUE"
    response = client.post(
        "/reports",
        content='{"secret": "' + marker,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert marker not in response.text


def test_non_json_content_type_is_rejected(client):
    response = client.post("/reports", content="{}", headers={"Content-Type": "text/plain"})
    assert response.status_code == 415


def test_excessive_declared_body_size_is_rejected(client, tmp_path):
    response = client.post(
        "/reports",
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(MAX_BODY_BYTES + 1),
        },
    )
    assert response.status_code == 413
    assert not list(tmp_path.glob("*.json"))


def test_excessive_body_without_content_length_is_rejected(client, tmp_path):
    def chunks():
        yield b" " * (MAX_BODY_BYTES // 2)
        yield b" " * (MAX_BODY_BYTES // 2 + 1)

    response = client.post(
        "/reports",
        content=chunks(),
        headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"},
    )
    assert response.status_code == 413
    assert not list(tmp_path.glob("*.json"))


def test_repeated_report_replaces_same_daily_file(client, payload, tmp_path):
    first = client.post("/reports", json=payload)
    assert first.status_code == 200
    payload["hostname"] = "updated-host"
    second = client.post("/reports", json=payload)
    assert second.status_code == 200
    assert second.json()["filename"] == first.json()["filename"]
    assert len(list(tmp_path.glob("*.json"))) == 1
    response = client.get("/reports", params={"ip": "127.0.0.1"})
    assert response.status_code == 200
    assert response.json()["hostname"] == "updated-host"


def test_corrupt_daily_copy_does_not_corrupt_database_query(client, payload, tmp_path):
    receipt = client.post("/reports", json=payload)
    assert receipt.status_code == 200
    path = tmp_path / receipt.json()["filename"]
    path.write_text("CORRUPT_PRIVATE_DISK_CONTENT", encoding="utf-8")
    response = client.get("/reports", params={"ip": "127.0.0.1"})
    assert response.status_code == 200
    assert response.json()["hostname"] == payload["hostname"]
    assert "CORRUPT_PRIVATE_DISK_CONTENT" not in response.text
    assert str(path) not in response.text
    assert "Traceback" not in response.text


def test_history_pagination_and_detail(client, payload):
    client.post("/reports", json=payload)
    payload["hostname"] = "second"
    client.post("/reports", json=payload)
    rows = client.get("/history?ip=127.0.0.1&limit=1").json()
    assert len(rows) == 1
    assert rows[0]["hostname"] == "second"
    detail = client.get(f"/history/{rows[0]['id']}")
    assert detail.json()["hostname"] == "second"
    older = client.get("/history?ip=127.0.0.1&limit=1&offset=1").json()
    assert older[0]["hostname"] == "test-host"
    assert client.get("/history/9999").status_code == 404
    assert client.get("/history?ip=127.0.0.1&limit=101").status_code == 422
    assert client.get("/history?ip=127.0.0.1&day=2000-01-01").json() == []


def test_local_interface_has_no_login(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'type="password"' not in response.text
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert client.get("/static/app.js").status_code == 200


def test_database_error_is_generic(client, monkeypatch):
    import sqlite3

    from api.database import InventoryStore

    def fail(self, ip):
        raise sqlite3.DatabaseError("PRIVATE_DATABASE_DETAIL")

    monkeypatch.setattr(InventoryStore, "latest", fail)
    response = client.get("/reports?ip=127.0.0.1")
    assert response.status_code == 500
    assert "PRIVATE_DATABASE_DETAIL" not in response.text


def test_history_accepts_maximum_date_without_server_error(client):
    response = client.get("/history?ip=127.0.0.1&day=9999-12-31")
    assert response.status_code == 200
    assert response.json() == []


def test_consulting_does_not_create_or_modify_inventory(client, payload):
    client.post("/reports", json=payload)
    before = client.get("/history?ip=127.0.0.1").json()
    first = client.get("/reports?ip=127.0.0.1").json()
    second = client.get("/reports?ip=127.0.0.1").json()
    detail = client.get(f"/history/{before[0]['id']}").json()
    assert first == second == detail
    assert client.get("/history?ip=127.0.0.1").json() == before


def test_dashboard_and_exports_read_history_without_creating_reports(client, payload):
    client.post("/reports", json=payload)
    payload["operating_system"]["version"] = "updated"
    client.post("/reports", json=payload)
    history = client.get("/history?ip=127.0.0.1").json()
    base, compared = history[1]["id"], history[0]["id"]
    query = f"/comparisons?before={base}&after={compared}"
    response = client.get(query)
    assert response.status_code == 200
    assert response.json()["changes"] == [
        {
            "section": "Sistema operativo",
            "field": "Versión del SO",
            "before": "test-version",
            "after": "updated",
        }
    ]
    csv = client.get(query + "&format=csv")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert f"_{base}_{compared}.csv" in csv.headers["content-disposition"]
    assert "test-version,updated" in csv.text
    html = client.get(query + "&format=html")
    assert html.status_code == 200 and html.headers["content-type"].startswith("text/html")
    assert "frame-ancestors 'none'" in html.headers["content-security-policy"]
    assert "test-version" in html.text and "updated" in html.text
    dashboard = client.get("/dashboard?limit=1")
    assert dashboard.status_code == 200
    assert dashboard.json()["summary"]["hosts"] == 1
    assert dashboard.json()["summary"]["reports"] == 2
    assert client.get("/history?ip=127.0.0.1").json() == history


@pytest.mark.parametrize(
    "query",
    [
        "/dashboard?stale_hours=0",
        "/dashboard?stale_hours=169",
        "/dashboard?limit=101",
        "/dashboard?offset=-1",
        "/comparisons?before=0&after=1",
        "/comparisons?before=1&after=2&format=pdf",
    ],
)
def test_reporting_rejects_invalid_parameters(client, query):
    assert client.get(query).status_code == 422


def test_comparison_handles_missing_mixed_and_reversed_sources(client, payload):
    assert client.get("/comparisons?before=1&after=2").status_code == 404
    client.post("/reports", json=payload)
    client.post("/reports", json=payload)
    payload["server_ip"] = "192.0.2.2"
    client.post("/reports", json=payload)
    for before, after in [(1, 1), (2, 1), (1, 3)]:
        response = client.get(f"/comparisons?before={before}&after={after}")
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], str)
