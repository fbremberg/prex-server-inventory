"""Pydantic valida el mismo contrato en el agente y en la API."""

from ipaddress import IPv4Address
from typing import Annotated

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)]
Count = Annotated[int, Field(strict=True, ge=1)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperatingSystem(ContractModel):
    name: Text
    version: Text


class CpuInfo(ContractModel):
    architecture: Text
    model: Text | None
    physical_cores: Count | None
    logical_cores: Count | None
    frequency_mhz: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None


class ProcessInfo(ContractModel):
    pid: Annotated[int, Field(strict=True, ge=0)]
    name: Text | None


class SessionInfo(ContractModel):
    username: Text
    terminal: Text | None
    started_at: AwareDatetime | None


class InventoryReport(ContractModel):
    server_ip: IPv4Address
    hostname: Text
    collected_at: AwareDatetime
    operating_system: OperatingSystem
    cpu: CpuInfo
    processes: Annotated[list[ProcessInfo], Field(max_length=20_000)]
    sessions: Annotated[list[SessionInfo], Field(max_length=1_000)]
    warnings: Annotated[list[Text], Field(max_length=20)] = Field(default_factory=list)

    @field_validator("server_ip", mode="before")
    @classmethod
    def require_ip_string(cls, value: object) -> object:
        # Evita aceptar una IP enviada como número JSON.
        if not isinstance(value, (str, IPv4Address)):
            raise ValueError("La IP debe enviarse como texto IPv4.")
        return value


class StoredReport(InventoryReport):
    received_at: AwareDatetime


class IngestResult(ContractModel):
    server_ip: IPv4Address
    received_at: AwareDatetime
    filename: Text
