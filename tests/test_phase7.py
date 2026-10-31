"""Phase 7: CV upload, contact sanitizing, and the base-CV build (Claude is always faked)."""
import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from docx import Document
from fastapi.testclient import TestClient

from app import config, db
from app.adapter import contacts, cv_builder, extract_text, llm
from app.adapter.contacts import Item
from app.adapter.cv_builder import BuildBlocked
from app.adapter.extract_text import UploadError
from app.adapter.schema import BaseCV, CVStructured
from app.main import app
from app.profile import Profile, load_profile

FIXTURES = Path(__file__).parent / "fixtures"
HEADERS = {"X-Forge-Request": "1"}
SECRET_KEY = "sk-ant-TESTSECRET-1234567890"
CV = (FIXTURES / "cv_sample.txt").read_text()
REAL_GET_CLIENT = llm.get_client

REAL_VALUES = ["Jane Q. Doe", "Jane Doe", "jane.doe@example.com", "(617) 555-0123", "617.555.0123", "janedoe",
               "linkedin.com/in/janedoe", "github.com/janedoe", "janedoe.dev", "12 Main Street", "02115", "02139",
               "https://github.com/janedoe/matcher"]


# --- fakes ---------------------------------------------------------------------------------

def good_structured(**overrides: Any) -> CVStructured:
    data: dict[str, Any] = {
        "name": "[NAME]", "headline": "", "summary": "Recent graduate with hands-on machine learning internships.",
        "competencies": [],
        "experience": [
            {"company": "Acme Corp", "location": "Boston, MA", "start": "Jun 2025", "end": "Aug 2025",
             "role": "Machine Learning Intern",
             "bullets": ["Built a PyTorch text classifier that improved F1 by 12% over the baseline",
                         "Deployed the model with Docker on AWS"]},
            {"company": "Beta Labs", "location": "New York, NY", "start": "Jun 2024", "end": "Aug 2024",
             "role": "Data Analyst Intern", "bullets": ["Wrote SQL reports for a team of 8 analysts"]}],
        "education": [{"institution": "Northeastern University", "degree": "B.S. Computer Science",
                       "location": "Boston, MA", "start": "2022", "end": "2026", "details": ["GPA 3.8"]}],
        "skills": [{"category": "Languages", "items": ["Python", "SQL", "Java"]}],
        "projects": [{"name": "Resume Matcher", "description": "Ranks resumes against job posts, see [GITHUB_2]", "bullets": []}],
        "languages": [],
        "suggested_profile": {
            "titles": ["Machine Learning Engineer", "Data Analyst", "NLP Engineer"],
            "keywords": ["machine learning", "NLP", "data analysis tools platform", "PyTorch"],
            "scoring_skills": ["Python", "PyTorch", "SQL", "Docker", "natural language processing models and tools"],
            "seniority": "entry"},
    }
    data.update(overrides)
    return CVStructured.model_validate(data)


class FakeClient:
    """Stands in for anthropic.Anthropic: records calls, returns a canned parse result."""

    def __init__(self, result: CVStructured | Exception | None = None, stop_reason: str = "end_turn") -> None:
        self.result = result if result is not None else good_structured()
        self.stop_reason = stop_reason
        self.parse_calls: list[dict[str, Any]] = []
        self.count_calls: list[dict[str, Any]] = []
        self.count_error: Exception | None = None
        self.messages = self

    def parse(self, **kwargs: Any) -> Any:
        self.parse_calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return SimpleNamespace(parsed_output=self.result, stop_reason=self.stop_reason,
                               usage=SimpleNamespace(input_tokens=1500, output_tokens=900))

    def count_tokens(self, **kwargs: Any) -> Any:
        self.count_calls.append(kwargs)
        if self.count_error:
            raise self.count_error
        return SimpleNamespace(input_tokens=1234)


def api_error(cls: type, status: int = 401, message: str = "") -> Exception:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, json={"error": {"message": message}})
    return cls(message or "boom", response=response, body=None)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeClient]:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET_KEY)
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    db.init_db()
    fake = FakeClient()
    monkeypatch.setattr(llm, "get_client", lambda: fake)
    monkeypatch.setitem(cv_builder._state, "building", False)
    monkeypatch.setitem(cv_builder._state, "error", None)
    yield fake


def upload(text: str = CV, name: str = "cv.txt") -> dict[str, Any]:
    return cv_builder.prepare_upload(text.encode(), name)


def build(**kwargs: bool) -> None:
    cv_builder.start_build(kwargs.get("cost", True), kwargs.get("contacts", True), background=False)


def base_json() -> dict[str, Any]:
    return json.loads((cv_builder.cv_dir() / "base_cv.json").read_text())


# --- contact detection and sanitizing ------------------------------------------------------------

def detected() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for item in contacts.detect(CV):
        result.setdefault(item.kind, []).append(item.value)
    return result


def test_detects_every_kind_of_contact_detail() -> None:
    found = detected()
    assert found["name"] == ["Jane Q. Doe"]
    assert found["email"] == ["jane.doe@example.com"]
    assert set(found["phone"]) == {"(617) 555-0123", "617.555.0123"}
    assert found["linkedin"] == ["linkedin.com/in/janedoe"]
    assert set(found["github"]) == {"github.com/janedoe", "https://github.com/janedoe/matcher"}
    assert found["url"] == ["janedoe.dev"]
    assert set(found["address"]) == {"Boston, MA 02115", "12 Main Street, Apt 4, Cambridge, MA 02139"}


def test_ordinary_text_is_not_mistaken_for_contact_details() -> None:
    values = {i.value for i in contacts.detect(CV)}
    for innocent in ("ASP.NET", "Node.js", "scikit-learn", "Boston, MA", "New York, NY", "12%", "3.8", "2022 - 2026"):
        assert innocent not in values, innocent


def test_sanitize_removes_every_occurrence_in_header_body_and_footer() -> None:
    sanitized = contacts.sanitize(CV, contacts.detect(CV))
    for real in REAL_VALUES:
        assert real.lower() not in sanitized.lower(), real
    assert sanitized.startswith("[NAME]\n[ADDRESS] | [PHONE] | [EMAIL]\n[LINKEDIN] | [GITHUB] | [URL]")
    assert sanitized.rstrip().endswith("[ADDRESS_2]")
    assert "Contact [NAME] at [EMAIL] or [PHONE_2] - [ADDRESS_2]" in sanitized
    assert "Boston, MA" in sanitized and "ASP.NET" in sanitized  # employer locations and tech names survive
    assert contacts.used_tokens(sanitized)[:3] == ["[NAME]", "[ADDRESS]", "[PHONE]"]


def test_restore_round_trips_the_text() -> None:
    items = contacts.detect(CV)
    # the shorter form "Jane Doe" in the footer is restored as the full name
    assert contacts.restore(contacts.sanitize(CV, items), items) == CV.replace("Contact Jane Doe", "Contact Jane Q. Doe")
    assert contacts.restore("see [GITHUB_2] and [EMAIL_9]", items) == "see https://github.com/janedoe/matcher and [EMAIL_9]"


def test_sanitize_matches_across_line_breaks_and_case() -> None:
    items = [Item("name", "Jane Q. Doe"), Item("email", "jane.doe@example.com")]
    assert contacts.sanitize("JANE  Q.\nDOE wrote to Jane.Doe@Example.com", items) == "[NAME] wrote to [EMAIL]"


def test_leftover_check_finds_missed_details_and_respects_kept_ones() -> None:
    sanitized = "Reach me at other.person@mail.com or +44 20 7946 0958, site https://example.org/me"
    left = contacts.leftovers(sanitized, set())
    assert "other.person@mail.com" in left and "https://example.org/me" in left and any("7946" in v for v in left)
    assert "https://example.org/me" not in contacts.leftovers(sanitized, {"https://example.org/me"})
    assert contacts.leftovers(contacts.sanitize(CV, contacts.detect(CV)), set()) == []


@pytest.mark.parametrize(("text", "name"), [
    ("JANE Q. DOE\nResume\nBoston", "JANE Q. DOE"), ("Resume\nMaria de la Cruz\nx", "Maria de la Cruz"),
    ("Curriculum Vitae\nProfile\nSkills", None), ("Python Developer\nSkills: x", None), ("jane@x.com\n555-0000", None),
])
def test_name_detection(text: str, name: str | None) -> None:
    assert contacts.detect_name(text) == name


# --- text extraction -------------------------------------------------------------------------------------

def make_docx(path: Path) -> None:
    document = Document()
    document.sections[0].header.paragraphs[0].text = "Jane Q. Doe | jane.doe@example.com"
    document.add_paragraph("EXPERIENCE")
    document.add_paragraph("Acme Corp - ML Intern")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Skills", "Python, SQL"
    document.sections[0].footer.paragraphs[0].text = "617.555.0123"
    document.save(path)


MINIMAL_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
               b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
               b"4 0 obj<</Length 54>>stream\nBT /F1 14 Tf 20 100 Td (Machine Learning Intern at Acme) Tj ET\nendstream endobj\n"
               b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R/Size 6>>\n%%EOF\n")


def test_extracts_text_from_txt_md_docx(tmp_path: Path) -> None:
    txt = tmp_path / "a.txt"
    txt.write_text("Hello\r\nWorld  \n")
    assert extract_text.extract_text(txt, "txt") == "Hello\nWorld"
    md = tmp_path / "a.md"
    md.write_text("# Jane\n\n- item\n")
    assert extract_text.extract_text(md, "md") == "# Jane\n\n- item"
    docx = tmp_path / "a.docx"
    make_docx(docx)
    text = extract_text.extract_text(docx, "docx")
    assert "Acme Corp - ML Intern" in text and "Python, SQL" in text
    assert "jane.doe@example.com" in text and "617.555.0123" in text  # header and footer are included


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext (poppler) is not installed")
def test_extracts_text_from_pdf(tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(MINIMAL_PDF)
    assert "Machine Learning Intern at Acme" in extract_text.extract_text(pdf, "pdf")


@pytest.mark.parametrize(("name", "data", "status"), [
    ("cv.exe", b"MZ", 415), ("cv", b"text", 415), ("cv.pdf", b"not a pdf", 415), ("cv.docx", b"not a zip", 415),
    ("cv.txt", b"", 400), ("cv.txt", b"a\x00b", 415), ("cv.txt", b"x" * (extract_text.MAX_UPLOAD_BYTES + 1), 413),
    ("../../etc/passwd", b"x", 415),
])
def test_upload_validation(name: str, data: bytes, status: int) -> None:
    with pytest.raises(UploadError) as exc:
        extract_text.check_upload(name, data)
    assert exc.value.status == status


def test_unreadable_files_are_rejected(tmp_path: Path) -> None:
    blank = tmp_path / "blank.txt"
    blank.write_text("   \n\n")
    with pytest.raises(UploadError, match="No text"):
        extract_text.extract_text(blank, "txt")
    long = tmp_path / "long.txt"
    long.write_text("x" * (extract_text.MAX_TEXT_CHARS + 1))
    with pytest.raises(UploadError, match="longer than"):
        extract_text.extract_text(long, "txt")
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"PK\x03\x04 broken")
    with pytest.raises(UploadError, match="could not be read"):
        extract_text.extract_text(broken, "docx")


# --- upload and storage ----------------------------------------------------------------------------------------

def test_upload_stores_file_text_and_detected_details(env: FakeClient) -> None:
    status = upload()
    assert (cv_builder.cv_dir() / "current" / "cv.txt").read_text() == CV
    assert (cv_builder.cv_dir() / "pending.txt").read_text() == CV.strip()
    assert status["has_base"] is False and status["building"] is False
    pending = status["pending"]
    assert pending["name"] == "Jane Q. Doe" and pending["filename"] == "cv.txt"
    kinds = {i["kind"]: i["placeholder"] for i in pending["items"]}
    assert kinds["name"] == "[NAME]" and kinds["email"] == "[EMAIL]"
    assert env.parse_calls == [] and env.count_calls == []  # uploading is free


def test_reupload_archives_the_previous_raw_file(env: FakeClient) -> None:
    upload(CV, "first.txt")
    upload(CV + "\nExtra line", "second.txt")
    archive = list((cv_builder.cv_dir() / "archive").iterdir())
    assert len(archive) == 1 and archive[0].read_text() == CV
    assert (cv_builder.cv_dir() / "current" / "cv.txt").read_text().endswith("Extra line")


def test_failed_upload_leaves_everything_as_it_was(env: FakeClient) -> None:
    upload()
    before = (cv_builder.cv_dir() / "pending.txt").read_text()
    with pytest.raises(UploadError):
        cv_builder.prepare_upload(b"   ", "blank.txt")
    assert (cv_builder.cv_dir() / "pending.txt").read_text() == before
    assert (cv_builder.cv_dir() / "current" / "cv.txt").read_text() == CV
    assert not list((cv_builder.cv_dir() / "current").glob(".upload*"))
    assert not (cv_builder.cv_dir() / "archive").exists()


def test_stored_filename_is_never_taken_from_the_upload(env: FakeClient) -> None:
    status = cv_builder.prepare_upload(CV.encode(), "../../evil name?.txt")
    assert status["pending"]["filename"] == "evil name_.txt"
    assert [p.name for p in (cv_builder.cv_dir() / "current").iterdir()] == ["cv.txt"]


# --- owner review of contact details ---------------------------------------------------------------------------------

def test_owner_can_add_remove_and_set_the_name(env: FakeClient) -> None:
    upload()
    status = cv_builder.update_contacts(add=[{"kind": "address", "value": "Boston, MA"}])
    assert "Boston, MA" in [i["value"] for i in status["pending"]["items"]]
    hidden = cv_builder.preview()["text"]
    assert "[ADDRESS_3]" in hidden and "Boston, MA" not in hidden  # the added city is now hidden everywhere

    status = cv_builder.update_contacts(remove=["janedoe.dev"])
    assert "janedoe.dev" not in [i["value"] for i in status["pending"]["items"]]
    assert "janedoe.dev" in cv_builder.preview()["text"]  # removed on purpose, so it is sent as written

    status = cv_builder.update_contacts(name="Jane Doe")
    assert status["pending"]["name"] == "Jane Doe"
    assert sum(1 for i in status["pending"]["items"] if i["kind"] == "name") == 1


@pytest.mark.parametrize("change", [
    {"add": [{"kind": "email", "value": "typo@nowhere.com"}]}, {"add": [{"kind": "bogus", "value": "Jane"}]},
    {"remove": ["not detected"]}, {"name": "Someone Else"},
])
def test_invalid_corrections_are_rejected(env: FakeClient, change: dict[str, Any]) -> None:
    upload()
    with pytest.raises(BuildBlocked):
        cv_builder.update_contacts(**change)


def test_corrections_need_an_upload(env: FakeClient) -> None:
    with pytest.raises(BuildBlocked) as exc:
        cv_builder.update_contacts(name="Jane Doe")
    assert exc.value.status == 409


# --- the privacy guarantee ----------------------------------------------------------------------------------------------

def test_contact_details_never_appear_in_what_is_sent_to_claude(env: FakeClient) -> None:
    upload()
    build()
    (call,) = env.parse_calls
    sent = call["system"] + "\n" + call["messages"][0]["content"]
    for real in REAL_VALUES:
        assert real.lower() not in sent.lower(), real
    assert "[NAME]" in sent and "[EMAIL]" in sent and "[PHONE]" in sent and "[LINKEDIN]" in sent
    assert "PyTorch text classifier" in sent  # the CV content itself is sent


def test_preview_is_exactly_what_is_sent(env: FakeClient) -> None:
    upload()
    cv_builder.update_contacts(add=[{"kind": "address", "value": "Boston, MA"}])
    shown = cv_builder.preview()
    assert shown["message"] == f"<cv>\n{shown['text']}\n</cv>" and shown["possible_leftovers"] == []
    build()
    assert env.parse_calls[0]["messages"] == [{"role": "user", "content": shown["message"]}]
    assert env.parse_calls[0]["system"] == cv_builder.load_prompt()


def test_text_is_not_sent_if_a_contact_detail_was_missed(env: FakeClient) -> None:
    upload(CV + "\nAlso reachable at second.address@mail.com")
    # simulate a miss: drop the extra email from the stored items so the pre-send check has to catch it
    meta = json.loads((cv_builder.cv_dir() / "pending.json").read_text())
    meta["items"] = [i for i in meta["items"] if i["value"] != "second.address@mail.com"]
    (cv_builder.cv_dir() / "pending.json").write_text(json.dumps(meta))
    with pytest.raises(BuildBlocked, match="second.address@mail.com") as exc:
        build()
    assert exc.value.status == 422 and env.parse_calls == []


def test_the_api_key_never_reaches_logs_data_or_responses(env: FakeClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    upload()
    env.result = api_error(anthropic.AuthenticationError, 401, f"invalid x-api-key {SECRET_KEY}")
    build()
    assert cv_builder.get_status()["error"] == "Anthropic rejected the API key."
    env.result = good_structured()
    build()
    texts = [caplog.text, json.dumps(cv_builder.get_status())]
    with db.connect() as conn:
        for table in ("activity_log", "llm_usage", "cv_versions", "settings", "runs"):
            texts.append(json.dumps([dict(r) for r in conn.execute(f"SELECT * FROM {table}")], default=str))
    for path in config.DATA_DIR.rglob("*"):
        if path.is_file() and path.name != "forge.db" and not path.name.startswith("forge.db"):
            texts.append(path.read_text(errors="replace"))
    assert not any(SECRET_KEY in t for t in texts)
    raw_db = b"".join(p.read_bytes() for p in config.DATA_DIR.glob("forge.db*"))
    assert SECRET_KEY.encode() not in raw_db


# --- building the base CV ----------------------------------------------------------------------------------------------------

def test_fixture_cv_builds_a_valid_base_cv(env: FakeClient) -> None:
    upload()
    build()
    status = cv_builder.get_status()
    assert status["error"] is None and status["building"] is False and status["has_base"] and status["pending"] is None
    base = BaseCV.model_validate(base_json())
    assert base.name == "Jane Q. Doe"  # the real name is re-attached by code
    assert base.contact.emails == ["jane.doe@example.com"] and base.contact.phones == ["(617) 555-0123", "617.555.0123"]
    assert base.contact.linkedin == ["linkedin.com/in/janedoe"] and base.contact.urls == ["janedoe.dev"]
    assert "12 Main Street, Apt 4, Cambridge, MA 02139" in base.contact.addresses
    assert base.projects[0].description == "Ranks resumes against job posts, see https://github.com/janedoe/matcher"
    assert [e.company for e in base.experience] == ["Acme Corp", "Beta Labs"]
    assert (cv_builder.cv_dir() / "base_cv.txt").read_text() == CV.strip()
    assert not (cv_builder.cv_dir() / "pending.txt").exists()
    with db.connect() as conn:
        version = conn.execute("SELECT raw_filename, valid FROM cv_versions").fetchone()
    assert tuple(version) == ("cv.txt", 1)


def test_one_call_with_the_configured_model_and_a_low_cost_setup(env: FakeClient, monkeypatch: pytest.MonkeyPatch) -> None:
    upload()
    build()
    (call,) = env.parse_calls
    assert call["model"] == "claude-sonnet-5-5" and call["output_format"] is CVStructured
    assert call["output_config"] == {"effort": "medium"} and call["max_tokens"] == 16000
    assert not {"temperature", "top_p", "top_k", "tools", "tool_choice"} & set(call)

    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-5-5")
    upload()
    build()
    assert env.parse_calls[1]["model"] == "claude-haiku-5-5"


def test_usage_and_activity_are_logged_by_code(env: FakeClient) -> None:
    with db.connect() as conn:
        db.set_setting(conn, "claude_prices", {"input_per_mtok": 3.0, "output_per_mtok": 15.0})
    upload()
    build()
    with db.connect() as conn:
        usage = conn.execute("SELECT model, input_tokens, output_tokens, est_cost_usd, job_key FROM llm_usage").fetchone()
        log = [tuple(r) for r in conn.execute("SELECT agent_name, status, model_used FROM activity_log WHERE agent_name='cv-adapter' ORDER BY created_at")]
    assert usage["model"] == "claude-sonnet-5-5" and (usage["input_tokens"], usage["output_tokens"]) == (1500, 900)
    assert usage["est_cost_usd"] == pytest.approx((1500 * 3.0 + 900 * 15.0) / 1_000_000) and usage["job_key"] is None
    assert ("cv-adapter", "completed", "claude-sonnet-5-5") in log


def test_failed_validation_keeps_the_previous_base_cv(env: FakeClient) -> None:
    upload()
    build()
    before = (cv_builder.cv_dir() / "base_cv.json").read_text()
    upload(CV.replace("Jane Q. Doe", "Jane Q. Doe").replace("Beta Labs", "Gamma Labs"))
    env.result = good_structured(experience=[], education=[], skills=[])
    build()
    status = cv_builder.get_status()
    assert "not valid" in status["error"] and "no experience, education, or skills" in status["error"]
    assert (cv_builder.cv_dir() / "base_cv.json").read_text() == before  # untouched
    assert status["has_base"] and status["pending"] is not None  # the new upload is still there to retry
    assert "Beta Labs" in (cv_builder.cv_dir() / "base_cv.txt").read_text()
    with db.connect() as conn:
        assert [r["valid"] for r in conn.execute("SELECT valid FROM cv_versions ORDER BY id")] == [1, 0]
        failed = conn.execute("SELECT base_cv_json FROM cv_versions WHERE valid = 0").fetchone()
    assert failed["base_cv_json"] is None


@pytest.mark.parametrize(("error", "message"), [
    (api_error(anthropic.AuthenticationError, 401, "bad"), "Anthropic rejected the API key."),
    (api_error(anthropic.RateLimitError, 429), "Anthropic is rate limiting requests. Try again in a minute."),
    (api_error(anthropic.InternalServerError, 500), "The Anthropic API returned an error (500)."),
    (api_error(anthropic.BadRequestError, 400, "echoed CV text"), "The Anthropic API returned an error (400)."),
    (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")),
     "Could not reach the Anthropic API."),
])
def test_api_errors_become_safe_messages(env: FakeClient, error: Exception, message: str) -> None:
    upload()
    env.result = error
    build()
    assert cv_builder.get_status()["error"] == message
    assert cv_builder.get_status()["has_base"] is False and cv_builder.get_status()["pending"] is not None
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM activity_log WHERE status='failed' AND agent_name='cv-adapter'").fetchone()[0] == 1


@pytest.mark.parametrize(("stop_reason", "message"), [("refusal", "declined"), ("max_tokens", "cut off")])
def test_refusal_and_truncation_are_reported(env: FakeClient, stop_reason: str, message: str) -> None:
    upload()
    env.stop_reason = stop_reason
    build()
    assert message in cv_builder.get_status()["error"] and not (cv_builder.cv_dir() / "base_cv.json").exists()


def test_missing_api_key_is_reported(env: FakeClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm, "get_client", REAL_GET_CLIENT)  # undo the fake for this test
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.NotConfigured, match="ANTHROPIC_API_KEY"):
        llm.get_client()
    upload()
    build()
    assert "ANTHROPIC_API_KEY is not set" in cv_builder.get_status()["error"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET_KEY)
    assert llm.get_client().api_key == SECRET_KEY


def test_confirmations_and_preconditions_are_enforced(env: FakeClient) -> None:
    with pytest.raises(BuildBlocked) as exc:
        build()
    assert exc.value.status == 409 and "Upload" in str(exc.value)
    upload()
    for flags in ({"cost": False}, {"contacts": False}, {"cost": False, "contacts": False}):
        with pytest.raises(BuildBlocked) as exc:
            build(**flags)
        assert exc.value.status == 400 and "Confirm" in str(exc.value)
    assert env.parse_calls == []


def test_a_name_must_be_known_before_building(env: FakeClient) -> None:
    upload("Skills: Python, SQL\nExperience: Acme Corp, ML Intern, 2024")
    with pytest.raises(BuildBlocked, match="No name"):
        build()
    cv_builder.update_contacts(add=[{"kind": "name", "value": "Python"}])  # an explicit owner choice
    build()
    assert len(env.parse_calls) == 1


def test_only_one_build_at_a_time(env: FakeClient) -> None:
    upload()
    cv_builder._state["building"] = True
    with pytest.raises(BuildBlocked) as exc:
        build()
    assert exc.value.status == 409 and env.parse_calls == []


# --- profile suggestions ----------------------------------------------------------------------------------------------------------

def test_suggestions_extend_defaults_without_overwriting_owner_edits(env: FakeClient) -> None:
    upload()
    build()
    profile = load_profile()
    assert profile.titles == list(dict.fromkeys(Profile().titles))  # all three suggestions were already default titles
    assert "natural language processing models and tools" not in profile.scoring_skills  # over the 4-word limit
    assert {"Python", "PyTorch", "SQL", "Docker"} <= set(profile.scoring_skills)
    assert profile.scoring_skills[:16] == Profile().scoring_skills  # defaults first
    assert "data analysis tools platform" not in profile.keywords and "NLP" in profile.keywords  # 2-word limit
    assert profile.seniority == "entry"

    with db.connect() as conn:  # the owner changed skills and seniority in Settings
        db.set_setting(conn, "profile", {"scoring_skills": ["Rust"], "seniority": "senior"})
    edited = load_profile()
    assert edited.scoring_skills == ["Rust"] and edited.seniority == "senior"
    assert "NLP" in edited.keywords and edited.titles == profile.titles  # settings the owner did not touch still merge


def test_new_skills_are_merged_into_the_default_list(env: FakeClient) -> None:
    env.result = good_structured(suggested_profile={
        "titles": ["Robotics Engineer"], "keywords": ["robotics"], "scoring_skills": ["ROS", "Python", "Gazebo"],
        "seniority": "Expert"})
    upload()
    build()
    profile = load_profile()
    assert profile.scoring_skills[-2:] == ["ROS", "Gazebo"] and profile.scoring_skills.count("Python") == 1
    assert "Robotics Engineer" in profile.titles and "robotics" in profile.keywords
    assert profile.seniority == "entry"  # "Expert" is not an allowed value, so the default stands


def test_a_suggestion_failure_never_breaks_the_build(env: FakeClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(cv_builder, "merge_suggestions", boom)
    upload()
    build()
    assert cv_builder.get_status()["error"] is None and cv_builder.get_status()["has_base"]


# --- cost estimate and the monthly cap --------------------------------------------------------------------------------------------------

def test_estimate_uses_counted_tokens_and_settings_prices(env: FakeClient) -> None:
    upload()
    shown = cv_builder.estimate()
    assert shown["basis"] == "counted" and shown["input_tokens"] == 1234 and shown["cost_usd"] is None
    assert shown["model"] == "claude-sonnet-5-5" and env.count_calls[0]["system"] == cv_builder.load_prompt()
    with db.connect() as conn:
        db.set_setting(conn, "claude_prices", {"input_per_mtok": 3.0, "output_per_mtok": 15.0})
    priced = cv_builder.estimate()
    assert priced["cost_usd"] == pytest.approx((1234 * 3.0 + priced["output_tokens_estimate"] * 15.0) / 1e6)


def test_estimate_falls_back_to_an_approximation(env: FakeClient) -> None:
    upload()
    env.count_error = api_error(anthropic.InternalServerError, 500)
    shown = cv_builder.estimate()
    assert shown["basis"] == "approximate" and shown["input_tokens"] > 100


def test_monthly_cap_is_a_hard_stop(env: FakeClient) -> None:
    upload()
    with db.connect() as conn:
        db.set_setting(conn, "monthly_cap_usd", 1.0)
    build()
    assert "prices are missing" in cv_builder.get_status()["error"] and env.parse_calls == []

    with db.connect() as conn:
        db.set_setting(conn, "claude_prices", {"input_per_mtok": 3.0, "output_per_mtok": 15.0})
        db.record_llm_usage(conn, None, "claude-sonnet-5-5", 1, 1, 0.999)  # almost at the cap already
    build()
    assert "Monthly cap of $1.00 would be exceeded" in cv_builder.get_status()["error"] and env.parse_calls == []

    with db.connect() as conn:
        db.set_setting(conn, "monthly_cap_usd", 50.0)
    build()
    assert cv_builder.get_status()["error"] is None and len(env.parse_calls) == 1


def test_spend_only_counts_this_month(env: FakeClient) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO llm_usage (ts, model, input_tokens, output_tokens, est_cost_usd) VALUES ('2020-01-05T00:00:00+00:00', 'm', 1, 1, 9.0)")
        db.record_llm_usage(conn, None, "m", 1, 1, 0.5)
        assert db.month_spend(conn) == pytest.approx(0.5)


# --- endpoints ---------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture()
def client(env: FakeClient) -> Iterator[TestClient]:
    with TestClient(app, base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


def post_cv(client: TestClient, body: bytes = CV.encode(), filename: str = "cv.txt") -> Any:
    return client.post("/api/cv", params={"filename": filename}, content=body, headers={**HEADERS, "Content-Type": "application/octet-stream"})


def test_cv_endpoints_end_to_end(client: TestClient, env: FakeClient) -> None:
    assert client.get("/api/cv").json() == {"building": False, "error": None, "has_base": False, "base_cv": None, "pending": None}
    resp = post_cv(client)
    assert resp.status_code == 200 and resp.json()["pending"]["name"] == "Jane Q. Doe"

    preview = client.get("/api/cv/preview").json()
    assert "[NAME]" in preview["text"] and "Jane" not in preview["text"] and preview["possible_leftovers"] == []
    estimate = client.get("/api/cv/estimate").json()
    assert estimate["input_tokens"] == 1234 and estimate["basis"] == "counted"

    fixed = client.post("/api/cv/contacts", json={"add": [{"kind": "address", "value": "Boston, MA"}]}, headers=HEADERS)
    assert fixed.status_code == 200 and any(i["value"] == "Boston, MA" for i in fixed.json()["pending"]["items"])

    assert client.post("/api/cv/build", json={}, headers=HEADERS).status_code == 400
    started = client.post("/api/cv/build", json={"confirm_cost": True, "confirm_contacts": True}, headers=HEADERS)
    assert started.status_code == 202
    cv_builder.wait_for_build()
    status = client.get("/api/cv").json()
    assert status["has_base"] and status["base_cv"]["name"] == "Jane Q. Doe" and status["pending"] is None


def test_cv_endpoint_errors(client: TestClient) -> None:
    assert client.post("/api/cv", params={"filename": "cv.txt"}, content=b"x").status_code == 403  # CSRF header
    assert post_cv(client, filename="cv.exe").status_code == 415
    assert post_cv(client, body=b"").status_code == 400
    assert post_cv(client, body=b"x" * (extract_text.MAX_UPLOAD_BYTES + 1)).status_code == 413
    assert client.post("/api/cv", content=b"x", headers=HEADERS).status_code == 422  # filename is required
    for path in ("/api/cv/preview", "/api/cv/estimate"):
        assert client.get(path).status_code == 409
    assert client.post("/api/cv/contacts", json={"name": "X Y"}, headers=HEADERS).status_code == 409
    assert client.post("/api/cv/contacts", json={"add": [{"kind": "bogus", "value": "x"}]}, headers=HEADERS).status_code == 422
    assert client.post("/api/cv/build", json={"confirm_cost": True, "confirm_contacts": True}, headers=HEADERS).status_code == 409


def test_upload_streaming_cap_applies_without_a_content_length(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(6):
            yield b"x" * 1_000_000

    resp = client.post("/api/cv", params={"filename": "cv.txt"}, content=chunks(), headers=HEADERS)
    assert resp.status_code == 413


# --- structure of the code --------------------------------------------------------------------------------------------------------------------------------------

def test_only_llm_py_talks_to_anthropic() -> None:
    offenders = []
    for path in (config.APP_ROOT / "app").rglob("*.py"):
        text = path.read_text()
        if re.search(r"^\s*(import anthropic|from anthropic)", text, re.MULTILINE) and path.name != "llm.py":
            offenders.append(str(path.relative_to(config.APP_ROOT)))
    assert offenders == []


def test_base_cv_schema_rules() -> None:
    with pytest.raises(ValueError, match="no name"):
        BaseCV(name=" ", skills=[{"category": "x", "items": ["y"]}])
    with pytest.raises(ValueError, match="no experience"):
        BaseCV(name="Jane")
    assert BaseCV(name="Jane", skills=[{"category": "x", "items": ["y"]}]).contact.emails == []
