"""Phase 9: headless-browser walkthrough of every page against a live in-process server.

Job sources and Claude are faked, so nothing leaves the machine. The test fails on any console error,
uncaught page error, or failed request. It is skipped where no headless Chromium is available.
"""
import glob
import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from app import config, db, forge, security, settings
from app.adapter import export, llm
from app.main import app
from app.scout.http import ScoutClient
from fakes import ComboClient
from test_phase7 import CV

playwright_sync = pytest.importorskip("playwright.sync_api")
FIXTURES = Path(__file__).parent / "fixtures"


def find_chromium() -> str | None:
    patterns = [
        "~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell",
        "~/.cache/ms-playwright/chromium-*/*/chrome",
    ]
    for pattern in patterns:
        hits = sorted(glob.glob(str(Path(pattern).expanduser())))
        if hits:
            return hits[-1]
    return None


CHROMIUM = find_chromium()
pytestmark = pytest.mark.skipif(CHROMIUM is None, reason="no headless Chromium found")

XSS_TITLE = "Machine Learning Engineer Intern <img src=x onerror=window.__xss=1>"


class Web:
    """Mock internet for the job sources; `gate` can hold requests to simulate a slow scan."""

    def __init__(self) -> None:
        self.gate: threading.Event | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.gate is not None:
            self.gate.wait(15)
        host = request.url.host
        if host == "boards-api.greenhouse.io":
            return httpx.Response(200, json=json.loads((FIXTURES / "greenhouse.json").read_text()))
        if host == "api.lever.co":
            return httpx.Response(200, json=json.loads((FIXTURES / "lever.json").read_text()))
        if host == "remoteok.com":
            return httpx.Response(200, json=[
                {"legal": "notice"},
                {"id": "x1", "position": XSS_TITLE, "company": "<script>window.__xss=2</script>Evil", "location": "Remote - US",
                 "description": "<p>We use Python and PyTorch.</p><script>window.__xss=3</script>", "url": "javascript:window.__xss=4",
                 "date": "2026-10-30T10:00:00+00:00"}])
        return httpx.Response(404, json={})


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def live(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    mp = pytest.MonkeyPatch()
    port = free_port()
    mp.setattr(config, "DATA_DIR", tmp_path_factory.mktemp("data"))
    mp.setenv("ANTHROPIC_API_KEY", "sk-ant-UITEST")
    host = f"127.0.0.1:{port}"
    mp.setattr(security, "ALLOWED_HOSTS", {host})
    mp.setattr(security, "ALLOWED_ORIGINS", {f"http://{host}"})
    web = Web()
    mp.setattr(forge, "make_client", lambda: ScoutClient(transport=httpx.MockTransport(web.handler), sleep=lambda s: None))
    mp.setattr(llm, "get_client", lambda: ComboClient())
    mp.setattr(settings, "make_client", lambda: ScoutClient(transport=httpx.MockTransport(web.handler), sleep=lambda s: None))
    mp.setattr(export, "convert_to_pdf", lambda p: (p.with_suffix(".pdf").write_bytes(b"%PDF-1.4 fake"), p.with_suffix(".pdf"))[1])
    db.init_db()
    (config.DATA_DIR / "boards.yaml").write_text("greenhouse:\n  - {slug: acme, name: Acme Inc}\nlever:\n  - {slug: beta, name: Beta}\n")
    with db.connect() as conn:
        db.set_setting(conn, "profile", {"enabled_sources": ["greenhouse", "lever", "remoteok"], "max_age_days": 100000})
        db.set_setting(conn, "claude_prices", {"input_per_mtok": 3.0, "output_per_mtok": 15.0})
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield {"base": f"http://{host}", "web": web}
    server.should_exit = True
    thread.join(5)
    mp.undo()


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with playwright_sync.sync_playwright() as p:
        instance = p.chromium.launch(executable_path=CHROMIUM, headless=True)
        yield instance
        instance.close()


def test_every_page_works_end_to_end(live: dict[str, Any], browser: Any) -> None:
    base, web = live["base"], live["web"]
    context = browser.new_context(viewport={"width": 1024, "height": 768}, accept_downloads=True)
    page = context.new_page()
    problems: list[str] = []
    page.on("console", lambda m: problems.append(f"console {m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("requestfailed", lambda r: problems.append(f"requestfailed: {r.url}"))
    page.on("dialog", lambda d: (problems.append(f"dialog: {d.message}"), d.dismiss()))

    def no_overflow(where: str) -> None:
        width = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
        assert width[0] <= width[1], f"horizontal overflow on {where}: {width}"

    def wait_until(predicate: Any, timeout: float = 20.0) -> None:
        """Poll from Python: the page's Content Security Policy forbids Playwright's string-based waits."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            page.wait_for_timeout(50)
        raise AssertionError("condition not met in time")

    def shot(name: str) -> None:
        """Set FORGE_SCREENSHOTS=/some/dir to save a screenshot of each stage for a visual check."""
        folder = os.environ.get("FORGE_SCREENSHOTS")
        if folder:
            Path(folder).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(folder) / f"{name}.png"), full_page=True)

    def clean_text(where: str) -> None:
        """No page, dialog, or drawer may show leaked JavaScript values."""
        text = page.inner_text("body")
        for bad in ("null", "undefined", "[object", "NaN"):
            assert bad not in text, f"'{bad}' shown on {where}"

    def pill() -> str:
        return page.inner_text("#run-pill")

    def row_count() -> int:
        return len(page.query_selector_all("#jobs-table tbody tr"))

    def finished_scan() -> None:
        wait_until(lambda: pill().startswith("LAST FIND"))

    # --- shell and an empty Overview --------------------------------------------------------------
    page.goto(base + "/")
    assert page.title().startswith("Forge")
    assert page.inner_text(".brand") == "FORGE"
    assert [a.inner_text() for a in page.query_selector_all("#nav a")] == ["Overview", "Jobs", "CV", "Settings"]
    page.wait_for_selector(".agent")
    assert len(page.query_selector_all(".agent")) == 4 and page.inner_text("#run-pill") == "NO SCAN YET"
    assert "Nothing has happened yet" in page.inner_text("#view")
    assert "Remote OK" in page.inner_text("#footer") and "The Muse" in page.inner_text("#footer")
    no_overflow("overview (empty)")
    clean_text("overview (empty)")

    # --- Jobs: scan, progress that survives a reload, ranked table ------------------------------------
    page.click("#nav a[data-page=jobs]")
    page.wait_for_selector("#scan-btn")
    assert "No scan has run yet" in page.inner_text("#scan-card")
    web.gate = threading.Event()
    page.click("#scan-btn")
    wait_until(lambda: pill() == "RUNNING...")
    page.wait_for_selector(".source-chip.running")
    page.reload()  # a page reload mid-scan still shows the live state
    wait_until(lambda: pill() == "RUNNING...")
    page.wait_for_selector(".source-chip")
    assert page.is_disabled("#scan-btn") and len(page.query_selector_all(".source-chip")) == 3
    web.gate.set()
    finished_scan()
    page.wait_for_selector("#jobs-table tbody tr")
    assert {c.get_attribute("data-source") for c in page.query_selector_all(".source-chip.done")} == {"greenhouse", "lever", "remoteok"}
    rows = page.query_selector_all("#jobs-table tbody tr")
    assert len(rows) == 3  # the sales role and the office manager do not pass the first cut
    titles = [r.query_selector("td:nth-child(3)").inner_text() for r in rows]
    assert XSS_TITLE in titles  # shown as text, never run as HTML
    assert page.evaluate("window.__xss") is None
    assert page.query_selector("#jobs-table img") is None and page.query_selector("#view script") is None
    assert page.query_selector("#jobs-table a[href^='javascript']") is None
    assert page.query_selector("td:nth-child(3) >> text=<img") is not None
    assert all(r.query_selector(".pill.info") for r in rows)  # NEW on the first scan
    assert page.inner_text("#scan-card").count("Last scan:") == 1
    shot("jobs")
    no_overflow("jobs")
    clean_text("jobs")

    # status dropdown persists across a reload
    select = page.query_selector("#jobs-table tbody tr select")
    first_key = page.query_selector("#jobs-table tbody tr").get_attribute("data-key")
    select.select_option("interested")
    page.wait_for_timeout(300)
    page.reload()
    page.wait_for_selector("#jobs-table tbody tr")
    assert page.query_selector(f"tr[data-key='{first_key}'] select").input_value() == "interested"

    # search and "new only" filters
    page.fill("#filter-q", "scientist")
    wait_until(lambda: row_count() == 1)
    page.fill("#filter-q", "")
    wait_until(lambda: row_count() == 3)
    page.check("#filter-new")
    page.wait_for_timeout(300)
    assert len(page.query_selector_all("#jobs-table tbody tr")) == 3
    page.uncheck("#filter-new")

    # job description drawer
    page.click("#jobs-table tbody tr:first-child button:has-text('JD')")
    page.wait_for_selector(".drawer")
    wait_until(lambda: "Loading" not in page.inner_text(".drawer"))
    drawer_text = page.inner_text(".drawer").lower()  # headings are upper-cased by CSS
    assert "not been read yet" in drawer_text and "description" in drawer_text and "build ml systems" in drawer_text
    clean_text("job drawer")
    page.keyboard.press("Escape")
    page.wait_for_selector(".drawer", state="detached")

    # paste text (read for free), then a pasted link that cannot be read asks for text
    page.fill("#paste-url", "")
    page.click("button:has-text('Paste text instead')")
    page.fill("#paste-text", "Data Scientist, Growth\nWe need Python, SQL and A/B testing.\nRequirements\n- Python and SQL\nUS only.")
    page.fill("#paste-company", "Beta Labs")
    page.click("#paste-add")
    page.wait_for_selector("#paste-message .pill.good")
    wait_until(lambda: row_count() == 4)
    assert "Data Scientist, Growth" in page.inner_text("#jobs-table")
    no_overflow("jobs with pasted job")

    # --- CV: upload, review, sanitized preview, cost, confirm, build -----------------------------------------
    page.click("#nav a[data-page=cv]")
    page.wait_for_selector("#cv-drop")
    assert "No base CV yet" in page.inner_text("#view")
    page.set_input_files("#cv-file", files=[{"name": "resume.txt", "mimeType": "text/plain", "buffer": CV.encode()}])
    page.wait_for_selector("#pending-card")
    assert page.input_value("#name-input") == "Jane Q. Doe"
    kinds = {i.get_attribute("data-kind") for i in page.query_selector_all("#contact-items .item")}
    assert {"name", "email", "phone", "linkedin", "github", "address"} <= kinds
    assert page.is_disabled("#build-btn")
    page.click("#preview-btn")
    page.wait_for_selector("#sanitized-text")
    preview = page.inner_text("#sanitized-text")
    assert "[NAME]" in preview and "[EMAIL]" in preview and "jane.doe@example.com" not in preview and "Jane" not in preview
    assert page.query_selector("#sanitized-text mark") is not None
    page.click("#estimate-btn")
    page.wait_for_selector("#estimate-box")
    assert "Estimated cost: $" in page.inner_text("#estimate-box")
    page.check("#confirm-cost")
    assert page.is_disabled("#build-btn")  # both confirmations are needed
    page.check("#confirm-contacts")
    assert not page.is_disabled("#build-btn")
    shot("cv_review")
    no_overflow("cv (review)")
    clean_text("cv (review)")
    page.click("#build-btn")
    wait_until(lambda: any(h2.inner_text().lower() == "base cv" for h2 in page.query_selector_all("#view h2")))
    assert "Jane Q. Doe" in page.inner_text("#view") and "Acme Corp" in page.inner_text("#view")
    assert "review before sending" not in page.inner_text("#view").lower()
    shot("cv_base")
    no_overflow("cv (base)")
    clean_text("cv (base)")

    # --- Jobs: tailor with a cost confirmation, CV view, downloads ---------------------------------------------
    page.click("#nav a[data-page=jobs]")
    page.wait_for_selector("#jobs-table tbody tr")
    row = page.query_selector("#jobs-table tbody tr:has-text('Machine Learning Engineer, New Grad')")
    key = row.get_attribute("data-key")
    row.query_selector("button:has-text('Tailor')").click()
    page.wait_for_selector("#confirm-tailor")
    dialog_text = page.inner_text(".modal")
    assert "Estimated cost: $" in dialog_text and "Claude rewrites only your headline" in dialog_text
    page.click("#confirm-tailor")
    page.wait_for_selector(".modal.wide .cv-preview", timeout=20000)
    assert "Passed validation" in page.inner_text(".modal.wide") and "Jane Q. Doe" in page.inner_text(".modal.wide")
    clean_text("tailored CV dialog")
    page.click(".modal.wide button:has-text('Close')")
    page.wait_for_selector(f"tr[data-key='{key}'] .pill.good:has-text('CV done')")
    docx_link = page.get_attribute(f"tr[data-key='{key}'] a:has-text('DOC')", "href")
    pdf_link = page.get_attribute(f"tr[data-key='{key}'] a:has-text('PDF')", "href")
    docx = page.request.get(base + docx_link)
    pdf = page.request.get(base + pdf_link)
    assert docx.status == 200 and docx.body()[:2] == b"PK" and pdf.status == 200 and pdf.body().startswith(b"%PDF")
    page.click(f"tr[data-key='{key}'] button:has-text('View')")
    page.wait_for_selector(".modal.wide")
    assert "Reused from earlier" in page.inner_text(".modal.wide")
    page.keyboard.press("Escape")
    no_overflow("jobs after tailoring")

    # --- Overview with real data ---------------------------------------------------------------------------------
    page.click("#nav a[data-page=overview]")
    page.wait_for_selector(".stat .num")
    nums = [int(n.inner_text().replace(",", "")) for n in page.query_selector_all(".stat .num")]
    assert nums[0] == 5 and nums[1] >= 3 and nums[3] == 1 and nums[4] == 4  # found, matched, ..., tailored, known
    assert len(page.query_selector_all(".funnel-row")) == 4 and len(page.query_selector_all(".stream-row")) >= 5
    statuses = {c.get_attribute("data-agent"): c.query_selector(".small").inner_text() for c in page.query_selector_all(".agent")}
    assert set(statuses) == {"forge", "scout", "job-reader", "cv-adapter"} and all("LIVE" in s for s in statuses.values())
    assert "Claude" not in page.inner_text(".agent-forge .last") and page.inner_text(".agent-cv-adapter .kv:nth-child(5) strong") != "no LLM"
    shot("overview")
    no_overflow("overview (with data)")
    clean_text("overview (with data)")

    # --- Settings: dirty tracking, chips, sliders, sources, reset, boards, Claude ------------------------------------
    page.click("#nav a[data-page=settings]")
    page.wait_for_selector("#save-bar #save-btn")
    assert page.is_disabled("#save-btn") and "All changes saved" in page.inner_text("#save-bar")
    assert page.inner_text("#claude-model") == "claude-sonnet-5-5" and page.inner_text("#claude-key") == "key set"
    assert "key not set" in page.inner_text("[data-key=adzuna]")  # status only: the key itself is never shown
    assert "UITEST" not in page.content()
    shot("settings")
    no_overflow("settings")
    clean_text("settings")

    page.click("#seniority button[data-level=mid]")
    assert not page.is_disabled("#save-btn") and "Unsaved changes" in page.inner_text("#save-bar")
    page.click("#discard-btn")
    assert page.is_disabled("#save-btn") and "entry" == page.query_selector("#seniority button.on").get_attribute("data-level")

    page.click("#seniority button[data-level=mid]")
    page.fill("input[data-chips=keywords]", "robotics")
    page.press("input[data-chips=keywords]", "Enter")
    assert "robotics" in page.inner_text("[data-field=keywords]")
    page.evaluate("""() => { const el = document.querySelector('#first_cut-range'); el.value = '80'; el.dispatchEvent(new Event('input', {bubbles: true})); }""")
    assert page.inner_text("#first_cut-value") == "80" and page.inner_text("#final-value") == "80"  # final follows the first cut up
    page.evaluate("""() => { const el = document.querySelector('#final-range'); el.value = '10'; el.dispatchEvent(new Event('input', {bubbles: true})); }""")
    assert page.inner_text("#final-value") == "80"  # and cannot be dragged below it
    page.evaluate("""() => { const el = document.querySelector('#first_cut-range'); el.value = '55'; el.dispatchEvent(new Event('input', {bubbles: true})); }""")
    page.fill("#num-pages", "9")
    page.dispatch_event("#num-pages", "change")
    assert page.input_value("#num-pages") == "5"  # clamped
    for name in ("greenhouse", "lever", "ashby", "workable", "adzuna", "remotive", "remoteok", "themuse", "jobicy"):
        page.uncheck(f"#src-{name}")
    assert page.is_disabled("#save-btn") and "at least one source" in page.inner_text("#save-issue")
    page.check("#src-greenhouse")
    page.check("#src-lever")
    page.check("#src-remoteok")
    page.fill("#monthly-cap", "25")
    assert not page.is_disabled("#save-btn")
    page.click("#save-btn")
    wait_until(lambda: "All changes saved" in page.inner_text("#save-bar"))
    assert "Settings saved" in page.inner_text("#toasts")

    page.reload()
    page.wait_for_selector("#save-bar #save-btn")
    assert page.query_selector("#seniority button.on").get_attribute("data-level") == "mid"
    assert "robotics" in page.inner_text("[data-field=keywords]") and page.inner_text("#final-value") == "80"
    assert page.input_value("#num-pages") == "5" and page.input_value("#monthly-cap") == "25"
    assert page.is_checked("#src-greenhouse") and not page.is_checked("#src-adzuna")
    page.click("[data-field=keywords] >> xpath=ancestor::div[contains(@class,'field')] >> button:has-text('Reset to default')")
    wait_until(lambda: "robotics" not in page.inner_text("[data-field=keywords]"))
    assert "machine learning" in page.inner_text("[data-field=keywords]")
    assert page.query_selector("#seniority button.on").get_attribute("data-level") == "mid"  # other settings are untouched

    # boards editor: edit, save, and check a slug
    page.fill("#boards-yaml", "# edited in the UI\ngreenhouse:\n  - {slug: acme, name: Acme Inc}\n")
    page.click("#boards-save")
    page.wait_for_selector("#boards-message .pill.good")
    page.fill("#check-slug", "acme")
    page.click("#check-btn")
    wait_until(lambda: "Found 2 open jobs" in page.inner_text("#check-result"))
    page.reload()
    page.wait_for_selector("#boards-yaml")
    assert "# edited in the UI" in page.input_value("#boards-yaml")
    assert "Data folder" in page.inner_text("#view") and "scheduler" in page.inner_text("#view").lower()
    no_overflow("settings (edited)")
    clean_text("settings (edited)")

    assert problems == [], problems
    context.close()
