"""Comparación reproducible de dos inventarios y exportación de sus diferencias."""

import csv
from collections import Counter
from datetime import UTC, datetime
from html import escape
from io import StringIO

from shared.models import StoredReport

NOTES = [
    "Se comparan dos instantáneas de la misma IP; la IP no acredita la identidad del equipo.",
    "Los procesos se agrupan por nombre y cantidad, sin usar el PID como identidad estable.",
    "Las sesiones se agrupan por usuario y terminal. No se infiere actividad entre capturas.",
    "Un dato no disponible no prueba ausencia. Los cambios no son un diagnóstico de seguridad.",
]


def compare_reports(
    before_id: int, before: StoredReport, after_id: int, after: StoredReport
) -> dict:
    if before.server_ip != after.server_ip:
        raise ValueError("Elegí dos relevamientos de la misma IP.")
    if before_id == after_id or (before.received_at, before_id) >= (after.received_at, after_id):
        raise ValueError("La captura comparada debe haberse recibido después de la base.")
    changes = []

    def changed(section, field, old, new):
        if old != new:
            changes.append({"section": section, "field": field, "before": old, "after": new})

    changed("Equipo", "Nombre del equipo", before.hostname, after.hostname)
    for field, label in [("name", "Nombre del SO"), ("version", "Versión del SO")]:
        changed(
            "Sistema operativo",
            label,
            getattr(before.operating_system, field),
            getattr(after.operating_system, field),
        )
    for field, label in [
        ("model", "Modelo"),
        ("architecture", "Arquitectura"),
        ("physical_cores", "Núcleos físicos"),
        ("logical_cores", "Núcleos lógicos"),
        ("frequency_mhz", "Frecuencia informada (MHz)"),
    ]:
        changed("Procesador", label, getattr(before.cpu, field), getattr(after.cpu, field))

    old_processes = Counter(item.name for item in before.processes)
    new_processes = Counter(item.name for item in after.processes)
    for name in sorted(old_processes.keys() | new_processes.keys(), key=lambda item: item or ""):
        changed(
            "Procesos",
            name if name is not None else "[Nombre no disponible]",
            old_processes[name],
            new_processes[name],
        )
    old_sessions = Counter((item.username, item.terminal) for item in before.sessions)
    new_sessions = Counter((item.username, item.terminal) for item in after.sessions)
    for user, terminal in sorted(
        old_sessions.keys() | new_sessions.keys(), key=lambda item: (item[0], item[1] or "")
    ):
        label = f"{user} · {terminal if terminal is not None else '[Terminal no disponible]'}"
        changed("Sesiones", label, old_sessions[user, terminal], new_sessions[user, terminal])
    changed(
        "Observaciones",
        "Avisos de recolección",
        "\n".join(before.warnings) or "Sin avisos",
        "\n".join(after.warnings) or "Sin avisos",
    )

    def source(report_id, report):
        return {
            "id": report_id,
            "hostname": report.hostname,
            "collected_at": report.collected_at.astimezone(UTC).isoformat(),
            "received_at": report.received_at.astimezone(UTC).isoformat(),
            "warnings": report.warnings,
        }

    return {
        "server_ip": str(before.server_ip),
        "generated_at": datetime.now(UTC).isoformat(),
        "before": source(before_id, before),
        "after": source(after_id, after),
        "summary": dict(Counter(item["section"] for item in changes)),
        "changes": changes,
        "notes": NOTES,
    }


def display(value) -> str:
    return "No disponible" if value is None else str(value)


def csv_report(comparison: dict) -> str:
    output = StringIO(newline="")
    writer = csv.writer(output)

    def row(values):
        # Los nombres del inventario no deben convertirse en fórmulas al abrir el CSV.
        safe = []
        for value in values:
            text = display(value)
            if text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(
                ("=", "+", "-", "@")
            ):
                text = "'" + text
            safe.append(text)
        writer.writerow(safe)

    row(["Informe de cambios", comparison["server_ip"]])
    row(["Generado (UTC)", comparison["generated_at"]])
    row(["Selección", "ID", "Equipo", "Recolección (UTC)", "Recepción (UTC)"])
    for label, side in [("Base", "before"), ("Comparado", "after")]:
        source = comparison[side]
        row(
            [label, source["id"], source["hostname"], source["collected_at"], source["received_at"]]
        )
        for warning in source["warnings"]:
            row(["Aviso de " + label, warning])
    row(["Sección", "Dato", "Base", "Comparado"])
    for change in comparison["changes"]:
        row([change[key] for key in ("section", "field", "before", "after")])
    if not comparison["changes"]:
        row(["Sin diferencias en los campos comparados"])
    for note in comparison["notes"]:
        row(["Criterio y límite", note])
    return "\ufeff" + output.getvalue()


def html_report(comparison: dict) -> str:
    def e(value):
        return escape(display(value))

    source_rows = []
    for label, side in [("Base", "before"), ("Comparado", "after")]:
        source = comparison[side]
        source_rows.append(
            "<tr>"
            + "".join(
                f"<td>{e(value)}</td>"
                for value in [
                    label,
                    source["id"],
                    source["hostname"],
                    source["collected_at"],
                    source["received_at"],
                ]
            )
            + "</tr>"
        )
    changes = "".join(
        "<tr>"
        + "".join(f"<td>{e(change[key])}</td>" for key in ("section", "field", "before", "after"))
        + "</tr>"
        for change in comparison["changes"]
    )
    if not changes:
        changes = '<tr><td colspan="4">Sin diferencias en los campos comparados.</td></tr>'
    notes = "".join(f"<li>{e(note)}</li>" for note in comparison["notes"])
    warnings = "".join(
        f"<li>{label}: {e(warning)}</li>"
        for label, side in [("Base", "before"), ("Comparado", "after")]
        for warning in comparison[side]["warnings"]
    )
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Informe de cambios · {e(comparison["server_ip"])}</title>
<link rel="stylesheet" href="/static/report.css"><script src="/static/report.js" defer></script>
</head><body><main>
<div class="print-controls"><button id="print">Imprimir / guardar PDF</button></div>
<p class="eyebrow">PREX · INVENTARIO</p><h1>Informe de cambios</h1>
<p>Equipo identificado por IP: <strong>{e(comparison["server_ip"])}</strong><br>
Generado (UTC): {e(comparison["generated_at"])}</p>
<h2>Relevamientos utilizados</h2><table><thead><tr><th>Selección</th><th>ID</th><th>Equipo</th>
<th>Recolección (UTC)</th><th>Recepción (UTC)</th></tr></thead>
<tbody>{"".join(source_rows)}</tbody></table>
<h2>Diferencias ({len(comparison["changes"])})</h2>
<p>En procesos y sesiones, las columnas indican cantidades observadas en cada captura.</p>
<table><thead><tr><th>Sección</th><th>Dato</th><th>Base</th><th>Comparado</th></tr></thead>
<tbody>{changes}</tbody></table><h2>Observaciones de las fuentes</h2>
<ul>{warnings or "<li>Sin avisos de recolección.</li>"}</ul>
<h2>Criterios y límites</h2><ul>{notes}</ul></main></body></html>"""
