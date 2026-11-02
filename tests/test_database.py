"""Persistencia del histórico y recuperación de las copias diarias."""

import sqlite3
from datetime import date
from ipaddress import IPv4Address

import pytest

from api.database import InventoryStore
from shared.models import InventoryReport


@pytest.fixture
def report():
    return InventoryReport.model_validate(
        {
            "server_ip": "192.0.2.10",
            "hostname": "original",
            "collected_at": "2026-11-01T12:00:00Z",
            "operating_system": {"name": "Linux", "version": "test"},
            "cpu": {
                "architecture": "x86_64",
                "model": None,
                "physical_cores": 2,
                "logical_cores": 4,
                "frequency_mhz": None,
            },
            "processes": [{"pid": 1, "name": None}, {"pid": 2, "name": "test"}],
            "sessions": [{"username": "user", "terminal": None, "started_at": None}],
            "warnings": ["Aviso de prueba"],
        }
    )


def test_history_keeps_every_report_and_roundtrips_children(tmp_path, report):
    store = InventoryStore(tmp_path)
    first, filename = store.save(report)
    second, _ = store.save(report.model_copy(update={"hostname": "updated"}))
    rows = store.history(report.server_ip, None, 25, 0)
    assert len(rows) == 2
    assert store.get(rows[0]["id"]) == second
    assert store.get(rows[1]["id"]) == first
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert '"updated"' in (tmp_path / filename).read_text(encoding="utf-8")
    reopened = InventoryStore(tmp_path)
    assert reopened.latest(report.server_ip) == second
    assert reopened.get(rows[1]["id"]) == first


def test_history_filters_by_ip_date_and_page(tmp_path, report):
    store = InventoryStore(tmp_path)
    saved, _ = store.save(report)
    store.save(report)
    store.save(report.model_copy(update={"server_ip": IPv4Address("192.0.2.11")}))
    first = store.history(report.server_ip, saved.received_at.date(), 1, 0)
    second = store.history(report.server_ip, saved.received_at.date(), 1, 1)
    assert len(first) == len(second) == 1
    assert first[0]["id"] != second[0]["id"]
    assert store.history(report.server_ip, date(2000, 1, 1), 25, 0) == []
    with pytest.raises(FileNotFoundError):
        store.get(999)


def test_export_failure_preserves_history_and_restart_repairs_json(tmp_path, report, monkeypatch):
    store = InventoryStore(tmp_path)
    _, filename = store.save(report)

    def fail_write(stored):
        raise OSError("simulated export failure")

    monkeypatch.setattr(store.json, "write", fail_write)
    with pytest.raises(OSError):
        store.save(report.model_copy(update={"hostname": "committed-before-failure"}))
    assert len(store.history(report.server_ip, None, 25, 0)) == 2
    repaired = InventoryStore(tmp_path)
    assert repaired.latest(report.server_ip).hostname == "committed-before-failure"
    assert "committed-before-failure" in (tmp_path / filename).read_text(encoding="utf-8")


def test_database_failure_rolls_back_report_and_leaves_daily_file(tmp_path, report):
    store = InventoryStore(tmp_path)
    _, filename = store.save(report)
    previous = (tmp_path / filename).read_bytes()
    with sqlite3.connect(store.path) as db:
        db.execute("""CREATE TRIGGER reject_process BEFORE INSERT ON processes
                    BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.save(report)
    assert len(store.history(report.server_ip, None, 25, 0)) == 1
    assert (tmp_path / filename).read_bytes() == previous


def test_concurrent_ingestions_preserve_all_reports(tmp_path, report):
    from concurrent.futures import ThreadPoolExecutor

    store = InventoryStore(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.save(report), range(12)))
    assert len(results) == 12
    assert len(store.history(report.server_ip, None, 25, 0)) == 12
    assert len(list(tmp_path.glob("*.json"))) == 1
    daily = next(tmp_path.glob("*.json"))
    assert store.latest(report.server_ip).model_dump_json(indent=2) == daily.read_text().strip()


def test_dashboard_empty_database(tmp_path):
    result = InventoryStore(tmp_path).dashboard(2, 25, 0)
    assert result["summary"] == {
        "hosts": 0,
        "recent": 0,
        "stale": 0,
        "with_warnings": 0,
        "reports": 0,
    }
    assert result["items"] == result["systems"] == []


def test_dashboard_uses_latest_receipt_per_ip_and_global_totals(tmp_path, report):
    from datetime import UTC, datetime, timedelta

    store = InventoryStore(tmp_path)
    store.save(report)
    updated = report.model_copy(deep=True)
    updated.hostname = "current"
    updated.warnings = []
    updated.operating_system.name = "Windows"
    store.save(updated)
    other = report.model_copy(update={"server_ip": IPv4Address("192.0.2.11")})
    store.save(other)
    now = datetime.now(UTC)
    with sqlite3.connect(store.path) as db:
        # La tercera recepción es más vieja aunque su ID sea mayor.
        db.execute(
            "UPDATE reports SET received_at = ? WHERE id = 3",
            ((now - timedelta(hours=3)).isoformat(),),
        )
        # Una fecha de recolección futura no determina la vigencia.
        db.execute(
            "UPDATE reports SET collected_at = ? WHERE id = 3",
            ((now + timedelta(hours=3)).isoformat(),),
        )
    result = store.dashboard(2, 1, 0)
    assert result["summary"] == {
        "hosts": 2,
        "recent": 1,
        "stale": 1,
        "with_warnings": 1,
        "reports": 3,
    }
    assert result["systems"] == [{"name": "Linux", "count": 1}, {"name": "Windows", "count": 1}]
    assert len(result["items"]) == 1
    latest = result["items"][0]
    assert (latest["id"], latest["hostname"], latest["stale"], latest["warnings"]) == (
        2,
        "current",
        0,
        0,
    )
    assert (latest["processes"], latest["sessions"]) == (2, 1)
    older = store.dashboard(2, 1, 1)
    assert older["summary"] == result["summary"]
    assert older["items"][0]["id"] == 3 and older["items"][0]["stale"] == 1
    assert store.dashboard(4, 25, 0)["summary"]["recent"] == 2
    assert store.dashboard(2, 25, 9)["items"] == []


def test_dashboard_breaks_receipt_ties_by_id(tmp_path, report):
    store = InventoryStore(tmp_path)
    first, _ = store.save(report)
    store.save(report.model_copy(update={"hostname": "second"}))
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE reports SET received_at = ?", (first.received_at.isoformat(),))
    assert store.dashboard(2, 25, 0)["items"][0]["hostname"] == "second"
