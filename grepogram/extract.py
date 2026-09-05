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
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

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

OCR_LANGUAGES = ("ru-RU", "en-US")
"""What OCR asks Vision for, best first — these are Russian and English chats.

Never handed to Vision as it is. A ``VNRecognizeTextRequest`` given a language the build does not
recognise fails outright rather than ignoring it, and Vision only learned Russian in macOS 15, so
the request carries this narrowed to ``supportedRecognitionLanguages`` by
:func:`_requested_languages`.
"""

_PDF_MAGIC = b"%PDF-"
_DOCX_MAGIC = b"PK\x03\x04"
"""A DOCX is an OOXML zip, so its magic is the zip local-file header."""

DOCUMENT_SUFFIXES = (".pdf", ".docx")
"""File extensions :func:`extract_document` can dispatch on, lowercased.

``document`` is ``sync.document_kind``'s fallback, so ``.xlsx``, ``.zip``, ``.apk`` and every
other attachment lands on that kind as well — and the stored ``messages.media_filename`` is
enough to tell them apart before anything is fetched. The extraction pass parks those rows
offline against this tuple (:func:`grepogram.db.park_unreadable_documents`) rather than
downloading a 5 MB spreadsheet to discover the dispatcher below has nowhere to send it.
"""


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
    built: dict[MediaKind, Extractor] = {}
    if _documents_available():
        built["document"] = extract_document
    else:
        log.debug("no document extractor: the 'media' extra brings pypdf and python-docx")
    reason = _ocr_unavailable()
    if reason is None:
        built["photo"] = ocr_image
    else:
        log.debug("no OCR extractor: %s", reason)
    # "voice" and "video_note" stay unmapped on purpose: whisper.cpp transcription is v0.3.0.
    return built


def _documents_available() -> bool:
    """Whether either document library is importable (the ``media`` extra brings both).

    Either is enough to register ``document``: the dispatcher picks per file, and a PDF still
    reads when only ``pypdf`` is installed. The missing half raises :class:`ExtractError` for
    its own files, which is retryable — installing the extra is what fixes it.
    """
    return any(importlib.util.find_spec(name) is not None for name in ("pypdf", "docx"))


def _ocr_unavailable() -> str | None:
    """Why photo OCR cannot run here, or ``None`` when it can.

    A reason rather than a flag so :func:`registry` can say in one debug line what would
    fix it. Both halves matter: Vision is a macOS framework, and its Python binding lives in the
    ``media`` extra. Absent either, ``photo`` stays unmapped and the extraction pass parks such
    media as unsupported instead of failing every photo it meets.
    """
    if sys.platform != "darwin":
        return f"macOS Vision needs darwin, not {sys.platform}"
    if importlib.util.find_spec("Vision") is None:
        return "the 'media' extra brings pyobjc-framework-Vision"
    return None


def ocr_image(path: Path) -> str:
    """Text macOS Vision recognises in an image, capped like every other extractor.

    A photo with no text in it reads as ``""`` — an empty extraction, not a failure — because
    that is the honest answer for the majority of photos a chat posts.
    """
    try:
        vision = _vision()
    except ImportError as exc:
        raise ExtractError(
            "pyobjc-framework-Vision is not installed; install the 'media' extra"
        ) from exc
    try:
        lines = _recognise(vision, path.read_bytes())
    except ExtractError:
        raise
    except Exception as exc:
        raise ExtractError(f"cannot read text from {path.name}: {exc}") from exc
    return _capped("\n".join(lines))


def _vision() -> Any:
    """The ``Vision`` framework itself — the one call in this module CI cannot make.

    Every test replaces this with a stand-in module, so the suite never needs a Mac with
    ``pyobjc-framework-Vision`` installed and the surface that cannot run there is this import
    rather than the extractor built on top of it.
    """
    import Vision

    return Vision


def _recognise(vision: Any, image: bytes) -> list[str]:
    """One accurate-level text request over image bytes, its recognised lines in Vision's order.

    Accurate rather than fast: this runs once per photo, off the sync's budget, and a
    photographed announcement is exactly the case the fast path reads wrong.
    """
    request = vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    supported, error = request.supportedRecognitionLanguagesAndReturnError_(None)
    if error is not None:
        raise ExtractError(f"Vision cannot report its recognition languages: {error}")
    request.setRecognitionLanguages_(_requested_languages(supported))
    handler = vision.VNImageRequestHandler.alloc().initWithData_options_(image, {})
    done, error = handler.performRequests_error_([request], None)
    if not done:
        raise ExtractError(f"Vision could not read the image: {error}")
    lines: list[str] = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if candidates:
            lines.append(candidates[0].string())
    return lines


def _requested_languages(supported: Sequence[str]) -> list[str]:
    """:data:`OCR_LANGUAGES` narrowed to what this build offers, in Vision's own spelling.

    A whole tag wins over a shared primary subtag, so ``en-US`` is preferred to ``en-GB`` where
    both are offered and ``en-GB`` still stands in where it is not. A build that offers neither
    language yields an empty list, which Vision reads as "use your default" — an unsupported
    language is dropped, never sent, because sending it fails the whole request.
    """
    chosen: list[str] = []
    for wanted in OCR_LANGUAGES:
        exact = next((o for o in supported if o.lower() == wanted.lower()), None)
        loose = next((o for o in supported if _primary(o) == _primary(wanted)), None)
        offer = exact or loose
        if offer is not None and offer not in chosen:
            chosen.append(offer)
    return chosen


def _primary(tag: str) -> str:
    """The primary subtag of a BCP 47 tag, lowercased: ``ru`` out of ``ru-RU``."""
    return tag.split("-", 1)[0].lower()


def extract_document(path: Path) -> str:
    """Text from a PDF or a DOCX, chosen by extension and confirmed by magic bytes.

    ``MediaKind`` cannot tell the two apart — ``sync.document_kind`` calls both ``document`` —
    so the file name is what does, and the first bytes are what stop a mislabelled file from
    being handed to the wrong parser. Anything else is an :class:`ExtractError`, though the pass
    parks most of those offline against :data:`DOCUMENT_SUFFIXES` and never gets here.
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
