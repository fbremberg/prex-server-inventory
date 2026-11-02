"""Tests de persistencia, fechas y recuperación ante errores de escritura."""

import pytest

from api import storage
from api.storage import JsonStore
from shared.models import StoredReport


@pytest.fixture
def report():
    return StoredReport.model_validate(
        {
            "server_ip": "192.0.2.10",
            "received_at": "2026-11-02T01:02:03Z",
            "hostname": "storage-test",
            "collected_at": "2026-11-01T12:00:00+00:00",
            "operating_system": {"name": "Linux", "version": "test-version"},
            "cpu": {
                "architecture": "x86_64",
                "model": None,
                "physical_cores": None,
                "logical_cores": 2,
                "frequency_mhz": None,
            },
            "processes": [{"pid": 1, "name": None}],
            "sessions": [],
            "warnings": [],
        }
    )


def test_filename_uses_reception_date_in_utc(tmp_path, report):
    store = JsonStore(tmp_path)
    filename = store.write(report)
    assert filename == "192.0.2.10_2026-11-02.json"
    assert StoredReport.model_validate_json((tmp_path / filename).read_text()) == report


def test_replace_failure_keeps_previous_json_and_removes_temporary_file(
    monkeypatch, tmp_path, report
):
    store = JsonStore(tmp_path)
    filename = store.write(report)
    old_bytes = (tmp_path / filename).read_bytes()
    old_files = {path.name for path in tmp_path.iterdir()}

    def fail_replace(*args, **kwargs):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.write(report.model_copy(update={"hostname": "must-not-be-persisted"}))

    assert (tmp_path / filename).read_bytes() == old_bytes
    assert {path.name for path in tmp_path.iterdir()} == old_files
    assert StoredReport.model_validate_json((tmp_path / filename).read_text()) == report
