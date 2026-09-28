"""Drives real Chrome against a local server that serves LinkedIn-like pages. Never touches LinkedIn.

Run only these with `uv run pytest -m browser`; skip them with `-m "not browser"`.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from pipeline.config import load_config
from pipeline.linkedin.session import LoginFailed, LinkedInBrowser, login
from pipeline.run import start_run
from pipeline.stages import STAGES, Stage
from tests.linkedin_pages import fixture_html

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not Path("/Applications/Google Chrome.app").exists(), reason="Google Chrome not installed"),
]

REDIRECTS = {
    "/sales/home-logged-out": "/checkpoint/challenge/AgE",
    "/start-login": "/sales/login",
}
PAGES = {
    "/checkpoint/challenge/AgE": fixture_html("checkpoint"),
    "/sales/home-no-cookie": fixture_html("sales_home"),
    # Sign-in hops through several pages, like LinkedIn's sign-in -> verification -> Sales Navigator.
    "/sales/login": "<body>Sign in<script>setTimeout(() => location = '/hop1', 2500)</script></body>",
    "/hop1": "<body>...<script>setTimeout(() => location = '/hop2', 300)</script></body>",
    "/hop2": "<body>...<script>setTimeout(() => location = '/sales/home', 300)</script></body>",
    # Sign-in that ends on LinkedIn's main site instead of Sales Navigator.
    "/login-to-feed": "<body>Sign in<script>setTimeout(() => location = '/feed/', 1500)</script></body>",
}


class FakeLinkedIn(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in REDIRECTS:
            self.send_response(302)
            self.send_header("Location", REDIRECTS[self.path])
            self.end_headers()
            return
        signed_in = "li_at=" in (self.headers.get("Cookie") or "")
        if self.path == "/sales/home-gated" and not signed_in:
            self.send_response(302)
            self.send_header("Location", "/login-to-feed")
            self.end_headers()
            return
        if self.path in ("/sales/home", "/sales/home-gated"):
            body = fixture_html("sales_home")
        elif self.path == "/feed/":
            body = "<body>Home feed</body>"
        else:
            body = PAGES.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if self.path in ("/sales/home", "/feed/"):
            self.send_header("Set-Cookie", "li_at=fake-session; Path=/")
        self.end_headers()
        self.wfile.write((body or "not found").encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeLinkedIn)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def session_only():
    """Real stages 1-2; later stages would need a full fake Sales Navigator."""
    return [s if s.number <= 2 else Stage(s.number, s.name, lambda ctx: None, s.uses_linkedin) for s in STAGES]


def point_browser_at(paths, start_url):
    icp_path = paths.config_dir / "icp.yaml"
    data = yaml.safe_load(icp_path.read_text())
    data["browser"].update(headless=True, start_url=start_url, page_timeout_seconds=15)
    data["caps"]["delay_seconds"] = [0.01, 0.02]
    icp_path.write_text(yaml.safe_dump(data))
    return load_config(paths.config_dir)


def test_session_check_passes_on_sales_navigator(paths, server):
    point_browser_at(paths, f"{server}/sales/home")
    run = start_run(paths, stages=session_only())
    assert run.status == "completed"
    assert (paths.browser_profile_dir / "Default").exists()
    (trace,) = (paths.output_dir / run.started_at[:10] / f"run{run.id}").glob("browser-trace-*.zip")
    assert trace.stat().st_size > 1000
    log = (paths.output_dir / run.started_at[:10] / "run.log").read_text()
    assert "opened /sales/home after a" in log and "checks ok" in log


def test_session_check_halts_on_redirect_to_checkpoint(paths, server):
    point_browser_at(paths, f"{server}/sales/home-logged-out")
    run = start_run(paths, stages=session_only())
    assert run.status == "halted"
    assert run.halt_reason == "LinkedIn security checkpoint"
    screenshot = Path(run.halt_screenshot_path)
    assert screenshot.stat().st_size > 1000
    assert "quick security check" in screenshot.with_suffix(".html").read_text()


def test_session_check_halts_without_session_cookie(paths, server):
    point_browser_at(paths, f"{server}/sales/home-no-cookie")
    run = start_run(paths, stages=session_only())
    assert run.status == "halted"
    assert "no LinkedIn session cookie" in run.halt_reason


def test_login_waits_through_redirects_until_session_cookie(paths, server, capsys):
    config = point_browser_at(paths, f"{server}/start-login")
    login(paths, config, timeout_seconds=60, headless=True)
    out = capsys.readouterr().out
    assert "at /sales/login" in out
    assert "Logged in" in out


def test_login_returns_to_sales_navigator_when_sign_in_lands_on_the_main_site(paths, server, capsys):
    config = point_browser_at(paths, f"{server}/sales/home-gated")
    login(paths, config, timeout_seconds=60, headless=True)
    out = capsys.readouterr().out
    assert "opening Sales Navigator" in out
    assert "Logged in" in out


def test_login_does_not_accept_a_sales_page_without_session_cookie(paths, server, capsys):
    config = point_browser_at(paths, f"{server}/sales/home-no-cookie")
    with pytest.raises(LoginFailed, match="last page: /sales/home-no-cookie"):
        login(paths, config, timeout_seconds=8, headless=True)


def test_login_reports_a_closed_window(paths, server, monkeypatch):
    config = point_browser_at(paths, f"{server}/sales/home-no-cookie")
    opened = []
    original_open = LinkedInBrowser.open.__func__

    def remember_open(cls, *args, **kwargs):
        opened.append(original_open(cls, *args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(LinkedInBrowser, "open", classmethod(remember_open))
    # You close the window during the first wait.
    monkeypatch.setattr("pipeline.linkedin.session.time.sleep", lambda seconds: opened[0].page.close())
    with pytest.raises(LoginFailed, match="closed before login finished"):
        login(paths, config, timeout_seconds=30, headless=True)


def test_searches_halt_when_the_saved_searches_link_is_missing(paths, server):
    point_browser_at(paths, f"{server}/sales/home")  # the fake home page has no Saved searches link
    stages = [s if s.number <= 3 else Stage(s.number, s.name, lambda ctx: None, s.uses_linkedin) for s in STAGES]
    run = start_run(paths, stages=stages)
    assert run.status == "halted"
    assert "Saved searches link wasn't found" in run.halt_reason
