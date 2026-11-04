import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from beauty_leads.demo import run_demo
from beauty_leads.http import HttpClient


class Handler(BaseHTTPRequestHandler):
    hits: dict = {}

    def do_GET(self):
        Handler.hits[self.path] = Handler.hits.get(self.path, 0) + 1
        if self.path == "/robots.txt":
            body, code = b"User-agent: *\nDisallow: /private\n", 200
        elif self.path == "/flaky":
            code = 503 if Handler.hits[self.path] == 1 else 200
            body = b"ok"
        else:
            body, code = b"<html><body>hello</body></html>", 200
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    Handler.hits = {}
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_http_cache_robots_retry(cfg, tmp_path, server, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    cfg.raw["http"]["per_host_delay_seconds"] = 0
    http = HttpClient(cfg, cache_path=tmp_path / "cache.sqlite")
    r1 = http.get(server + "/page", crawl=True)
    r2 = http.get(server + "/page", crawl=True)
    assert r1.ok and "hello" in r1.text and not r1.from_cache
    assert r2.from_cache and Handler.hits["/page"] == 1
    blocked = http.get(server + "/private/x", crawl=True)
    assert blocked.error == "blocked by robots.txt" and "/private/x" not in Handler.hits
    assert http.get(server + "/flaky").ok and Handler.hits["/flaky"] == 2
    offline = HttpClient(cfg, cache_path=tmp_path / "cache.sqlite", offline=True)
    assert offline.get(server + "/page").from_cache
    assert offline.get(server + "/never").error.startswith("offline")


def test_demo_end_to_end(tmp_path):
    paths = run_demo(tmp_path / "out")
    for key in ("leads_csv", "accounts_csv", "xlsx", "html", "summary_json", "registry_only_csv",
                "outreach_csv", "outreach_email_csv", "outreach_instagram_csv"):
        assert paths[key].exists(), key
    html = paths["html"].read_text()
    assert "<title>BC Beauty Leads</title>" in html and "Demo data" in html
    leads = paths["leads_csv"].read_text().splitlines()
    assert len(leads) == 38  # header + 37 fictional locations (one OSM + one licence record merged)
    from openpyxl import load_workbook
    wb = load_workbook(paths["xlsx"], read_only=True)
    assert {"Email outreach", "Instagram outreach", "Leads"} <= set(wb.sheetnames)
    assert "Copy" in html and "Reach via" in html
    reg = paths["registry_only_csv"].read_text()
    assert "PRAIRIE ROSE NAILS" in reg and "FADEHOUSE" not in reg
