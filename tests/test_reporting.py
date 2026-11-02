"""Comparaciones con fuentes trazables y exportaciones seguras."""

import csv
from io import StringIO

import pytest

from api.reporting import compare_reports, csv_report, html_report
from shared.models import StoredReport


def snapshot(received_at, **changes):
    data = {
        "server_ip": "192.0.2.10",
        "hostname": "demo",
        "collected_at": "2026-11-01T10:00:00Z",
        "received_at": received_at,
        "operating_system": {"name": "Linux", "version": "1"},
        "cpu": {
            "architecture": "x86_64",
            "model": None,
            "physical_cores": 2,
            "logical_cores": 4,
            "frequency_mhz": None,
        },
        "processes": [{"pid": 1, "name": "service"}],
        "sessions": [{"username": "demo", "terminal": "pts/0", "started_at": None}],
        "warnings": [],
    }
    data.update(changes)
    return StoredReport.model_validate(data)


def test_comparison_counts_instances_and_keeps_source_ids():
    before = snapshot("2026-11-01T10:00:01Z")
    after = snapshot(
        "2026-11-01T11:00:01Z",
        operating_system={"name": "Linux", "version": "2"},
        processes=[
            {"pid": 2, "name": "service"},
            {"pid": 3, "name": "service"},
            {"pid": 4, "name": None},
        ],
        sessions=[],
        warnings=["Sesiones no disponibles"],
    )
    result = compare_reports(8, before, 12, after)
    rows = {
        (row["section"], row["field"]): (row["before"], row["after"]) for row in result["changes"]
    }
    assert rows["Sistema operativo", "Versión del SO"] == ("1", "2")
    assert rows["Procesos", "service"] == (1, 2)
    assert rows["Procesos", "[Nombre no disponible]"] == (0, 1)
    assert rows["Sesiones", "demo · pts/0"] == (1, 0)
    assert rows["Observaciones", "Avisos de recolección"] == (
        "Sin avisos",
        "Sesiones no disponibles",
    )
    assert result["before"]["id"] == 8 and result["after"]["id"] == 12
    assert result["after"]["warnings"] == after.warnings
    assert result["before"]["received_at"] == before.received_at.isoformat()


def test_pid_and_session_start_changes_do_not_imply_inventory_changes():
    before = snapshot("2026-11-01T10:00:01Z")
    after = snapshot(
        "2026-11-01T11:00:01Z",
        processes=[{"pid": 999, "name": "service"}],
        sessions=[{"username": "demo", "terminal": "pts/0", "started_at": "2026-11-01T10:30:00Z"}],
    )
    result = compare_reports(1, before, 2, after)
    assert result["changes"] == []
    assert "Sin diferencias" in csv_report(result)
    assert "Sin diferencias" in html_report(result)


def test_repeated_sessions_are_counted_without_deduplication():
    before = snapshot("2026-11-01T10:00:01Z")
    session = {"username": "demo", "terminal": "pts/0", "started_at": None}
    after = snapshot("2026-11-01T11:00:01Z", sessions=[session, session])
    assert compare_reports(1, before, 2, after)["changes"] == [
        {"section": "Sesiones", "field": "demo · pts/0", "before": 1, "after": 2}
    ]


@pytest.mark.parametrize(
    "before_id,after_id,after_time,after_ip",
    [
        (1, 2, "2026-11-01T11:00:01Z", "192.0.2.11"),
        (1, 1, "2026-11-01T11:00:01Z", "192.0.2.10"),
        (1, 2, "2026-11-01T09:00:01Z", "192.0.2.10"),
        (2, 1, "2026-11-01T10:00:01Z", "192.0.2.10"),
    ],
)
def test_comparison_rejects_other_hosts_and_invalid_order(
    before_id, after_id, after_time, after_ip
):
    before = snapshot("2026-11-01T10:00:01Z")
    after = snapshot(after_time, server_ip=after_ip)
    with pytest.raises(ValueError):
        compare_reports(before_id, before, after_id, after)


def test_id_breaks_ties_when_receipt_times_match():
    report = snapshot("2026-11-01T10:00:01Z")
    assert compare_reports(1, report, 2, report)["changes"] == []


@pytest.mark.parametrize("text", ["=1+1", "+cmd", "-1", "@sum(1)", "  =1", "\ttext"])
def test_csv_treats_inventory_values_as_text(text):
    before = snapshot("2026-11-01T10:00:01Z")
    after = snapshot("2026-11-01T11:00:01Z", hostname=text)
    output = csv_report(compare_reports(1, before, 2, after))
    rows = list(csv.reader(StringIO(output.lstrip("\ufeff"))))
    assert ["Equipo", "Nombre del equipo", "demo", "'" + text] in rows
    assert output.startswith("\ufeff")


def test_html_escapes_inventory_and_preserves_warnings_in_both_sources():
    warning = '<script>alert("test")</script>'
    before = snapshot("2026-11-01T10:00:01Z", warnings=[warning])
    after = snapshot("2026-11-01T11:00:01Z", hostname="<img src=x>", warnings=[warning])
    output = html_report(compare_reports(1, before, 2, after))
    assert warning not in output and "<img src=x>" not in output
    assert "&lt;img src=x&gt;" in output
    assert "Base: &lt;script&gt;" in output
    assert "Comparado: &lt;script&gt;" in output
    assert "Imprimir / guardar PDF" in output


def test_export_source_dates_are_utc_even_if_agent_sends_another_timezone():
    before = snapshot("2026-11-01T10:00:01Z", collected_at="2026-11-01T07:00:00-03:00")
    after = snapshot("2026-11-01T11:00:01Z", collected_at="2026-11-01T08:00:00-03:00")
    result = compare_reports(1, before, 2, after)
    assert result["before"]["collected_at"] == "2026-11-01T10:00:00+00:00"
    assert result["after"]["collected_at"] == "2026-11-01T11:00:00+00:00"
