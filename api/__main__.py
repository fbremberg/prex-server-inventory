"""Ejecutar desde la raíz del proyecto con: python -m api."""

import os
import sqlite3
import sys

import uvicorn

from api.app import create_app


def main() -> int:
    try:
        app = create_app()
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"No se pudo iniciar la API: {error}", file=sys.stderr)
        return 2
    uvicorn.run(
        app,
        host=os.environ.get("INVENTORY_BIND_HOST", "127.0.0.1"),
        port=8000,
        workers=1,
        proxy_headers=False,
        server_header=False,
        limit_concurrency=32,
        timeout_keep_alive=5,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
