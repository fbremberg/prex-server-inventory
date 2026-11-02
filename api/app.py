"""Recepción, consulta e histórico para uso local."""

import logging
import os
import sqlite3
from datetime import date
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from api.database import InventoryStore
from api.reporting import compare_reports, csv_report, html_report
from shared.config import MAX_BODY_BYTES
from shared.models import IngestResult, InventoryReport, StoredReport

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    store = InventoryStore(Path(os.environ.get("INVENTORY_DATA_DIR", "data")))
    app = FastAPI(title="Prex Server Inventory", version="1.0.0", debug=False)
    app.add_middleware(RequestBodyLimitMiddleware, max_body_size=MAX_BODY_BYTES)

    @app.middleware("http")
    async def response_policy(request: Request, call_next) -> Response:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if request.method == "POST" and content_type != "application/json":
            response = JSONResponse(status_code=415, content={"detail": "Se requiere JSON."})
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Swagger y ReDoc generan su propia página con recursos de CDN.
        if request.url.path not in {"/docs", "/redoc", "/docs/oauth2-redirect"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_report(request: Request, error: RequestValidationError) -> JSONResponse:
        # No devolver el cuerpo recibido: puede contener información sensible.
        return JSONResponse(
            status_code=422,
            content={"detail": "Datos inválidos; revisar formato, campos y límites."},
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/reports", response_model=IngestResult)
    def receive_report(report: InventoryReport) -> IngestResult:
        try:
            stored, filename = store.save(report)
        except (OSError, sqlite3.Error):
            logger.exception("Error al guardar el inventario.")
            raise HTTPException(status_code=500, detail="Error de almacenamiento.") from None
        return IngestResult(
            server_ip=stored.server_ip, received_at=stored.received_at, filename=filename
        )

    @app.get("/reports", response_model=StoredReport)
    def query_report(ip: IPv4Address) -> StoredReport:
        try:
            return store.latest(ip)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No hay datos para esa IP.") from None
        except (OSError, sqlite3.Error, ValidationError, ValueError):
            logger.exception("Error al leer el inventario.")
            raise HTTPException(status_code=500, detail="Error de almacenamiento.") from None

    @app.get("/history")
    def history(
        ip: IPv4Address,
        day: date | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
    ) -> list[dict]:
        try:
            return store.history(ip, day, limit, offset)
        except (OSError, sqlite3.Error):
            logger.exception("Error al consultar el histórico.")
            raise HTTPException(status_code=500, detail="Error de almacenamiento.") from None

    @app.get("/history/{report_id}", response_model=StoredReport)
    def historical_report(report_id: int) -> StoredReport:
        try:
            return store.get(report_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No existe ese relevamiento.") from None
        except (OSError, sqlite3.Error, ValidationError, ValueError):
            logger.exception("Error al leer el histórico.")
            raise HTTPException(status_code=500, detail="Error de almacenamiento.") from None

    @app.get("/dashboard")
    def dashboard(
        stale_hours: Annotated[int, Query(ge=1, le=168)] = 2,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=1000000)] = 0,
    ) -> dict:
        try:
            return store.dashboard(stale_hours, limit, offset)
        except (OSError, sqlite3.Error):
            logger.exception("Error al consultar el dashboard.")
            raise HTTPException(status_code=500, detail="Error de almacenamiento.") from None

    @app.get("/comparisons")
    def comparison(
        before: Annotated[int, Query(ge=1)],
        after: Annotated[int, Query(ge=1)],
        output: Annotated[Literal["json", "csv", "html"], Query(alias="format")] = "json",
    ) -> Response:
        try:
            base, compared = store.get(before), store.get(after)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="No existe ese relevamiento.") from None
        except (OSError, sqlite3.Error, ValidationError, ValueError):
            logger.exception("Error al leer las fuentes de la comparación.")
            raise HTTPException(status_code=500, detail="Error de almacenamiento.") from None
        try:
            result = compare_reports(before, base, after, compared)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        if output == "csv":
            filename = f"comparacion_{result['server_ip']}_{before}_{after}.csv"
            return Response(
                csv_report(result),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        if output == "html":
            return HTMLResponse(html_report(result))
        return JSONResponse(result)

    static = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def interface() -> FileResponse:
        return FileResponse(static / "index.html")

    return app
