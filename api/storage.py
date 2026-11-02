"""Publicación atómica del JSON diario derivado del histórico."""

import os
import tempfile
from pathlib import Path
from threading import Lock

from shared.models import StoredReport


class JsonStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        # En Linux, la carpeta nueva queda accesible solo para su propietario.
        # En Windows se heredan los permisos NTFS de la carpeta del proyecto.
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._write_lock = Lock()

    def write(self, stored: StoredReport) -> str:
        with self._write_lock:
            filename = f"{stored.server_ip}_{stored.received_at.date().isoformat()}.json"
            destination = self.directory / filename
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.directory, prefix=".inventory-", suffix=".tmp"
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                    file.write(stored.model_dump_json(indent=2))
                    file.write("\n")
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            return filename
