"""The persistent browser profile, the stage 2 session check, and the one-time manual login.

You log in yourself in a visible window; the profile keeps the session. No LinkedIn password is ever stored.
"""

import logging
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from pipeline.config import Browser, Config
from pipeline.context import Halt, Paths, RunContext
from pipeline.linkedin.checks import detect_problem, has_session_cookie, require_sales_navigator
from pipeline.linkedin.pacing import Pacer


class LinkedInBrowser:
    def __init__(self, playwright: Playwright, context: BrowserContext, page: Page):
        self.playwright = playwright
        self.context = context
        self.page = page

    @classmethod
    def open(cls, paths: Paths, settings: Browser, headless: bool | None = None) -> "LinkedInBrowser":
        playwright = sync_playwright().start()
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(paths.browser_profile_dir),
                channel="chrome" if settings.channel == "chrome" else None,
                headless=settings.headless if headless is None else headless,
                chromium_sandbox=True,  # Playwright disables Chrome's sandbox by default; keep it on
                viewport={"width": 1440, "height": 900},
            )
        except Exception:
            playwright.stop()
            raise
        context.set_default_timeout(settings.page_timeout_seconds * 1000)
        page = context.pages[0] if context.pages else context.new_page()
        return cls(playwright, context, page)

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            self.playwright.stop()


def session_check(ctx: RunContext) -> None:
    """Stage 2: open the saved profile, load Sales Navigator, and halt on anything but a normal logged-in page."""
    if ctx.dry_run:
        ctx.log.info("dry run: not opening LinkedIn")
        return
    settings = ctx.config.icp.browser
    browser = LinkedInBrowser.open(ctx.paths, settings)
    ctx.on_close(browser.close)
    pacer = Pacer(browser.page, ctx.budget, ctx.config.icp.caps.delay_seconds, ctx.output_dir, ctx.log)
    pacer.goto(settings.start_url)
    pacer.require_sales_navigator()
    ctx.state["pacer"] = pacer
    ctx.log.info("Sales Navigator session ok")


class LoginFailed(Exception):
    pass


def login(paths: Paths, config: Config, timeout_seconds: int = 900, headless: bool = False) -> None:
    """Open a visible window on Sales Navigator and wait for you to finish logging in."""
    settings = config.icp.browser
    browser = LinkedInBrowser.open(paths, settings, headless=headless)
    try:
        browser.page.goto(settings.start_url, wait_until="domcontentloaded")
        print("Log in to Sales Navigator in the browser window (including any verification).", flush=True)
        print(f"Waiting up to {timeout_seconds // 60} minutes. The window closes by itself once you're in.", flush=True)
        deadline = time.monotonic() + timeout_seconds
        last_path, logged_in_polls = None, 0
        while time.monotonic() < deadline:
            time.sleep(2)
            page = _current_page(browser.context)
            if page is None:
                raise LoginFailed("the browser window was closed before login finished")
            state = _login_state(browser.context, page)
            if state is None:
                continue  # mid-navigation; look again on the next poll
            path, logged_in = state
            if path != last_path:
                print(f"  at {path}", flush=True)
                last_path = path
            if not logged_in and _signed_in_elsewhere(browser.context, page):
                # LinkedIn often lands you on its main site after sign-in; take you back to Sales Navigator.
                print("  signed in; opening Sales Navigator", flush=True)
                try:
                    page.goto(settings.start_url, wait_until="domcontentloaded")
                except Exception:
                    pass  # checked again on the next poll
                continue
            # Two polls in a row, so a page that flashes by during redirects doesn't count.
            logged_in_polls = logged_in_polls + 1 if logged_in else 0
            if logged_in_polls >= 2:
                print(f"Logged in. The session is saved in {paths.browser_profile_dir}.", flush=True)
                return
        raise LoginFailed(f"not logged in after {timeout_seconds // 60} minutes (last page: {last_path})")
    finally:
        browser.close()


def _current_page(context: BrowserContext) -> Page | None:
    """The newest open tab, since sign-in can open new ones. None once you've closed them all."""
    open_pages = [p for p in context.pages if not p.is_closed()]
    return open_pages[-1] if open_pages else None


def _login_state(context: BrowserContext, page: Page) -> tuple[str, bool] | None:
    try:
        url = page.url
        text = page.inner_text("body", timeout=2_000)
        html = page.content()
        cookies = context.cookies(url)
    except Exception:
        return None
    logged_in = (
        has_session_cookie(cookies)
        and require_sales_navigator(url, html) is None
        and detect_problem(url, None, text) is None
    )
    return urlparse(url).path or "/", logged_in


def _signed_in_elsewhere(context: BrowserContext, page: Page) -> bool:
    """Has a session cookie but is on a normal non-Sales Navigator page (not a login or verification step)."""
    try:
        url = page.url
        text = page.inner_text("body", timeout=2_000)
        cookies = context.cookies(url)
    except Exception:
        return False
    return (
        has_session_cookie(cookies)
        and require_sales_navigator(url) is not None
        and detect_problem(url, None, text) is None
    )


def check_session(paths: Paths, config: Config) -> tuple[bool, str]:
    """Load Sales Navigator once, outside any run, and report whether a run would pass stage 2."""
    browser = LinkedInBrowser.open(paths, config.icp.browser)
    try:
        pacer = _standalone_pacer(browser, paths, config)
        pacer.goto(config.icp.browser.start_url)
        pacer.require_sales_navigator()
        return True, "Sales Navigator session ok"
    except Halt as halt:
        where = f" (screenshot: {halt.screenshot_path})" if halt.screenshot_path else ""
        return False, f"{halt.reason}{where}"
    finally:
        browser.close()


def capture(paths: Paths, config: Config, url: str, name: str) -> Path:
    """Save a LinkedIn page's HTML and screenshot as a local test fixture. Captures hold real people's data,
    so they go to tests/fixtures/captured/, which git ignores."""
    browser = LinkedInBrowser.open(paths, config.icp.browser)
    try:
        pacer = _standalone_pacer(browser, paths, config)
        pacer.goto(config.icp.browser.start_url)
        pacer.require_sales_navigator()
        pacer.goto(url)
        out_dir = paths.captured_fixtures_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        html_path = out_dir / f"{name}.html"
        html_path.write_text(browser.page.content())
        browser.page.screenshot(path=out_dir / f"{name}.png", full_page=True)
        return html_path
    finally:
        browser.close()


def _standalone_pacer(browser: LinkedInBrowser, paths: Paths, config: Config) -> Pacer:
    halt_dir = paths.output_dir / date.today().isoformat()
    return Pacer(browser.page, None, config.icp.caps.delay_seconds, halt_dir, logging.getLogger("leadgen"))
