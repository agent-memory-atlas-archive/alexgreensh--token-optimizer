"""The Cowork health probe only visits the operator's HTTP(S) endpoint."""

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts/cowork_doctor.py"


def _doctor():
    spec = importlib.util.spec_from_file_location("cowork_doctor_url_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:secret@example.com", "https://example.com?token=secret", "https://example.com\n.evil.test"])
def test_health_probe_rejects_non_endpoint_urls(monkeypatch, url):
    doctor = _doctor()
    monkeypatch.setenv("TO_COWORK_COLLECTOR_URL", url)
    monkeypatch.delenv("TO_PROBE_URL", raising=False)
    monkeypatch.setattr(doctor.urllib.request, "build_opener", lambda *args: pytest.fail("network request attempted"))

    checks = doctor._telemetry_checks()

    assert checks[-1]["status"] == "WARN"
    assert "invalid collector URL" in checks[-1]["detail"]
    assert "secret" not in checks[-1]["detail"]


def test_health_probe_requests_only_healthz_without_redirects(monkeypatch):
    doctor = _doctor()
    monkeypatch.setenv("TO_COWORK_COLLECTOR_URL", "http://127.0.0.1:4321")
    monkeypatch.delenv("TO_PROBE_URL", raising=False)
    opened = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Opener:
        def open(self, url, timeout):
            opened.append((url, timeout))
            return Response()

    def build_opener(handler):
        assert handler.redirect_request(None, None, 302, "redirect", {}, "http://other.test/") is None
        return Opener()

    monkeypatch.setattr(doctor.urllib.request, "build_opener", build_opener)

    checks = doctor._telemetry_checks()

    assert opened == [("http://127.0.0.1:4321/healthz", 2)]
    assert checks[-1]["status"] == "OK"
