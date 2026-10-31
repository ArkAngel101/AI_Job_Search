"""API audit: every endpoint answers as expected, bad input is rejected, traversal is refused.

Run it against the running container (it needs only the standard library plus httpx):

    .venv/bin/python scripts/api_audit.py [http://127.0.0.1:8000]

It never starts a scan, never makes a paid Claude call, and never contacts a job source. The only data it
changes is your settings and boards file, which it snapshots first and restores afterwards.
The same audit runs in the test suite against an in-process server (tests/test_phase11_audit.py).
"""
import json
import sys
from typing import Any, Protocol

UNKNOWN_KEY = "0123456789abcdef"
EVIL_KEYS = ["../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "/etc/passwd", "....//....//etc/passwd", "0123456789abcde/../../x"]
PROFILE_KEYS = ("seniority", "titles", "keywords", "scoring_skills", "first_cut", "final", "pages", "max_age_days",
                "results_per_scan", "enabled_sources")
WRITE = {"X-Forge-Request": "1"}


class Client(Protocol):
    """The subset of httpx.Client / Starlette TestClient the audit uses."""

    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


class Audit:
    """Collects what was exercised and what failed."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.failures: list[str] = []
        self.covered: set[tuple[str, str]] = set()
        self.checks = 0

    # -- helpers ---------------------------------------------------------------------------------
    def hit(self, method: str, route: str, path: str | None = None, **kwargs: Any) -> Any:
        """Send a request; `route` is the route pattern it covers (for the coverage check)."""
        self.covered.add((method, route))
        return self.client.request(method, path or route, **kwargs)

    def check(self, ok: bool, what: str) -> None:
        self.checks += 1
        if not ok:
            self.failures.append(what)

    def status(self, resp: Any, expected: int | tuple[int, ...], what: str) -> None:
        wanted = (expected,) if isinstance(expected, int) else expected
        self.check(resp.status_code in wanted, f"{what}: expected {wanted}, got {resp.status_code} {resp.text[:120]!r}")

    def keys(self, resp: Any, expected: set[str], what: str) -> Any:
        """Expect a 200 JSON object containing at least `expected` keys."""
        self.status(resp, 200, what)
        data = resp.json() if resp.status_code == 200 else {}
        self.check(isinstance(data, dict) and expected <= set(data), f"{what}: missing keys {sorted(expected - set(data or {}))}")
        return data

    def client_error(self, resp: Any, what: str) -> None:
        self.check(400 <= resp.status_code < 500, f"{what}: expected a 4xx, got {resp.status_code}")


# --- the audit ---------------------------------------------------------------------------------------

def audit(client: Client) -> Audit:
    """Run every check and return the results. Settings and boards are restored even if a check fails."""
    a = Audit(client)
    snapshot = a.client.request("GET", "/api/settings").json()
    try:
        _reads(a)
        _bad_input(a)
        _security(a)
        _writes(a, snapshot)
    finally:
        _restore(a, snapshot)
    return a


def _reads(a: Audit) -> None:
    a.keys(a.hit("GET", "/api/health"), {"status"}, "health")
    index = a.hit("GET", "/")
    a.status(index, 200, "index")
    a.check(index.headers.get("cache-control") == "no-store" and "FORGE" in index.text, "index: no-store and the FORGE page")
    a.keys(a.hit("GET", "/api/overview"), {"found", "matched", "read", "tailored", "known", "new"}, "overview")
    a.keys(a.hit("GET", "/api/run-status"), {"active", "run", "sources"}, "run-status")
    jobs = a.hit("GET", "/api/jobs")
    a.status(jobs, 200, "jobs")
    rows = jobs.json() if jobs.status_code == 200 else []
    a.check(isinstance(rows, list) and all({"rank", "score", "title", "company", "key", "status", "needs_sponsorship"} <= set(r) for r in rows), "jobs: row keys")
    for params in ({"min_score": 50}, {"new_only": "true"}, {"hide_closed": "true"}, {"q": "engineer"}, {"limit": 1, "offset": 0}, {"status": "read"}, {"user_status": "new"}):
        a.status(a.hit("GET", "/api/jobs", params=params), 200, f"jobs filter {params}")
    agents = a.hit("GET", "/api/agents")
    a.status(agents, 200, "agents")
    a.check([x["name"] for x in agents.json()] == ["forge", "scout", "job-reader", "cv-adapter"] if agents.status_code == 200 else False, "agents: the four agents")
    activity = a.hit("GET", "/api/activity", params={"limit": 5})
    a.status(activity, 200, "activity")
    a.check(isinstance(activity.json(), list) and len(activity.json()) <= 5 if activity.status_code == 200 else False, "activity: a list of at most 5")
    credits = a.hit("GET", "/api/attributions")
    a.status(credits, 200, "attributions")
    a.check({"remoteok", "themuse"} <= set(credits.json()) if credits.status_code == 200 else False, "attributions: RemoteOK and The Muse")
    settings = a.keys(a.hit("GET", "/api/settings"), {"profile", "customized", "defaults", "sources", "boards_yaml", "claude", "system"}, "settings")
    a.check(set(PROFILE_KEYS) <= set(settings.get("profile", {})), "settings: profile keys")
    a.keys(a.hit("GET", "/api/cv"), {"building", "error", "has_base", "base_cv", "pending"}, "cv status")
    for route in ("/api/cv/preview", "/api/cv/estimate"):  # 409 until a CV has been uploaded
        a.status(a.hit("GET", route), (200, 409), route)
    for route, params in (("/api/promote/estimate", {"key": UNKNOWN_KEY}), ("/api/tailored", {"key": UNKNOWN_KEY}), ("/api/jd", {"key": UNKNOWN_KEY})):
        a.status(a.hit("GET", route, params=params), 404, f"{route} with an unknown job")
    for route in ("/api/cv-docx", "/api/cv-pdf"):
        a.status(a.hit("GET", route, params={"key": UNKNOWN_KEY}), 404, f"{route} with an unknown job")
    for name in ("core.js", "overview.js", "jobs.js", "cv.js", "settings.js", "app.js", "styles.css"):
        resp = a.client.request("GET", f"/static/{name}")
        a.check(resp.status_code == 200 and resp.headers.get("cache-control") == "no-store", f"static {name}")


def _bad_input(a: Audit) -> None:
    posts = [
        ("/api/add-job", {}), ("/api/add-job", {"text": "   "}), ("/api/add-job", {"url": "javascript:alert(1)"}),
        ("/api/add-job", {"url": "http://example.com/job"}), ("/api/add-job", {"url": "https://127.0.0.1/admin"}),
        ("/api/add-job", {"url": "https://169.254.169.254/latest/meta-data/"}), ("/api/add-job", {"text": "x" * 100_001}),
        ("/api/add-board", {"ats": "workday", "slug": "x"}), ("/api/add-board", {"ats": "lever", "slug": "../x"}),
        ("/api/cv/contacts", {"add": [{"kind": "bogus", "value": "x"}]}), ("/api/cv/build", {}),
        ("/api/promote", {"key": ""}), ("/api/job/status", {"key": UNKNOWN_KEY, "user_status": "hired"}),
        ("/api/job/status", {"key": UNKNOWN_KEY, "user_status": "new"}),
        ("/api/settings", {"profile": {"seniority": "boss"}}), ("/api/settings", {"profile": {"enabled_sources": []}}),
        ("/api/settings", {"profile": {"pages": "abc"}}), ("/api/settings", {"nonsense": 1}),
        ("/api/settings", {"claude": {"monthly_cap_usd": -1}}), ("/api/boards", {"yaml": "greenhouse:\n  - '../etc/passwd'\n"}),
        ("/api/boards", {"yaml": "workday:\n  - x\n"}), ("/api/boards/check", {"ats": "workday", "slug": "x"}),
        ("/api/boards/check", {"ats": "lever", "slug": "../x"}),
    ]
    for route, body in posts:
        resp = a.hit("POST", route, json=body, headers=WRITE)
        a.client_error(resp, f"POST {route} {json.dumps(body)[:60]}")
    for params in ({"min_score": 101}, {"limit": 0}, {"limit": 5000}, {"offset": -1}, {"status": "bogus"}, {"min_score": "abc"}):
        a.status(a.hit("GET", "/api/jobs", params=params), 422, f"jobs {params}")
    a.status(a.hit("GET", "/api/activity", params={"limit": 0}), 422, "activity limit 0")
    a.client_error(a.hit("POST", "/api/cv", params={"filename": "cv.exe"}, content=b"MZ", headers={**WRITE, "Content-Type": "application/octet-stream"}), "cv upload .exe")
    a.client_error(a.hit("POST", "/api/cv", params={"filename": "cv.txt"}, content=b"", headers=WRITE), "cv upload empty")
    for resp in (a.client.request("POST", "/api/settings", content=b"not json", headers={**WRITE, "Content-Type": "application/json"}),
                 a.client.request("POST", "/api/settings", json=[1, 2], headers=WRITE)):
        a.client_error(resp, "malformed settings body")


def _security(a: Audit) -> None:
    for evil in EVIL_KEYS:
        for route in ("/api/cv-docx", "/api/cv-pdf"):
            a.status(a.client.request("GET", f"{route}?key={evil}"), 400, f"{route} traversal {evil!r}")
    for path in ("/static/../app/db.py", "/static/%2e%2e/app/db.py", "/static/..%2fapp%2fdb.py"):
        a.check(a.client.request("GET", path).status_code in (400, 404), f"static traversal {path}")
    a.status(a.client.request("GET", "/api/health", headers={"Host": "evil.example"}), 400, "bad Host")
    a.status(a.client.request("GET", "/api/health", headers={"Host": "192.168.1.5:8000"}), 400, "LAN Host")
    post_routes = ["/api/run", "/api/add-job", "/api/add-board", "/api/cv/contacts", "/api/cv/build", "/api/promote",
                   "/api/job/status", "/api/settings", "/api/boards", "/api/boards/check"]
    for route in post_routes:
        a.status(a.client.request("POST", route, json={}), 403, f"POST {route} without X-Forge-Request")
        a.status(a.client.request("POST", route, json={}, headers={**WRITE, "Origin": "http://evil.example"}), 403, f"POST {route} from a foreign Origin")
    a.status(a.client.request("POST", "/api/cv", params={"filename": "cv.txt"}, content=b"x"), 403, "cv upload without header")
    for route in ("/api/health", "/api/settings", "/api/jobs", "/"):
        resp = a.client.request("GET", route, headers={"Origin": "http://evil.example"})
        a.check(not any(name.lower().startswith("access-control-") for name in resp.headers), f"no CORS headers on {route}")
        a.check("default-src 'self'" in resp.headers.get("content-security-policy", "") and resp.headers.get("x-content-type-options") == "nosniff", f"security headers on {route}")
    pre = a.client.request("OPTIONS", "/api/settings", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    a.check(not any(name.lower().startswith("access-control-") for name in pre.headers), "no CORS preflight answer")
    for path in ("/docs", "/redoc", "/openapi.json"):
        a.status(a.client.request("GET", path), 404, f"{path} is disabled")
    a.check(a.client.request("DELETE", "/api/settings", headers=WRITE).status_code in (403, 405), "DELETE is not accepted")
    a.status(a.hit("POST", "/api/run", json={}), 403, "POST /api/run without header")


def _writes(a: Audit, snapshot: dict[str, Any]) -> None:
    resp = a.hit("POST", "/api/settings", json={"profile": {"pages": 5, "max_age_days": 999}}, headers=WRITE)
    data = a.keys(resp, {"profile"}, "settings write")
    a.check(data.get("profile", {}).get("pages") == 5 and data.get("profile", {}).get("max_age_days") == 365, "settings: saved and clamped")
    resp = a.hit("POST", "/api/boards", json={"yaml": snapshot["boards_yaml"]}, headers=WRITE)
    a.keys(resp, {"saved", "boards"}, "boards write")
    jobs = a.client.request("GET", "/api/jobs").json()
    if jobs:
        job = jobs[0]
        resp = a.hit("POST", "/api/job/status", json={"key": job["key"], "user_status": job["user_status"]}, headers=WRITE)
        a.keys(resp, {"key", "user_status"}, "job status write (unchanged value)")
        detail = a.keys(a.hit("GET", "/api/jd", params={"key": job["key"]}), {"key", "title", "read", "description"}, "job detail")
        a.check(not detail.get("url") or detail["url"].lower().startswith(("http://", "https://")), "job detail: only http(s) links")


def _restore(a: Audit, snapshot: dict[str, Any]) -> None:
    """Put settings and boards back exactly as they were."""
    profile = snapshot["profile"]
    custom = snapshot["customized"]
    body = {
        "profile": {k: profile[k] for k in custom},
        "reset": [k for k in PROFILE_KEYS if k not in custom],
        "claude": {"prices": snapshot["claude"]["prices"], "monthly_cap_usd": snapshot["claude"]["monthly_cap_usd"]},
    }
    a.client.request("POST", "/api/settings", json=body, headers=WRITE)
    a.client.request("POST", "/api/boards", json={"yaml": snapshot["boards_yaml"]}, headers=WRITE)
    after = a.client.request("GET", "/api/settings").json()
    a.check(after["profile"] == profile and after["customized"] == custom, "restore: settings are back as they were")
    a.check(after["boards_yaml"].strip() == snapshot["boards_yaml"].strip(), "restore: boards are back as they were")


def main() -> int:
    import httpx

    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    with httpx.Client(base_url=base, timeout=30) as client:
        result = audit(client)
    print(f"{result.checks} checks, {len(result.failures)} failed")
    for failure in result.failures:
        print(f"  FAIL {failure}")
    return 1 if result.failures else 0


if __name__ == "__main__":
    sys.exit(main())
