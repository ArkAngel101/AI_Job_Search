"""Validate an uploaded CV file and extract its text. Uploaded content is only parsed, never executed."""
import subprocess
import zipfile
from pathlib import Path

from docx import Document

MAX_UPLOAD_BYTES = 5_000_000
MAX_TEXT_CHARS = 60_000
MAX_DOCX_UNZIPPED_BYTES = 50_000_000
ALLOWED_EXTENSIONS = ("pdf", "docx", "md", "txt")
PDF_TIMEOUT_SECONDS = 30


class UploadError(Exception):
    """The upload was rejected. `status` is the HTTP status to return."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def check_upload(filename: str, data: bytes) -> str:
    """Return the file extension if the upload is acceptable, else raise UploadError."""
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in ALLOWED_EXTENSIONS:
        raise UploadError(f"Only {', '.join(ALLOWED_EXTENSIONS)} files are accepted", 415)
    if not data:
        raise UploadError("The file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"The file is larger than {MAX_UPLOAD_BYTES // 1_000_000} MB", 413)
    if extension == "pdf" and not data.startswith(b"%PDF"):
        raise UploadError("That file is not a PDF", 415)
    if extension == "docx" and not data.startswith(b"PK"):
        raise UploadError("That file is not a Word document", 415)
    if extension in ("md", "txt") and b"\x00" in data:
        raise UploadError("That file is not plain text", 415)
    return extension


def extract_text(path: Path, extension: str) -> str:
    """Plain text of a PDF (pdftotext), DOCX (python-docx, including headers and footers), MD, or TXT file."""
    if extension == "pdf":
        text = _pdf_text(path)
    elif extension == "docx":
        text = _docx_text(path)
    else:
        text = path.read_bytes().decode("utf-8", errors="replace")
    text = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines()).strip()
    if not text:
        raise UploadError("No text could be read from that file (a scanned PDF needs OCR first)")
    if len(text) > MAX_TEXT_CHARS:
        raise UploadError(f"The CV text is longer than {MAX_TEXT_CHARS:,} characters; please upload a shorter CV")
    return text


def _pdf_text(path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True, timeout=PDF_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise UploadError("The PDF could not be read", 422) from None
    if result.returncode != 0:
        raise UploadError("The PDF could not be read", 422)
    return result.stdout.decode("utf-8", errors="replace")


def _docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_DOCX_UNZIPPED_BYTES:
                raise UploadError("The Word document is too large once unpacked", 413)
        document = Document(str(path))
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise UploadError("The Word document could not be read", 422) from None
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        parts += [cell.text for row in table.rows for cell in row.cells]
    for section in document.sections:  # contact details often sit in the page header or footer
        parts += [p.text for p in section.header.paragraphs] + [p.text for p in section.footer.paragraphs]
    return "\n".join(parts)
