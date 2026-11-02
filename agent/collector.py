"""Recolección de solo lectura, sin comandos de shell ni elevación de privilegios."""

import platform
import socket
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path

import psutil

from shared.models import CpuInfo, InventoryReport, OperatingSystem, ProcessInfo, SessionInfo


def collect_processes(warnings: list[str]) -> list[ProcessInfo]:
    processes = []
    denied = 0
    unnamed = 0
    disappeared = 0
    for process in psutil.process_iter():
        try:
            name = process.name() or None
            if name is None:
                unnamed += 1
        except psutil.AccessDenied:
            name = None
            denied += 1
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            disappeared += 1
            continue
        processes.append(ProcessInfo(pid=process.pid, name=name))
    if denied:
        warnings.append(f"Nombre no disponible por permisos en {denied} procesos.")
    if unnamed:
        warnings.append(f"Procesos sin nombre informado por el sistema: {unnamed}.")
    if disappeared:
        warnings.append(f"Se omitieron {disappeared} procesos finalizados durante la consulta.")
    return processes


def collect_sessions(warnings: list[str]) -> list[SessionInfo]:
    try:
        users = psutil.users()
    except (psutil.Error, OSError, NotImplementedError):
        warnings.append("No se pudieron consultar las sesiones del sistema.")
        return []
    sessions = []
    invalid_dates = 0
    for user in users:
        started = None
        try:
            if user.started > 0:
                started = datetime.fromtimestamp(user.started, UTC)
        except (ValueError, OverflowError, OSError):
            invalid_dates += 1
        sessions.append(
            SessionInfo(username=user.name, terminal=user.terminal or None, started_at=started)
        )
    if invalid_dates:
        warnings.append(f"Fecha de inicio no disponible en {invalid_dates} sesiones.")
    if not sessions:
        warnings.append("El sistema no devolvió sesiones; esto no prueba que no haya usuarios.")
    return sessions


def collect_os() -> OperatingSystem:
    name, version = platform.system(), platform.version()
    if name == "Windows":
        release, version, _, _ = platform.win32_ver(version=version)
        name = f"Windows {release}".strip()
    elif name == "Linux":
        try:
            release = platform.freedesktop_os_release()
            name = release.get("NAME") or release.get("PRETTY_NAME") or name
            version = release.get("VERSION") or release.get("VERSION_ID") or platform.release()
        except OSError:
            version = platform.release()
    return OperatingSystem(name=name, version=version)


def collect_cpu_model(warnings: list[str]) -> str | None:
    # platform.processor() puede devolver solo la familia o la arquitectura.
    model = ""
    try:
        if platform.system() == "Windows":
            import winreg

            path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                model = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        else:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition(":")
                if separator and key.strip() in {"model name", "Hardware"} and value.strip():
                    model = value.strip()
                    break
    except (OSError, UnicodeError):
        pass
    if not model:
        model = platform.processor().strip()
        if model == platform.machine():
            model = ""
        warnings.append(
            "Modelo comercial de CPU no disponible; se informa el identificador del sistema."
            if model
            else "Modelo de CPU no disponible."
        )
    return model or None


def collect_report(ip: IPv4Address) -> InventoryReport:
    if platform.system() not in {"Windows", "Linux"}:
        raise ValueError("Esta versión del agente admite Windows y Linux.")
    collected_at = datetime.now(UTC)
    warnings: list[str] = []
    frequency = None
    try:
        cpu_frequency = psutil.cpu_freq()
        if cpu_frequency and cpu_frequency.current > 0:
            frequency = cpu_frequency.current
    except (psutil.Error, OSError, NotImplementedError):
        pass
    if frequency is None:
        warnings.append("Frecuencia de CPU no disponible.")
    cpu = CpuInfo(
        architecture=platform.machine(),
        model=collect_cpu_model(warnings),
        physical_cores=psutil.cpu_count(logical=False),
        logical_cores=psutil.cpu_count(logical=True),
        frequency_mhz=frequency,
    )
    processes = collect_processes(warnings)
    sessions = collect_sessions(warnings)
    return InventoryReport(
        server_ip=ip,
        hostname=socket.gethostname(),
        collected_at=collected_at,
        operating_system=collect_os(),
        cpu=cpu,
        processes=processes,
        sessions=sessions,
        warnings=warnings,
    )
