"""Phase 11: hardening checks that guard the rules in CLAUDE.md (isolation, secrets, LLM use, egress)."""
import ast
import json
import re
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, db, forge
from app.adapter import export, llm
from app.main import app
from app.reader import safe_fetch
from fakes import ComboClient
from test_phase7 import CV

ROOT = config.APP_ROOT
APP_FILES = sorted((ROOT / "app").rglob("*.py"))
HEADERS = {"X-Forge-Request": "1"}
FIXTURES = Path(__file__).parent / "fixtures"
SECRETS = {
    "GROQ_API_KEY": "gsk_HARDENING-secret-0001", "ADZUNA_APP_ID": "adzuna-id-HARDENING-0002",
    "ADZUNA_APP_KEY": "adzuna-key-HARDENING-0003", "RAPIDAPI_KEY": "rapid-HARDENING-0004",
}

# Hosts the app may name: the job-source APIs in BUILD_SPEC.md (the Anthropic API host is the SDK's default).
ALLOWED_HOSTS = {
    "boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com", "apply.workable.com", "api.adzuna.com",
    "remotive.com", "remoteok.com", "www.themuse.com", "jobicy.com", "api.groq.com",
}
ALLOWED_ENV = {"GROQ_API_KEY", "GROQ_MODEL", "CLAUDE_MODEL", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "FORGE_DATA_DIR", "HOME"}


def imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


# --- network egress: only the allowed hosts, only through the audited clients -------------------------------

def test_every_url_in_the_code_points_at_an_allowed_host() -> None:
    found: dict[str, set[str]] = {}
    for path in APP_FILES:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.match(r"https?://[^/\s]", node.value):
                found.setdefault(urlsplit(node.value).hostname or "", set()).add(rel(path))
    assert set(found) <= ALLOWED_HOSTS, {h: sorted(f) for h, f in found.items() if h not in ALLOWED_HOSTS}
    assert {"boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com", "apply.workable.com", "api.adzuna.com"} <= set(found)


def test_the_browser_code_only_talks_to_this_app() -> None:
    for path in (ROOT / "app" / "static").glob("*.js"):
        code = path.read_text()
        assert not re.search(r"""["'`]https?://[a-z0-9]""", code, re.IGNORECASE), f"{path.name} names an external host"
        for call in re.findall(r"fetch\(([^,)]+)", code):
            assert call.strip().startswith(("path", "response")) or "/api/" in call, (path.name, call)
    html = (ROOT / "app" / "static" / "index.html").read_text()
    assert not re.search(r"""(src|href)=["']https?://""", html), "the page loads nothing from other sites"


def test_only_the_audited_modules_open_network_connections() -> None:
    forbidden = {"requests", "urllib.request", "http.client", "aiohttp", "ftplib", "smtplib", "telnetlib", "websockets"}
    for path in APP_FILES:
        used = imports(path)
        assert not (used & forbidden), f"{rel(path)} imports {sorted(used & forbidden)}"
        if rel(path) != "app/reader/safe_fetch.py":  # the only module that resolves host names itself (to vet them)
            assert "socket" not in used, rel(path)
    makers = {"app/scout/http.py", "app/reader/safe_fetch.py", "app/adapter/llm.py"}
    for path in APP_FILES:
        text = path.read_text()
        if re.search(r"httpx\.(Client|AsyncClient)\(", text):
            assert rel(path) in makers, f"{rel(path)} creates its own HTTP client"


def test_no_scheduler_messaging_or_telemetry_code() -> None:
    banned = {"smtplib", "email.mime", "email.message", "telegram", "apscheduler", "schedule", "sched", "celery", "crontab",
              "slack_sdk", "twilio", "sentry_sdk", "posthog", "mixpanel", "segment", "opentelemetry"}
    for path in APP_FILES:
        used = imports(path)
        assert not (used & banned), f"{rel(path)} imports {sorted(used & banned)}"
        assert "threading.Timer" not in path.read_text(), rel(path)
    for name in ("requirements.txt", "requirements-dev.txt"):
        text = (ROOT / name).read_text().lower()
        assert not any(b in text for b in ("apscheduler", "celery", "sentry", "telegram", "twilio", "schedule==")), name


def test_environment_variables_read_are_the_documented_ones() -> None:
    names: set[str] = set()
    for path in APP_FILES:
        names |= set(re.findall(r"""os\.environ(?:\.get)?[\[(]\s*["']([A-Z_]+)["']""", path.read_text()))
        names |= set(re.findall(r"""os\.getenv\(\s*["']([A-Z_]+)["']""", path.read_text()))
    assert names <= ALLOWED_ENV, sorted(names - ALLOWED_ENV)


# --- LLM use ---------------------------------------------------------------------------------------------------------

def test_claude_is_only_ever_called_from_llm_py() -> None:
    for path in APP_FILES:
        text = path.read_text()
        if rel(path) == "app/adapter/llm.py":
            continue
        assert "api.groq.com" not in text and "anthropic" not in imports(path), rel(path)
        assert not re.search(r"\.messages\.(create|parse|stream|count_tokens)\(", text), rel(path)
    callers = {rel(p) for p in APP_FILES if "llm.structured_call(" in p.read_text() or "llm.estimate(" in p.read_text()}
    assert callers <= {"app/adapter/cv_builder.py", "app/adapter/cv_tailor.py"}, callers
    # Scout, Job Reader, and Forge are deterministic Python: none of them may even import the adapter
    for path in list((ROOT / "app" / "scout").rglob("*.py")) + list((ROOT / "app" / "reader").rglob("*.py")) + [ROOT / "app" / "forge.py"]:
        assert not any(name.startswith("app.adapter") for name in imports(path)), rel(path)


# --- container and repository setup ----------------------------------------------------------------------------------------

def test_run_script_enforces_the_isolation_flags() -> None:
    script = (ROOT / "run.sh").read_text()
    for flag in ("-p 127.0.0.1:8000:8000", "--userns=keep-id", "--env-file .env", "--read-only", "--tmpfs /tmp",
                 "--cap-drop=all", "--security-opt no-new-privileges"):
        assert flag in script, flag
    mounts = re.findall(r"(?<![\w-])-v\s+(\S+)", script)
    assert len(mounts) == 1 and mounts[0].strip('"').endswith("/data:/data:Z"), mounts  # the only bind mount is ./data
    assert not re.search(r"--(privileged|network[= ]host|pid[= ]host|device|cap-add)", script)
    assert "0.0.0.0:8000" not in script and not re.search(r"-p\s+8000", script)


def test_containerfile_runs_as_a_non_root_user_and_copies_only_the_app() -> None:
    text = (ROOT / "Containerfile").read_text()
    assert re.search(r"^USER forge$", text, re.MULTILINE) and "useradd --uid 1000" in text
    sources = {token for line in re.findall(r"^COPY (.+)$", text, re.MULTILINE) for token in line.split()[:-1]}
    assert sources <= {"requirements.txt", "app", "prompts", "boards.yaml", "skills.yaml"}, sources  # never .env, data, tests, or .git
    assert "uvicorn" in text and "--host\", \"0.0.0.0\"" in text  # listens on all container interfaces, published to loopback only
    assert ".env" not in text and "GROQ" not in text


def test_secrets_are_ignored_by_git_and_not_tracked() -> None:
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignore and "data/" in ignore and "!.env.example" in ignore
    example = (ROOT / ".env.example").read_text()
    for name in ("GROQ_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "RAPIDAPI_KEY"):
        assert re.search(rf"^{name}=$", example, re.MULTILINE), name  # present and empty
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False).stdout.split()
    except FileNotFoundError:  # no git (for example in a slim test container): the file checks above still ran
        tracked = []
    if tracked:
        assert ".env" not in tracked and not [f for f in tracked if f.startswith("data/")]
        for name in tracked:
            if name.startswith("tests/") or name.endswith((".png", ".pdf", ".docx")):
                continue
            text = (ROOT / name).read_text(errors="ignore")
            assert not re.search(r"sk-ant-[A-Za-z0-9_-]{8,}", text), f"{name} looks like it contains an API key"


def test_dependencies_are_pinned() -> None:
    for name in ("requirements.txt", "requirements-dev.txt"):
        for line in (ROOT / name).read_text().splitlines():
            if line.strip() and not line.startswith(("#", "-r")):
                assert re.match(r"^[A-Za-z0-9_.\-\[\]]+==[0-9][\w.]*$", line.strip()), f"{name}: {line}"


# --- runtime: no secret leaks, no real connection, nothing unsafe is fetched -------------------------------------------------------

@pytest.fixture()
def secured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setitem(forge._state, "active", False)
    monkeypatch.setattr(llm, "get_client", lambda: ComboClient())
    monkeypatch.setattr(export, "convert_to_pdf", lambda p: None)

    def web(request: httpx.Request) -> httpx.Response:
        files = {"boards-api.greenhouse.io": "greenhouse.json", "api.lever.co": "lever.json", "api.adzuna.com": "adzuna.json"}
        name = files.get(request.url.host)
        if name == "adzuna.json" and request.url.params.get("sort_by") == "boom":
            return httpx.Response(500)
        if not name:
            return httpx.Response(404, json={})
        text = (FIXTURES / name).read_text().replace("utm_source=id123", f"utm_source={SECRETS['ADZUNA_APP_ID']}")  # Adzuna adds the app id to links
        return httpx.Response(200, json=json.loads(text))

    from app.scout.http import ScoutClient

    monkeypatch.setattr(forge, "make_client", lambda: ScoutClient(transport=httpx.MockTransport(web), sleep=lambda s: None))
    with TestClient(app, base_url="http://127.0.0.1:8000") as test_client:
        (config.DATA_DIR / "boards.yaml").write_text("greenhouse:\n  - {slug: acme, name: Acme}\nlever:\n  - {slug: beta, name: Beta}\n")
        test_client.post("/api/settings", json={"profile": {"enabled_sources": ["greenhouse", "lever", "adzuna"], "max_age_days": 365}}, headers=HEADERS)
        yield test_client


def walk_flows(client: TestClient) -> list[str]:
    """Exercise scan, add-job, CV build, tailoring, and settings; return every response body."""
    bodies: list[str] = []

    def record(resp: Any) -> Any:
        bodies.append(resp.text)
        return resp

    record(client.post("/api/run", headers=HEADERS))
    forge.wait_for_run(20)
    record(client.get("/api/run-status"))
    record(client.post("/api/add-job", json={"text": "Data Scientist\nRequirements\n- Python\nUS only. No visa sponsorship."}, headers=HEADERS))
    record(client.post("/api/add-job", json={"url": "https://127.0.0.1/admin"}, headers=HEADERS))
    record(client.post("/api/cv", params={"filename": "resume.txt"}, content=CV.encode(), headers=HEADERS))
    record(client.get("/api/cv/preview"))
    record(client.get("/api/cv/estimate"))
    from app.adapter import cv_builder

    record(client.post("/api/cv/build", json={"confirm_cost": True, "confirm_contacts": True}, headers=HEADERS))
    cv_builder.wait_for_build()
    record(client.get("/api/cv"))
    key = client.get("/api/jobs").json()[0]["key"]
    record(client.get("/api/promote/estimate", params={"key": key}))
    record(client.post("/api/promote", json={"key": key, "confirm_cost": True}, headers=HEADERS))
    for path in ("/api/jobs", "/api/overview", "/api/agents", "/api/activity", "/api/jd", "/api/tailored", "/api/settings"):
        record(client.get(path, params={"key": key} if path in ("/api/jd", "/api/tailored") else None))
    record(client.post("/api/settings", json={"profile": {"pages": 3}}, headers=HEADERS))
    record(client.post("/api/settings", json={"profile": {"pages": "x"}}, headers=HEADERS))
    return bodies


def test_no_secret_value_appears_in_data_logs_or_responses(secured: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    bodies = walk_flows(secured)
    haystacks = [caplog.text, *bodies]
    for path in config.DATA_DIR.rglob("*"):
        if path.is_file():
            haystacks.append(path.read_bytes().decode("latin-1"))
    for name, value in SECRETS.items():
        for text in haystacks:
            assert value not in text, f"{name} leaked"
    assert (config.DATA_DIR / "cv" / "base_cv.json").exists() and list((config.DATA_DIR / "cv" / "tailored").glob("*.docx"))
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 2  # one CV build and one tailoring, nothing else


def test_no_real_network_connection_is_attempted(secured: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[Any] = []

    def refuse(self: Any, address: Any, *args: Any, **kwargs: Any) -> None:
        attempts.append(address)
        raise OSError("blocked by the test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: attempts.append(a) or (_ for _ in ()).throw(OSError("blocked")))
    walk_flows(secured)
    assert attempts == []


def test_pasted_urls_that_break_the_rules_never_reach_the_network(secured: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("an unsafe URL reached the HTTP client")

    monkeypatch.setattr(safe_fetch.httpx, "Client", no_client)
    for url in ("http://example.com/job", "https://user:pw@example.com/job", "https://example.com:8443/job",
                "https://127.0.0.1/", "https://[::1]/", "https://10.1.2.3/x", "https://172.16.0.9/x", "https://192.168.0.7/x",
                "https://169.254.169.254/latest/meta-data/", "https://[fc00::1]/", "https://[fe80::1]/", "https://0.0.0.0/",
                "javascript:alert(1)", "file:///etc/passwd", "ftp://example.com/x"):
        resp = secured.post("/api/add-job", json={"url": url}, headers=HEADERS)
        assert resp.status_code == 400, (url, resp.status_code, resp.text[:80])


def test_a_hostname_that_resolves_to_a_private_address_is_blocked_before_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("connected to a private address")

    monkeypatch.setattr(safe_fetch.httpx, "Client", no_client)
    for address in ("127.0.0.1", "10.0.0.5", "172.16.5.5", "192.168.1.1", "169.254.169.254", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1"):
        with pytest.raises(safe_fetch.UnsafeURL):
            safe_fetch.fetch_public_page("https://rebind.example/job", resolver=lambda host, port, a=address: [a])


def test_cors_is_never_enabled_on_any_route(secured: TestClient) -> None:
    paths = {route.path for route in app.routes if getattr(route, "path", "").startswith("/api")} | {"/"}
    for path in sorted(paths):
        for method in ("GET", "OPTIONS"):
            resp = secured.request(method, path, headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
            assert not [h for h in resp.headers if h.lower().startswith("access-control-")], (method, path)
            assert resp.status_code != 500, (method, path)


def test_every_endpoint_survives_garbage_without_a_server_error(secured: TestClient) -> None:
    garbage: list[dict[str, Any]] = [{}, {"key": "x" * 5000}, {"url": 5}, {"text": ["a"]}, {"profile": "x"}, {"yaml": None},
                                     {"ats": 1, "slug": []}, {"kind": {}, "value": []}, {"add": "x", "remove": 3}]
    post_paths = sorted(r.path for r in app.routes if getattr(r, "path", "").startswith("/api") and "POST" in getattr(r, "methods", set()))
    for path in post_paths:
        if path == "/api/cv":
            continue
        for body in garbage:
            resp = secured.post(path, json=body, headers=HEADERS)
            assert resp.status_code < 500, (path, body, resp.status_code, resp.text[:100])
    for path in sorted(r.path for r in app.routes if getattr(r, "path", "").startswith("/api") and "GET" in getattr(r, "methods", set())):
        for params in ({}, {"key": "x" * 5000}, {"key": "../../etc/passwd"}, {"limit": "x"}):
            assert secured.get(path, params=params).status_code < 500, (path, params)


def test_the_test_suite_itself_cannot_reach_the_internet() -> None:
    """conftest.py refuses non-loopback connections and lookups, so no test can silently use the network."""
    with pytest.raises(RuntimeError, match="must not use the network"):
        socket.create_connection(("93.184.216.34", 80), timeout=1)
    with pytest.raises(RuntimeError, match="must not use the network"):
        socket.getaddrinfo("example.com", 443)


# --- secrets that providers put into the data they return --------------------------------------------------------

def test_scrub_url_removes_only_the_parameters_that_carry_a_secret() -> None:
    from app.scout.text import scrub_url

    secret = "adzuna-id-1234"
    url = f"https://www.adzuna.com/land/ad/1?se=x&utm_source={secret}&v=2#frag"
    assert scrub_url(url, [secret, ""]) == "https://www.adzuna.com/land/ad/1?se=x&v=2#frag"
    assert scrub_url("https://example.com/a?b=c", [secret]) == "https://example.com/a?b=c"
    assert scrub_url("", [secret]) == ""
    assert scrub_url(f"https://example.com/{secret}/page", [secret]) == ""  # a secret in the path cannot be removed: drop the link


def test_secret_values_in_already_stored_links_are_cleaned_at_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("ADZUNA_APP_ID", SECRETS["ADZUNA_APP_ID"])
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    db.init_db()
    with db.connect() as conn:
        conn.execute("INSERT INTO jobs (key, title, url) VALUES ('a', 't', ?)", (f"https://adzuna.example/1?utm_source={SECRETS['ADZUNA_APP_ID']}&x=1",))
        conn.execute("INSERT INTO jobs (key, title, url) VALUES ('b', 't', 'https://other.example/2?x=1')")
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        rows = {r["key"]: r["url"] for r in client.get("/api/jobs").json()}
    assert rows == {"a": "https://adzuna.example/1?x=1", "b": "https://other.example/2?x=1"}
    with db.connect() as conn:
        assert SECRETS["ADZUNA_APP_ID"] not in json.dumps([dict(r) for r in conn.execute("SELECT * FROM jobs")])
