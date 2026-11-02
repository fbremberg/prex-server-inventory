"""Histórico normalizado en SQLite y archivos diarios derivados de sus datos."""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from ipaddress import IPv4Address
from pathlib import Path
from threading import Lock

from api.storage import JsonStore
from shared.models import InventoryReport, StoredReport

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    server_ip TEXT NOT NULL, hostname TEXT NOT NULL,
    collected_at TEXT NOT NULL, received_at TEXT NOT NULL,
    os_name TEXT NOT NULL, os_version TEXT NOT NULL,
    architecture TEXT NOT NULL, cpu_model TEXT,
    physical_cores INTEGER, logical_cores INTEGER, frequency_mhz REAL
);
CREATE INDEX IF NOT EXISTS reports_ip_date ON reports(server_ip, received_at DESC, id DESC);
CREATE VIEW IF NOT EXISTS latest_reports AS
SELECT r.* FROM (SELECT DISTINCT server_ip FROM reports) AS ips
JOIN reports r ON r.id = (
    SELECT id FROM reports WHERE server_ip = ips.server_ip
    ORDER BY received_at DESC, id DESC LIMIT 1
);
CREATE TABLE IF NOT EXISTS processes (
    report_id INTEGER NOT NULL REFERENCES reports(id), position INTEGER NOT NULL,
    pid INTEGER NOT NULL, name TEXT, PRIMARY KEY (report_id, position)
);
CREATE TABLE IF NOT EXISTS sessions (
    report_id INTEGER NOT NULL REFERENCES reports(id), position INTEGER NOT NULL,
    username TEXT NOT NULL, terminal TEXT, started_at TEXT,
    PRIMARY KEY (report_id, position)
);
CREATE TABLE IF NOT EXISTS warnings (
    report_id INTEGER NOT NULL REFERENCES reports(id), position INTEGER NOT NULL,
    message TEXT NOT NULL, PRIMARY KEY (report_id, position)
);
"""


class InventoryStore:
    def __init__(self, directory: Path) -> None:
        self.json = JsonStore(directory)
        self.path = self.json.directory / "inventory.sqlite3"
        self._lock = Lock()
        with closing(self.connect()) as db:
            db.executescript(SCHEMA)
            # SQLite es la fuente del histórico. Al reiniciar se reparan las copias diarias
            # si el proceso se detuvo después del commit y antes de publicar el JSON.
            rows = db.execute(
                "SELECT MAX(id) AS id FROM reports GROUP BY server_ip, substr(received_at, 1, 10)"
            ).fetchall()
            for row in rows:
                self.json.write(self._read(db, row["id"]))

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def save(self, report: InventoryReport) -> tuple[StoredReport, str]:
        with self._lock, closing(self.connect()) as db:
            stored = StoredReport(**report.model_dump(), received_at=datetime.now(UTC))
            with db:
                cursor = db.execute(
                    """INSERT INTO reports (server_ip, hostname, collected_at, received_at,
                    os_name, os_version, architecture, cpu_model, physical_cores,
                    logical_cores, frequency_mhz) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(stored.server_ip),
                        stored.hostname,
                        stored.collected_at.isoformat(),
                        stored.received_at.isoformat(),
                        stored.operating_system.name,
                        stored.operating_system.version,
                        stored.cpu.architecture,
                        stored.cpu.model,
                        stored.cpu.physical_cores,
                        stored.cpu.logical_cores,
                        stored.cpu.frequency_mhz,
                    ),
                )
                report_id = cursor.lastrowid
                db.executemany(
                    "INSERT INTO processes VALUES (?, ?, ?, ?)",
                    [
                        (report_id, index, process.pid, process.name)
                        for index, process in enumerate(stored.processes)
                    ],
                )
                db.executemany(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            report_id,
                            index,
                            session.username,
                            session.terminal,
                            session.started_at.isoformat() if session.started_at else None,
                        )
                        for index, session in enumerate(stored.sessions)
                    ],
                )
                db.executemany(
                    "INSERT INTO warnings VALUES (?, ?, ?)",
                    [(report_id, index, warning) for index, warning in enumerate(stored.warnings)],
                )
            # El commit precede al JSON: un error de exportación no borra el histórico.
            # En ese caso el endpoint devuelve 500 y el reinicio reconstruye el archivo.
            filename = self.json.write(stored)
            return stored, filename

    def _read(self, db: sqlite3.Connection, report_id: int) -> StoredReport:
        row = db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if row is None:
            raise FileNotFoundError("No hay datos para ese relevamiento.")
        return StoredReport.model_validate(
            {
                "server_ip": row["server_ip"],
                "hostname": row["hostname"],
                "collected_at": row["collected_at"],
                "received_at": row["received_at"],
                "operating_system": {"name": row["os_name"], "version": row["os_version"]},
                "cpu": {
                    "architecture": row["architecture"],
                    "model": row["cpu_model"],
                    "physical_cores": row["physical_cores"],
                    "logical_cores": row["logical_cores"],
                    "frequency_mhz": row["frequency_mhz"],
                },
                "processes": [
                    dict(item)
                    for item in db.execute(
                        "SELECT pid, name FROM processes WHERE report_id = ? ORDER BY position",
                        (report_id,),
                    )
                ],
                "sessions": [
                    dict(item)
                    for item in db.execute(
                        "SELECT username, terminal, started_at FROM sessions "
                        "WHERE report_id = ? ORDER BY position",
                        (report_id,),
                    )
                ],
                "warnings": [
                    item[0]
                    for item in db.execute(
                        "SELECT message FROM warnings WHERE report_id = ? ORDER BY position",
                        (report_id,),
                    )
                ],
            }
        )

    def latest(self, ip: IPv4Address) -> StoredReport:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT id FROM reports WHERE server_ip = ? "
                "ORDER BY received_at DESC, id DESC LIMIT 1",
                (str(ip),),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("No hay datos para esa IP.")
            return self._read(db, row["id"])

    def get(self, report_id: int) -> StoredReport:
        with closing(self.connect()) as db:
            return self._read(db, report_id)

    def history(self, ip: IPv4Address, day: date | None, limit: int, offset: int) -> list[dict]:
        sql = (
            "SELECT id, server_ip, hostname, collected_at, received_at "
            "FROM reports WHERE server_ip = ?"
        )
        parameters: list = [str(ip)]
        if day is not None:
            start = datetime.combine(day, time.min, tzinfo=UTC)
            end = datetime.combine(day, time.max, tzinfo=UTC)
            sql += " AND received_at >= ? AND received_at <= ?"
            parameters.extend([start.isoformat(), end.isoformat()])
        sql += " ORDER BY received_at DESC, id DESC LIMIT ? OFFSET ?"
        parameters.extend([limit, offset])
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(sql, parameters)]

    def dashboard(self, stale_hours: int, limit: int, offset: int) -> dict:
        now = datetime.now(UTC)
        cutoff = (now - timedelta(hours=stale_hours)).isoformat()
        with closing(self.connect()) as db:
            # Los totales y la página se leen desde la misma instantánea de SQLite.
            db.execute("BEGIN")
            summary = dict(
                db.execute(
                    """
                SELECT COUNT(*) AS hosts,
                COALESCE(SUM(received_at >= ?), 0) AS recent,
                COALESCE(SUM(received_at < ?), 0) AS stale,
                COALESCE(SUM(EXISTS(SELECT 1 FROM warnings w WHERE w.report_id = l.id)), 0)
                    AS with_warnings,
                (SELECT COUNT(*) FROM reports) AS reports
                FROM latest_reports AS l""",
                    (cutoff, cutoff),
                ).fetchone()
            )
            systems = [
                dict(row)
                for row in db.execute("""
                SELECT os_name AS name, COUNT(*) AS count FROM latest_reports AS l
                GROUP BY os_name ORDER BY count DESC, os_name""")
            ]
            items = [
                dict(row)
                for row in db.execute(
                    """
                SELECT id, server_ip, hostname, os_name, os_version, collected_at, received_at,
                    received_at < ? AS stale,
                    (SELECT COUNT(*) FROM processes p WHERE p.report_id = l.id) AS processes,
                    (SELECT COUNT(*) FROM sessions s WHERE s.report_id = l.id) AS sessions,
                    (SELECT COUNT(*) FROM warnings w WHERE w.report_id = l.id) AS warnings
                FROM latest_reports AS l ORDER BY received_at DESC, id DESC LIMIT ? OFFSET ?""",
                    (cutoff, limit, offset),
                )
            ]
        return {
            "generated_at": now.isoformat(),
            "stale_hours": stale_hours,
            "summary": summary,
            "systems": systems,
            "items": items,
            "limit": limit,
            "offset": offset,
        }
