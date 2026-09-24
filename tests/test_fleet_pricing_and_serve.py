"""Fleet auditor: generation-aware price overrides and a locked-down dashboard server."""

import http.client
import json
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "fleet-auditor" / "scripts"))

import fleet  # noqa: E402


def test_generation_override_does_not_leak_onto_the_family(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, "FLEET_DB_DIR", tmp_path)
    monkeypatch.setattr(fleet, "_pricing_override", None)
    (tmp_path / "pricing.json").write_text(json.dumps({"opus-5-5": {"input": 1e-6, "output": 2e-6}}))
    pricing = fleet._load_pricing()
    assert pricing["opus-5-5"]["input"] == pytest.approx(1e-6)
    assert pricing["opus"]["input"] != pytest.approx(1e-6)
    assert fleet._pricing_key("claude-opus-5-5") == "opus-5-5"
    monkeypatch.setattr(fleet, "_pricing_override", None)


def test_dashboard_server_serves_only_the_page(tmp_path, monkeypatch):
    page = tmp_path / "fleet-dashboard.html"
    page.write_text("<h1>fleet</h1>")
    (tmp_path / "fleet.db").write_text("private")
    monkeypatch.setattr(fleet, "FLEET_DASHBOARD_PATH", page)
    port = 18000 + (hash(str(tmp_path)) % 1000)
    threading.Thread(target=fleet._serve_dashboard, args=("127.0.0.1", port), daemon=True).start()
    time.sleep(0.4)

    def status(path, host):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path, headers={"Host": host})
        return conn.getresponse().status

    assert status("/fleet-dashboard.html", f"127.0.0.1:{port}") == 200
    assert status("/", f"localhost:{port}") == 200
    assert status("/fleet.db", f"127.0.0.1:{port}") == 404
    assert status("/pricing.json", f"127.0.0.1:{port}") == 404
    assert status("/fleet-dashboard.html", f"attacker.example:{port}") == 403
