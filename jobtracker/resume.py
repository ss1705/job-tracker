"""Your resume, stored locally (data/, gitignored) as plain text for outreach drafting."""
import io
import re
import zipfile
from datetime import datetime, timezone

from . import config

ALLOWED = (".pdf", ".docx", ".txt", ".md")
MAX_BYTES = 5 * 1024 * 1024
MAX_CHARS = 14000


class ResumeError(ValueError):
    pass


def _text_path():
    return config.DATA_DIR / "resume.txt"


def _meta_path():
    return config.DATA_DIR / "resume.meta"


def extract_text(filename: str, data: bytes) -> str:
    ext = "." + filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext not in ALLOWED:
        raise ResumeError(f"Unsupported file type {ext or '(none)'}; use PDF, DOCX, TXT or MD.")
    if len(data) > MAX_BYTES:
        raise ResumeError("File is larger than 5 MB.")
    if ext == ".pdf":
        from pypdf import PdfReader
        try:
            text = "\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(data)).pages)
        except Exception as e:
            raise ResumeError(f"Couldn't read that PDF ({type(e).__name__}).")
    elif ext == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
        except Exception:
            raise ResumeError("Couldn't read that DOCX.")
        xml = re.sub(r"</w:p>|<w:br/>|<w:tab/>", "\n", xml)
        text = re.sub(r"<[^>]+>", "", xml)
        for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'")):
            text = text.replace(ent, ch)
    else:
        text = data.decode("utf-8", errors="ignore")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if len(text) < 80:
        raise ResumeError("Almost no text found. If this is a scanned PDF, export a text version or paste it into a .txt file.")
    return text[:MAX_CHARS]


def save(filename: str, data: bytes) -> dict:
    text = extract_text(filename, data)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _text_path().write_text(text, encoding="utf-8")
    _meta_path().write_text(f"{filename}\n{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n", encoding="utf-8")
    for p in (_text_path(), _meta_path()):
        p.chmod(0o600)
    return status()


def load_text() -> str:
    return _text_path().read_text(encoding="utf-8") if _text_path().exists() else ""


def status() -> dict:
    t = load_text()
    if not t:
        return {"present": False}
    name, at = (_meta_path().read_text(encoding="utf-8").splitlines() + ["", ""])[:2] if _meta_path().exists() else ("", "")
    return {"present": True, "filename": name, "updated": at, "words": len(t.split()), "preview": t[:300]}


def first_name() -> str:
    """Best guess at your first name: the first line of the resume that looks like a name."""
    for line in load_text().splitlines()[:6]:
        words = line.strip().split()
        if 2 <= len(words) <= 4 and all(w[0].isupper() for w in words if w[0].isalpha()) and not re.search(r"[@\d|•]", line):
            return words[0]
    return ""
