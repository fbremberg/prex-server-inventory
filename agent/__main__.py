"""Ejecutar con: python -m agent --ip 127.0.0.1."""

import argparse
import sys
from ipaddress import IPv4Address
from urllib.parse import urlsplit

import httpx
import psutil
from pydantic import ValidationError

from agent.collector import collect_report
from shared.config import MAX_BODY_BYTES
from shared.models import InventoryReport


def validate_api_url(value: str) -> str:
    parsed = urlsplit(value)
    # Acceder a port también valida que el puerto sea un entero entre 0 y 65535.
    _ = parsed.port
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("La URL debe ser una dirección base HTTP(S), sin credenciales ni ruta.")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("HTTP solo se admite en loopback; para una API remota se requiere HTTPS.")
    return value.rstrip("/")


def send_report(report: InventoryReport, api_url: str) -> None:
    api_url = validate_api_url(api_url)
    body = report.model_dump_json().encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("El inventario supera el límite de 4 MiB; no se envió parcialmente.")
    with httpx.Client(timeout=10.0, trust_env=False, follow_redirects=False) as client:
        response = client.post(
            f"{api_url}/reports",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description="Releva el equipo y envía un inventario a la API.")
    parser.add_argument(
        "--ip", required=True, type=IPv4Address, help="IPv4 de inventario del equipo."
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8000", help="URL base de la API.")
    arguments = parser.parse_args()
    try:
        api_url = validate_api_url(arguments.api_url)
        report = collect_report(arguments.ip)
        send_report(report, api_url)
    except httpx.HTTPStatusError as error:
        print(f"La API rechazó el envío: HTTP {error.response.status_code}.", file=sys.stderr)
        return 1
    except httpx.RequestError:
        print(
            "No se pudo contactar a la API. Revisar dirección, conexión y certificado.",
            file=sys.stderr,
        )
        return 1
    except ValidationError:
        print(
            "El inventario no cumple el contrato; no se envió. Revisar límites y datos.",
            file=sys.stderr,
        )
        return 1
    except (ValueError, OSError, psutil.Error) as error:
        print(f"No se pudo completar el relevamiento: {error}", file=sys.stderr)
        return 1
    print(
        f"Inventario enviado para {report.server_ip}: "
        f"{len(report.processes)} procesos, {len(report.sessions)} sesiones."
    )
    print(f"CPU: {report.cpu.model or 'No disponible'}.")
    print(f"Sistema: {report.operating_system.name}; versión: {report.operating_system.version}.")
    for warning in report.warnings:
        print(f"Aviso: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
