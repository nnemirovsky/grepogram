"""Text out of the media a message carries: PDFs, DOCX files, photos through macOS OCR.

The extraction pass downloads a file and asks this module what it says. Every extractor is a
``Callable[[Path], str]``; :func:`registry` maps the :data:`~grepogram.models.MediaKind` a
message stores to the one that reads it, and a kind the registry has no entry for is media this
build cannot read at all (``db.MEDIA_UNSUPPORTED``) rather than a failure to retry.

The registry is **derived on every call, never frozen at import**. What a kind maps to is a
property of the environment: ``pypdf`` and ``python-docx`` come with the ``media`` extra, and OCR
needs macOS and ``pyobjc-framework-Vision``. An installation without them registers less and the
pass parks that media as unsupported; nothing raises and nothing is lost.

``MediaKind`` has a single ``document`` member for both PDF and DOCX (``sync.document_kind``
maps them together), so the document entry is itself a dispatcher: the extension of the
downloaded file picks the format and the file's magic bytes confirm it, because a ``.docx``
holding a PDF is a mislabelled file, not a DOCX to feed to ``python-docx``.

``voice`` and ``video_note`` are deliberately unmapped — whisper.cpp transcription is v0.3.0.
"""

import importlib.util
import logging
from collections.abc import Callable
from pathlib import Path

from grepogram.models import MediaKind

log = logging.getLogger(__name__)

EXTRACT_MAX_CHARS = 4000
"""How much text one file contributes to its message, at most.

A message's extracted text is rendered into the unit that holds it, and a single message longer
than ``units.window_max_chars`` (1500) forms a window of its own, so this bounds that window at a
few times the cap instead of letting a 400-page PDF become one enormous unit that no embedder
sees the end of. Extractors stop reading once they are past it rather than parsing the rest.
"""

Extractor = Callable[[Path], str]
"""What every extractor is: a downloaded file in, its text out, :class:`ExtractError` on failure."""

_PDF_MAGIC = b"%PDF-"
_DOCX_MAGIC = b"PK\x03\x04"
"""A DOCX is an OOXML zip, so its magic is the zip local-file header."""


class ExtractError(Exception):
    """This file's text cannot be read: a corrupt file, a mislabelled one, a missing library.

    The pass records it as ``db.MEDIA_FAILED`` — retryable, because the next attempt may run
    with the ``media`` extra installed or against a file that downloaded cleanly.
    """


def registry() -> dict[MediaKind, Extractor]:
    """The extractors this installation can run, re-derived on every call.

    Not a module constant: availability depends on what imports here and on the platform, and
    the tests have to be able to build the map again with a different answer to both.
    """
    return _build_registry()


def _build_registry() -> dict[MediaKind, Extractor]:
    built: dict[MediaKind, Extractor] = {}
    if _documents_available():
        built["document"] = extract_document
    else:
        log.debug("no document extractor: the 'media' extra brings pypdf and python-docx")
    # "voice" and "video_note" stay unmapped on purpose: whisper.cpp transcription is v0.3.0.
    return built


def _documents_available() -> bool:
    """Whether either document library is importable (the ``media`` extra brings both).

    Either is enough to register ``document``: the dispatcher picks per file, and a PDF still
    reads when only ``pypdf`` is installed. The missing half raises :class:`ExtractError` for
    its own files, which is retryable — installing the extra is what fixes it.
    """
    return any(importlib.util.find_spec(name) is not None for name in ("pypdf", "docx"))


def extract_document(path: Path) -> str:
    """Text from a PDF or a DOCX, chosen by extension and confirmed by magic bytes.

    ``MediaKind`` cannot tell the two apart — ``sync.document_kind`` calls both ``document`` —
    so the file name is what does, and the first bytes are what stop a mislabelled file from
    being handed to the wrong parser. Anything else is an :class:`ExtractError`.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        magic, extract = _PDF_MAGIC, extract_pdf
    elif suffix == ".docx":
        magic, extract = _DOCX_MAGIC, extract_docx
    else:
        raise ExtractError(f"no document extractor for {suffix or 'a name with no extension'}")
    if not _head(path, len(magic)).startswith(magic):
        raise ExtractError(f"{path.name} does not hold {suffix.lstrip('.')} data")
    return extract(path)


def extract_pdf(path: Path) -> str:
    """Text from a PDF, page by page, stopping once :data:`EXTRACT_MAX_CHARS` is reached."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ExtractError("pypdf is not installed; install the 'media' extra") from exc
    pages: list[str] = []
    total = 0
    try:
        for page in PdfReader(str(path)).pages:
            text = page.extract_text() or ""
            pages.append(text)
            total += len(text)
            if total > EXTRACT_MAX_CHARS:
                break
    except Exception as exc:
        raise ExtractError(f"cannot read {path.name} as a PDF: {exc}") from exc
    return _capped("\n".join(pages))


def extract_docx(path: Path) -> str:
    """Text from a DOCX: its paragraphs, then its tables — a price list is usually a table."""
    try:
        import docx
    except ImportError as exc:
        raise ExtractError("python-docx is not installed; install the 'media' extra") from exc
    parts: list[str] = []
    try:
        document = docx.Document(str(path))
        parts.extend(paragraph.text for paragraph in document.paragraphs)
        for table in document.tables:
            for row in table.rows:
                parts.extend(cell.text for cell in row.cells)
    except Exception as exc:
        raise ExtractError(f"cannot read {path.name} as a DOCX: {exc}") from exc
    return _capped("\n".join(parts))


def _head(path: Path, size: int) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(size)
    except OSError as exc:
        raise ExtractError(f"cannot read {path.name}: {exc}") from exc


def _capped(text: str) -> str:
    """Blank lines dropped, every line stripped, the whole cut to :data:`EXTRACT_MAX_CHARS`.

    Line breaks survive — a form or a price list is its lines — and how a unit renders them is
    the renderer's decision, not this module's.
    """
    joined = "\n".join(line for line in (raw.strip() for raw in text.splitlines()) if line)
    if len(joined) <= EXTRACT_MAX_CHARS:
        return joined
    return joined[:EXTRACT_MAX_CHARS].rstrip()
