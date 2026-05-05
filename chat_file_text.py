"""Small text extraction helpers for chat attachments."""

from __future__ import annotations

import io
import mimetypes
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, Tuple

TEXT_ATTACHMENT_EXTS = {
    ".txt", ".md", ".csv", ".json", ".js", ".ts", ".tsx", ".jsx", ".py", ".html", ".css",
    ".xml", ".yml", ".yaml", ".sql", ".ini", ".cfg", ".log", ".rtf", ".sh", ".bat", ".php", ".java", ".go", ".rs"
}


def detect_attachment_kind(filename: str, content_type: str = "") -> str:
    ext = Path(filename or "").suffix.lower()
    ctype = (content_type or mimetypes.guess_type(filename or "")[0] or "").lower()
    if ctype.startswith("image/"):
        return "image"
    if ctype.startswith("audio/") or ext in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}:
        return "audio"
    if ctype.startswith("video/") or ext in {".mp4", ".mov", ".webm", ".mkv"}:
        return "video"
    if ext == ".pdf" or ctype == "application/pdf":
        return "pdf"
    if ext == ".docx" or ctype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return "docx"
    if ext in TEXT_ATTACHMENT_EXTS or ctype.startswith("text/") or ctype in {"application/json", "application/xml", "text/csv", "application/javascript"}:
        return "text"
    return "binary"


def decode_text_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def extract_docx_text(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
    except Exception:
        return ""
    text = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    text = re.sub(r"<w:br[^>]*/>", "\n", text)
    text = re.sub(r"</w:p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf_text(raw: bytes, *, max_pages: int = 20) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return ""
    try:
        reader = PdfReader(io.BytesIO(raw))
        chunks = []
        for page in list(reader.pages)[:max_pages]:
            chunks.append(page.extract_text() or "")
        return "\n\n".join(x.strip() for x in chunks if x and x.strip()).strip()
    except Exception:
        return ""


def extract_file_text(raw: bytes, filename: str, content_type: str = "") -> Tuple[str, str, str]:
    """Return (kind, extracted_text, notice)."""
    kind = detect_attachment_kind(filename, content_type)
    text = ""
    notice = ""
    if kind == "text":
        text = decode_text_bytes(raw)
    elif kind == "docx":
        text = extract_docx_text(raw)
        if not text:
            notice = f"{filename}: не удалось извлечь текст из DOCX."
    elif kind == "pdf":
        text = extract_pdf_text(raw)
        if not text:
            notice = f"{filename}: PDF принят, но текст извлечь не удалось. Возможно, это скан или не установлен pypdf."
    else:
        notice = f"{filename}: формат {kind} пока не разбирается как текст."
    return kind, (text or "").replace("\x00", "").strip(), notice


def file_meta(filename: str, kind: str, content_type: str, size_bytes: int, parsed: bool = False) -> Dict[str, Any]:
    return {
        "name": Path(filename or "file").name or "file",
        "kind": kind,
        "content_type": content_type or mimetypes.guess_type(filename or "")[0] or "application/octet-stream",
        "size_bytes": int(size_bytes or 0),
        "parsed": bool(parsed),
    }
