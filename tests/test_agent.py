"""Pruebas del agente sin recolectar datos personales ni acceder a la red."""

from datetime import UTC, datetime
from ipaddress import IPv4Address
from types import SimpleNamespace

import httpx
import psutil
import pytest

from agent import __main__ as cli
from agent import collector
from shared.models import InventoryReport


def test_process_permissions_and_disappearance_do_not_abort_inventory(monkeypatch):
    class FakeProcess:
        def __init__(self, pid, name=None, error=None):
            self.pid, self._name, self.error = pid, name, error

        def name(self):
            if self.error:
                raise self.error
            return self._name

    monkeypatch.setattr(
        collector.psutil,
        "process_iter",
        lambda: iter(
            [
                FakeProcess(1, "normal"),
                FakeProcess(2, error=psutil.AccessDenied(2)),
                FakeProcess(3, error=psutil.NoSuchProcess(3)),
            ]
        ),
    )
    warnings = []
    processes = collector.collect_processes(warnings)
    assert [(process.pid, process.name) for process in processes] == [(1, "normal"), (2, None)]
    assert len(warnings) == 2


def test_sessions_preserve_distinct_logins_of_the_same_user(monkeypatch):
    monkeypatch.setattr(
        collector.psutil,
        "users",
        lambda: [
            SimpleNamespace(name="test-user", terminal="pts/0", started=1_700_000_000),
            SimpleNamespace(name="test-user", terminal="pts/1", started=1_700_000_001),
        ],
    )
    warnings = []
    sessions = collector.collect_sessions(warnings)
    assert len(sessions) == 2
    assert sessions[0].terminal != sessions[1].terminal
    assert sessions[0].started_at.tzinfo is not None
    assert warnings == []


def test_sessions_unavailable_are_reported_as_a_warning(monkeypatch):
    def denied():
        raise psutil.AccessDenied()

    monkeypatch.setattr(collector.psutil, "users", denied)
    warnings = []
    assert collector.collect_sessions(warnings) == []
    assert warnings


def test_empty_sessions_do_not_claim_absence_of_users(monkeypatch):
    monkeypatch.setattr(collector.psutil, "users", lambda: [])
    warnings = []
    assert collector.collect_sessions(warnings) == []
    assert "no prueba" in warnings[0]


def test_linux_distribution_version_is_used_when_available(monkeypatch):
    monkeypatch.setattr(collector.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        collector.platform,
        "freedesktop_os_release",
        lambda: {"PRETTY_NAME": "Test Linux", "VERSION_ID": "42"},
    )
    operating_system = collector.collect_os()
    assert operating_system.name == "Test Linux"
    assert operating_system.version == "42"


def test_agent_checks_url_before_collecting_or_sending(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["agent", "--ip", "127.0.0.1", "--api-url", "http://192.0.2.10:8000"]
    )

    def must_not_collect(*args):
        pytest.fail("La recolección no debe empezar sin configuración válida.")

    monkeypatch.setattr(cli, "collect_report", must_not_collect)
    assert cli.main() == 1
    assert "HTTP solo se admite en loopback" in capsys.readouterr().err


@pytest.mark.parametrize(
    "url",
    [
        "http://192.0.2.10:8000",
        "http://127.0.0.1.attacker.invalid",
        "https://user:password@example.invalid",
        "file:///tmp/data",
        "http://127.0.0.1:8000/other",
        "http://127.0.0.1:8000?page=1",
        "http://127.0.0.1:99999",
    ],
)
def test_agent_rejects_unsafe_or_ambiguous_api_urls(url):
    with pytest.raises(ValueError):
        cli.validate_api_url(url)


@pytest.fixture
def report():
    return InventoryReport(
        server_ip=IPv4Address("127.0.0.1"),
        hostname="test-host",
        collected_at=datetime.now(UTC),
        operating_system={"name": "Test OS", "version": "1"},
        cpu={
            "architecture": "x86_64",
            "model": None,
            "physical_cores": None,
            "logical_cores": 2,
            "frequency_mhz": None,
        },
        processes=[{"pid": 1, "name": "test"}],
        sessions=[],
    )


def test_agent_sends_expected_contract_and_does_not_follow_redirects(monkeypatch, report):
    requests = []
    real_client = httpx.Client

    def handler(request):
        requests.append(request)
        assert request.headers["Content-Type"] == "application/json"
        assert InventoryReport.model_validate_json(request.content) == report
        assert request.url.path == "/reports"
        return httpx.Response(307, headers={"Location": "https://example.invalid/steal"})

    def client_factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"] == 10.0
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cli.httpx, "Client", client_factory)
    with pytest.raises(httpx.HTTPStatusError):
        cli.send_report(report, "http://127.0.0.1:8000")
    assert len(requests) == 1


def test_agent_does_not_send_an_oversized_report(monkeypatch, report):
    monkeypatch.setattr(cli, "MAX_BODY_BYTES", 1)
    with pytest.raises(ValueError, match="4 MiB"):
        cli.send_report(report, "http://127.0.0.1:8000")


def test_windows_name_includes_release_and_keeps_build_version(monkeypatch):
    monkeypatch.setattr(collector.platform, "system", lambda: "Windows")
    monkeypatch.setattr(collector.platform, "version", lambda: "10.0.26200")
    monkeypatch.setattr(
        collector.platform, "win32_ver", lambda **kwargs: ("11", "10.0.26200", "", "")
    )
    result = collector.collect_os()
    assert result.name == "Windows 11"
    assert result.version == "10.0.26200"


def test_linux_name_and_version_are_separate(monkeypatch):
    monkeypatch.setattr(collector.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        collector.platform,
        "freedesktop_os_release",
        lambda: {
            "NAME": "Ubuntu",
            "PRETTY_NAME": "Ubuntu 24.04.1 LTS",
            "VERSION": "24.04.1 LTS (Noble Numbat)",
            "VERSION_ID": "24.04",
        },
    )
    result = collector.collect_os()
    assert result.name == "Ubuntu"
    assert result.version == "24.04.1 LTS (Noble Numbat)"


def test_windows_cpu_uses_model_from_registry(monkeypatch):
    import sys
    from contextlib import nullcontext

    registry = SimpleNamespace(
        HKEY_LOCAL_MACHINE=1,
        OpenKey=lambda root, path: nullcontext("key"),
        QueryValueEx=lambda key, name: ("  AMD Ryzen Test  ", 1),
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(collector.platform, "system", lambda: "Windows")
    monkeypatch.setattr(collector.platform, "processor", lambda: "AMD64 Family 25")
    warnings = []
    assert collector.collect_cpu_model(warnings) == "AMD Ryzen Test"
    assert warnings == []


def test_linux_cpu_uses_model_instead_of_architecture(monkeypatch):
    monkeypatch.setattr(collector.platform, "system", lambda: "Linux")
    monkeypatch.setattr(collector.platform, "processor", lambda: "x86_64")
    monkeypatch.setattr(
        collector.Path,
        "read_text",
        lambda self, **kwargs: "processor: 0\nmodel name: Intel Test CPU\nprocessor: 1\n",
    )
    warnings = []
    assert collector.collect_cpu_model(warnings) == "Intel Test CPU"
    assert warnings == []


@pytest.mark.parametrize(
    "fallback,expected", [("", None), ("x86_64", None), ("Family 25", "Family 25")]
)
def test_cpu_unavailable_does_not_invent_model(monkeypatch, fallback, expected):
    def unavailable(*args, **kwargs):
        raise PermissionError("unavailable")

    monkeypatch.setattr(collector.platform, "system", lambda: "Linux")
    monkeypatch.setattr(collector.platform, "processor", lambda: fallback)
    monkeypatch.setattr(collector.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(collector.Path, "read_text", unavailable)
    warnings = []
    assert collector.collect_cpu_model(warnings) == expected
    assert len(warnings) == 1


def test_empty_process_name_is_explicitly_reported(monkeypatch):
    monkeypatch.setattr(
        collector.psutil, "process_iter", lambda: iter([SimpleNamespace(pid=0, name=lambda: "")])
    )
    warnings = []
    assert collector.collect_processes(warnings)[0].name is None
    assert "Procesos sin nombre informado por el sistema: 1." in warnings[0]


def test_cli_collects_once_then_posts_complete_report(monkeypatch, report, capsys):
    events = []
    real_client = httpx.Client

    def collect(ip):
        events.append("collect")
        assert ip == report.server_ip
        return report

    def receive(request):
        events.append("post")
        assert request.method == "POST"
        assert request.url.path == "/reports"
        assert InventoryReport.model_validate_json(request.content) == report
        return httpx.Response(200, json={"server_ip": str(report.server_ip)})

    monkeypatch.setattr(cli, "collect_report", collect)
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(receive), **kwargs),
    )
    monkeypatch.setattr("sys.argv", ["agent", "--ip", "127.0.0.1"])
    assert cli.main() == 0
    assert events == ["collect", "post"]
    assert "Inventario enviado" in capsys.readouterr().out
