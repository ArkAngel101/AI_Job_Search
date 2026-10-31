"""Render a CV to .docx (python-docx) and convert it to PDF with headless LibreOffice."""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

SECTION_FILL = "D9E2F3"
ACCENT = RGBColor(0x1F, 0x3A, 0x5F)
PDF_TIMEOUT_SECONDS = 120
KEY_RE = re.compile(r"^[0-9a-f]{16}$")


def tailored_dir(base: Path) -> Path:
    """Folder for generated CVs."""
    return base / "cv" / "tailored"


def _shade(paragraph: Any, fill: str) -> None:
    properties = paragraph._p.get_or_add_pPr()
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), fill)
    properties.append(shading)


def _section_bar(document: Any, title: str) -> None:
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(8)
    paragraph.paragraph_format.space_after = Pt(3)
    run = paragraph.add_run(title.upper())
    run.bold = True
    run.font.size = Pt(10)
    run.font.color.rgb = ACCENT
    _shade(paragraph, SECTION_FILL)


def _line(document: Any, text: str, *, bold: bool = False, size: float = 10, italic: bool = False, after: float = 0) -> Any:
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(after)
    paragraph.paragraph_format.space_before = Pt(0)
    run = paragraph.add_run(text)
    run.bold, run.italic, run.font.size = bold, italic, Pt(size)
    return paragraph


def _bullets(document: Any, items: list[str]) -> None:
    for item in items:
        paragraph = document.add_paragraph(style="List Bullet")
        paragraph.paragraph_format.space_after = Pt(1)
        paragraph.add_run(item).font.size = Pt(10)


def _join(*parts: str, sep: str = "  |  ") -> str:
    return sep.join(p for p in parts if p)


def build_docx(cv: dict[str, Any], path: Path) -> None:
    """Write the CV (BaseCV-shaped dict) to a .docx file: name, headline, shaded section bars, and sections."""
    document = Document()
    section = document.sections[0]
    section.left_margin = section.right_margin = Inches(0.75)
    section.top_margin = section.bottom_margin = Inches(0.6)
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10)

    title = document.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_after = Pt(0)
    run = title.add_run(cv["name"])
    run.bold, run.font.size, run.font.color.rgb = True, Pt(20), ACCENT
    contact = cv.get("contact", {})
    links = [*contact.get("linkedin", []), *contact.get("github", []), *contact.get("urls", [])]
    details = _join(*contact.get("addresses", [])[:1], *contact.get("phones", [])[:1], *contact.get("emails", [])[:1])
    for text in (cv.get("headline", ""), details, _join(*links)):
        if text:
            line = _line(document, text, size=10.5 if text == cv.get("headline") else 9, bold=text == cv.get("headline"))
            line.alignment = WD_ALIGN_PARAGRAPH.CENTER

    if cv.get("summary"):
        _section_bar(document, "Summary")
        _line(document, cv["summary"])
    if cv.get("competencies"):
        _section_bar(document, "Core competencies")
        _line(document, _join(*cv["competencies"], sep="  •  "))
    if cv.get("experience"):
        _section_bar(document, "Experience")
        for job in cv["experience"]:
            _line(document, _join(job["role"], job["company"], sep=" — "), bold=True, after=0)
            _line(document, _join(job.get("location", ""), _join(job.get("start", ""), job.get("end", ""), sep=" – ")),
                  italic=True, size=9, after=1)
            _bullets(document, job.get("bullets", []))
    if cv.get("education"):
        _section_bar(document, "Education")
        for school in cv["education"]:
            _line(document, _join(school["degree"], school["institution"], sep=" — "), bold=True)
            _line(document, _join(school.get("location", ""), _join(school.get("start", ""), school.get("end", ""), sep=" – ")),
                  italic=True, size=9, after=1)
            _bullets(document, school.get("details", []))
    if cv.get("skills"):
        _section_bar(document, "Skills")
        for group in cv["skills"]:
            _line(document, f"{group['category']}: {', '.join(group['items'])}")
    if cv.get("projects"):
        _section_bar(document, "Projects")
        for project in cv["projects"]:
            _line(document, project["name"], bold=True)
            if project.get("description"):
                _line(document, project["description"])
            _bullets(document, project.get("bullets", []))
    if cv.get("languages"):
        _section_bar(document, "Languages")
        _line(document, _join(*(f"{x['lang']} ({x['level']})" if x.get("level") else x["lang"] for x in cv["languages"]), sep=", "))

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    document.save(str(temp))
    os.replace(temp, path)


def convert_to_pdf(docx_path: Path) -> Path | None:
    """Convert with headless LibreOffice. Returns the PDF path, or None if conversion is unavailable or fails."""
    office = shutil.which("soffice") or shutil.which("libreoffice")
    if office is None:
        return None
    pdf_path = docx_path.with_suffix(".pdf")
    with tempfile.TemporaryDirectory(prefix="lo_") as scratch:
        env = {**os.environ, "HOME": scratch}  # the container's root filesystem is read-only
        command = [office, f"-env:UserInstallation=file://{scratch}/profile", "--headless", "--norestore",
                   "--convert-to", "pdf", "--outdir", scratch, str(docx_path)]
        try:
            result = subprocess.run(command, capture_output=True, timeout=PDF_TIMEOUT_SECONDS, check=False, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return None
        produced = Path(scratch) / docx_path.with_suffix(".pdf").name
        if result.returncode != 0 or not produced.exists():
            return None
        temp = pdf_path.with_name(pdf_path.name + ".tmp")
        shutil.move(str(produced), str(temp))
        os.replace(temp, pdf_path)
    return pdf_path


def export_cv(cv: dict[str, Any], key: str, base: Path) -> tuple[Path, Path | None]:
    """Write <key>.docx and, if possible, <key>.pdf into the tailored folder."""
    if not KEY_RE.match(key):
        raise ValueError("invalid job key")
    docx_path = tailored_dir(base) / f"{key}.docx"
    build_docx(cv, docx_path)
    return docx_path, convert_to_pdf(docx_path)
