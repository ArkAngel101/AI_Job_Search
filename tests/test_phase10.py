"""Phase 10: settings (validation, clamping, preserved keys, no secrets, effect on the next scan)."""
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, db, forge, settings
from app.main import app
from app.profile import Profile, load_profile
from app.scout.http import ScoutClient

FIXTURES = Path(__file__).parent / "fixtures"
HEADERS = {"X-Forge-Request": "1"}
SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-SECRETVALUE-aaaa1111", "ADZUNA_APP_ID": "adzunaid-SECRET-2222",
    "ADZUNA_APP_KEY": "adzunakey-SECRET-3333", "RAPIDAPI_KEY": "rapid-SECRET-4444",
}


def fx(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    monkeypatch.setitem(forge._state, "active", False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


def post(client: TestClient, body: Any) -> Any:
    return client.post("/api/settings", json=body, headers=HEADERS)


def get(client: TestClient) -> dict[str, Any]:
    return client.get("/api/settings").json()


def stored_profile() -> dict[str, Any]:
    with db.connect() as conn:
        return db.get_setting(conn, "profile", {})


# --- reading ---------------------------------------------------------------------------------------

def test_defaults(client: TestClient) -> None:
    data = get(client)
    p = Profile()
    assert data["profile"] == {k: getattr(p, k) for k in settings.PROFILE_KEYS}
    assert data["customized"] == [] and data["defaults"] == data["profile"]
    assert data["profile"]["seniority"] == "entry" and (data["profile"]["first_cut"], data["profile"]["final"]) == (50, 65)
    assert [s["name"] for s in data["sources"]] == list(Profile().enabled_sources) and all(s["enabled"] for s in data["sources"])
    assert data["claude"] == {"model": "claude-sonnet-5-5", "model_from_env": False, "key_set": False, "prices": None,
                              "monthly_cap_usd": None, "spent_this_month_usd": 0.0}
    assert "greenhouse:" in data["boards_yaml"] and data["system"]["scheduler"].startswith("none")


def test_no_secret_ever_appears_in_a_settings_response(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-5-5")
    responses = [client.get("/api/settings").text, post(client, {"profile": {"pages": 3}}).text,
                 post(client, {"profile": {"pages": 99999999}}).text, post(client, {"nonsense": 1}).text,
                 post(client, {"claude": {"prices": {"input_per_mtok": 1, "output_per_mtok": 2}}}).text]
    for text in responses:
        for value in SECRETS.values():
            assert value not in text, (value, text[:80])
    data = get(client)
    assert data["claude"]["key_set"] is True and data["claude"]["model"] == "claude-haiku-5-5" and data["claude"]["model_from_env"] is True
    adzuna = next(s for s in data["sources"] if s["name"] == "adzuna")
    assert adzuna["needs_key"] and adzuna["key_set"] is True and adzuna["key_names"] == ["ADZUNA_APP_ID", "ADZUNA_APP_KEY"]
    with db.connect() as conn:  # nothing secret was written to the database either
        dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM settings")])
    assert not any(v in dump for v in SECRETS.values())


def test_key_status_per_source(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    by_name = {s["name"]: s for s in get(client)["sources"]}
    assert by_name["adzuna"]["key_set"] is False and by_name["greenhouse"]["needs_key"] is False and by_name["greenhouse"]["key_set"] is None
    monkeypatch.setenv("ADZUNA_APP_ID", "x")
    assert next(s for s in get(client)["sources"] if s["name"] == "adzuna")["key_set"] is False  # both halves are needed
    monkeypatch.setenv("ADZUNA_APP_KEY", "y")
    assert next(s for s in get(client)["sources"] if s["name"] == "adzuna")["key_set"] is True
    assert by_name["remotive"]["min_hours_between_fetches"] == 6.0 and by_name["jobicy"]["min_hours_between_fetches"] == 1.0
    assert "jsearch" not in by_name


# --- clamping -----------------------------------------------------------------------------------------

@pytest.mark.parametrize(("field", "sent", "stored"), [
    ("pages", 0, 1), ("pages", 9, 5), ("pages", 3, 3), ("pages", 2.6, 3),
    ("max_age_days", 0, 1), ("max_age_days", 1000, 365), ("max_age_days", 14, 14),
    ("results_per_scan", 5, 10), ("results_per_scan", 999, 300), ("results_per_scan", 120, 120),
    ("first_cut", -5, 0), ("first_cut", 150, 100), ("final", 150, 100),
])
def test_numbers_are_clamped(client: TestClient, field: str, sent: float, stored: int) -> None:
    resp = post(client, {"profile": {field: sent}})
    assert resp.status_code == 200 and resp.json()["profile"][field] == stored
    assert stored_profile()[field] == stored


def test_final_threshold_never_sits_below_the_first_cut(client: TestClient) -> None:
    assert post(client, {"profile": {"first_cut": 70, "final": 60}}).status_code == 200
    p = get(client)["profile"]
    assert (p["first_cut"], p["final"]) == (70, 70)
    p = post(client, {"profile": {"final": 40}}).json()["profile"]  # lowering final alone is clamped to the first cut
    assert (p["first_cut"], p["final"]) == (70, 70)
    p = post(client, {"profile": {"first_cut": 90}}).json()["profile"]  # raising the first cut pushes final up
    assert (p["first_cut"], p["final"]) == (90, 90)
    p = post(client, {"profile": {"first_cut": 20, "final": 95}}).json()["profile"]
    assert (p["first_cut"], p["final"]) == (20, 95)


# --- invalid input -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"profile": {"seniority": "boss"}}, {"profile": {"seniority": ""}}, {"profile": {"seniority": 3}},
    {"profile": {"enabled_sources": []}}, {"profile": {"enabled_sources": ["greenhouse", "linkedin"]}},
    {"profile": {"enabled_sources": "greenhouse"}}, {"profile": {"unknown_setting": 1}}, {"nonsense": 1},
    {"profile": {"pages": "abc"}}, {"profile": {"pages": None}}, {"profile": {"pages": True}}, {"profile": {"first_cut": [1]}},
    {"profile": {"titles": []}}, {"profile": {"titles": "Data Scientist"}}, {"profile": {"titles": [1, 2]}},
    {"profile": {"keywords": []}}, {"profile": {"keywords": ["machine learning engineer"]}},
    {"profile": {"keywords": ["x" * 41]}}, {"profile": {"scoring_skills": ["one two three four five"]}},
    {"profile": {"titles": [f"title {i}" for i in range(31)]}},
    {"profile": []}, {"claude": []}, {"claude": {"model": "x"}},
    {"claude": {"prices": {"input_per_mtok": -1, "output_per_mtok": 2}}}, {"claude": {"prices": {"input_per_mtok": 1}}},
    {"claude": {"prices": {"input_per_mtok": "a", "output_per_mtok": 2}}}, {"claude": {"prices": 5}},
    {"claude": {"prices": {"input_per_mtok": 99999, "output_per_mtok": 2}}},
    {"claude": {"monthly_cap_usd": -1}}, {"claude": {"monthly_cap_usd": "ten"}}, {"claude": {"monthly_cap_usd": 1e9}},
    {"reset": ["nope"]}, {"reset": "pages"},
])
def test_invalid_input_returns_400_and_saves_nothing(client: TestClient, body: Any) -> None:
    before = stored_profile()
    resp = post(client, body)
    assert resp.status_code == 400 and isinstance(resp.json()["detail"], str) and resp.json()["detail"]
    assert stored_profile() == before


def test_a_partly_invalid_update_saves_nothing_at_all(client: TestClient) -> None:
    resp = post(client, {"profile": {"pages": 4, "seniority": "boss"}})
    assert resp.status_code == 400 and stored_profile() == {}
    resp = post(client, {"profile": {"pages": 4}, "claude": {"monthly_cap_usd": -1}})
    assert resp.status_code == 400 and stored_profile() == {}


def test_malformed_bodies_and_csrf(client: TestClient) -> None:
    assert client.post("/api/settings", content=b"not json", headers={**HEADERS, "Content-Type": "application/json"}).status_code == 422
    assert client.post("/api/settings", json=[1, 2], headers=HEADERS).status_code == 422
    assert client.post("/api/settings", json={"profile": {"pages": 2}}).status_code == 403
    assert client.post("/api/boards", json={"yaml": "x"}).status_code == 403
    assert client.post("/api/boards/check", json={"ats": "lever", "slug": "x"}).status_code == 403


# --- what is saved: only changed keys, atomically ----------------------------------------------------------------

def test_only_changed_keys_are_stored_and_untouched_ones_survive(client: TestClient) -> None:
    post(client, {"profile": {"keywords": ["quantum computing", "NLP"]}})
    post(client, {"profile": {"pages": 3}})
    assert stored_profile() == {"keywords": ["quantum computing", "NLP"], "pages": 3}
    post(client, {"profile": {"seniority": "senior"}, "claude": {"monthly_cap_usd": 25}})
    assert stored_profile() == {"keywords": ["quantum computing", "NLP"], "pages": 3, "seniority": "senior"}
    data = get(client)
    assert data["customized"] == ["keywords", "pages", "seniority"] and data["claude"]["monthly_cap_usd"] == 25.0
    assert data["profile"]["titles"] == Profile().titles  # never touched, still following the defaults


def test_lists_are_cleaned_and_deduplicated(client: TestClient) -> None:
    data = post(client, {"profile": {"titles": ["  Data   Scientist ", "data scientist", "NLP Engineer", ""]}}).json()
    assert data["profile"]["titles"] == ["Data Scientist", "NLP Engineer"]
    assert post(client, {"profile": {"scoring_skills": []}}).json()["profile"]["scoring_skills"] == []  # skills may be empty


def test_sources_keep_their_canonical_order(client: TestClient) -> None:
    data = post(client, {"profile": {"enabled_sources": ["remoteok", "greenhouse"]}}).json()
    assert data["profile"]["enabled_sources"] == ["greenhouse", "remoteok"]
    assert [s["enabled"] for s in data["sources"]] == [True, False, False, False, False, False, True, False, False]


def test_reset_returns_a_setting_to_its_default(client: TestClient) -> None:
    post(client, {"profile": {"pages": 4, "seniority": "mid"}})
    data = post(client, {"reset": ["pages"]}).json()
    assert data["profile"]["pages"] == 2 and data["profile"]["seniority"] == "mid" and stored_profile() == {"seniority": "mid"}
    post(client, {"reset": ["seniority"]})
    with db.connect() as conn:
        assert db.get_setting(conn, "profile") is None  # nothing left to store


def test_cv_suggestions_follow_until_the_owner_edits_then_resume_after_reset(client: TestClient) -> None:
    with db.connect() as conn:
        db.set_setting(conn, "cv_suggestions", {"scoring_skills": ["ROS"], "keywords": ["robotics"], "seniority": "mid"})
    p = get(client)["profile"]
    assert "ROS" in p["scoring_skills"] and "robotics" in p["keywords"] and p["seniority"] == "mid"
    p = post(client, {"profile": {"scoring_skills": ["Rust"], "seniority": "senior"}}).json()["profile"]
    assert p["scoring_skills"] == ["Rust"] and p["seniority"] == "senior" and "robotics" in p["keywords"]  # keywords still follow
    p = post(client, {"reset": ["scoring_skills", "seniority"]}).json()["profile"]
    assert "ROS" in p["scoring_skills"] and "Rust" not in p["scoring_skills"] and p["seniority"] == "mid"


def test_prices_and_cap_are_saved_and_cleared(client: TestClient) -> None:
    data = post(client, {"claude": {"prices": {"input_per_mtok": 2, "output_per_mtok": 10}, "monthly_cap_usd": 15.5}}).json()
    assert data["claude"]["prices"] == {"input_per_mtok": 2.0, "output_per_mtok": 10.0} and data["claude"]["monthly_cap_usd"] == 15.5
    with db.connect() as conn:
        db.record_llm_usage(conn, None, "m", 10, 10, 1.2345)
    assert get(client)["claude"]["spent_this_month_usd"] == 1.2345
    data = post(client, {"claude": {"prices": None, "monthly_cap_usd": None}}).json()
    assert data["claude"]["prices"] is None and data["claude"]["monthly_cap_usd"] is None
    assert post(client, {"claude": {"monthly_cap_usd": 0}}).json()["claude"]["monthly_cap_usd"] is None  # 0 means no cap


# --- the next scan uses the new settings -------------------------------------------------------------------------------

class Web:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        data = {"boards-api.greenhouse.io": "greenhouse.json", "api.lever.co": "lever.json", "api.adzuna.com": "adzuna.json"}.get(host)
        return httpx.Response(200, json=fx(data)) if data else httpx.Response(404, json={})


def scan(web: Web, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(forge, "make_client", lambda: ScoutClient(transport=httpx.MockTransport(web.handler), sleep=lambda s: None))
    run_id = forge.start_find(background=False)
    with db.connect() as conn:
        return json.loads(conn.execute("SELECT summary_json FROM runs WHERE id = ?", (run_id,)).fetchone()[0])


def test_a_threshold_change_and_a_new_keyword_change_the_next_scan(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    (config.DATA_DIR / "boards.yaml").write_text("greenhouse:\n  - {slug: acme, name: Acme}\nlever:\n  - {slug: beta, name: Beta}\n")
    post(client, {"profile": {"enabled_sources": ["greenhouse", "lever", "adzuna"], "max_age_days": 365, "pages": 1}})

    web = Web()
    first = scan(web, monkeypatch)
    adzuna_terms = {r.url.params["what"] for r in web.requests if r.url.host == "api.adzuna.com"}
    assert adzuna_terms == set(Profile().keywords) and first["matched"] >= 3

    post(client, {"profile": {"keywords": ["quantum computing"]}})  # a new keyword
    web = Web()
    second = scan(web, monkeypatch)
    assert {r.url.params["what"] for r in web.requests if r.url.host == "api.adzuna.com"} == {"quantum computing"}
    assert second["matched"] == first["matched"]

    post(client, {"profile": {"first_cut": 99}})  # a stricter threshold
    third = scan(Web(), monkeypatch)
    assert third["matched"] == 0 and third["stored"] == 0

    post(client, {"profile": {"first_cut": 50}, "reset": ["keywords"]})
    assert scan(Web(), monkeypatch)["matched"] == first["matched"]


def test_disabling_a_source_skips_it_and_a_cap_on_results_applies(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    (config.DATA_DIR / "boards.yaml").write_text("greenhouse:\n  - {slug: acme, name: Acme}\nlever:\n  - {slug: beta, name: Beta}\n")
    post(client, {"profile": {"enabled_sources": ["greenhouse"], "max_age_days": 365, "results_per_scan": 10}})
    web = Web()
    summary = scan(web, monkeypatch)
    assert {r.url.host for r in web.requests} == {"boards-api.greenhouse.io"} and summary["sources_run"] == 1


# --- boards.yaml and the slug check ---------------------------------------------------------------------------------------

def test_boards_file_can_be_edited_and_is_validated(client: TestClient) -> None:
    text = "# my boards\ngreenhouse:\n  - {slug: acme, name: Acme}\nlever: []\n"
    resp = client.post("/api/boards", json={"yaml": text}, headers=HEADERS)
    assert resp.status_code == 200 and resp.json() == {"saved": True, "boards": {"greenhouse": 1, "lever": 0}}
    assert get(client)["boards_yaml"] == text  # comments are kept: the text is saved as written
    assert not list((config.DATA_DIR).glob("*.tmp")) and not list(config.DATA_DIR.glob("*.yaml.tmp"))
    before = (config.DATA_DIR / "boards.yaml").read_text()
    for bad in ("greenhouse:\n  - '../etc/passwd'\n", "workday:\n  - x\n", "greenhouse: [unclosed", "- just\n- a list\n", "x" * 60_000):
        resp = client.post("/api/boards", json={"yaml": bad}, headers=HEADERS)
        assert resp.status_code in (400, 422), bad[:30]
        assert (config.DATA_DIR / "boards.yaml").read_text() == before  # an invalid file never replaces the good one


def test_slug_check_reports_found_missing_and_errors(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "ghost" in request.url.path:
            return httpx.Response(404, json={})
        if "broken" in request.url.path:
            return httpx.Response(500)
        if "weird" in request.url.path:
            return httpx.Response(200, json={"unexpected": 1})
        return httpx.Response(200, json=fx("greenhouse.json"))

    monkeypatch.setattr(settings, "make_client", lambda: ScoutClient(transport=httpx.MockTransport(handler), sleep=lambda s: None))

    def check(ats: str, slug: str) -> Any:
        return client.post("/api/boards/check", json={"ats": ats, "slug": slug}, headers=HEADERS)

    assert check("greenhouse", "acme").json() == {"ok": True, "jobs": 2, "detail": "Found 2 open jobs."}
    assert check("greenhouse", "ghost").json() == {"ok": False, "detail": "No Greenhouse board named 'ghost' was found (404)."}
    assert check("greenhouse", "broken").json()["ok"] is False and "HTTP 500" in check("greenhouse", "broken").json()["detail"]
    assert check("greenhouse", "weird").json() == {"ok": False, "detail": "The board answered in an unexpected format."}
    for body in ({"ats": "workday", "slug": "x"}, {"ats": "lever", "slug": "../x"}, {"ats": "lever", "slug": ""}, {"ats": "lever", "slug": "a b"}):
        assert client.post("/api/boards/check", json=body, headers=HEADERS).status_code == 422


# --- system -------------------------------------------------------------------------------------------------------------------

def test_system_section_counts(client: TestClient) -> None:
    db.log_activity("forge", "x", "completed")
    system = get(client)["system"]
    assert system["activity_entries"] == 1 and system["jobs"] == 0 and system["database_bytes"] > 0
    assert system["data_dir"] == str(config.DATA_DIR) and load_profile() == Profile()
