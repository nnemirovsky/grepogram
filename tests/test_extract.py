"""The extractor registry, the document dispatcher, and ``FakeClient``'s fetch/download pair.

The committed fixtures (``tests/fixtures/sample.pdf``, ``tests/fixtures/sample.docx``) are the
round-trip cases; the ``_write_*`` helpers build the oversized and malformed files a fixture
should not be. Nothing here downloads anything.
"""

import importlib.util
import sys
import zipfile
from pathlib import Path

import docx
import pytest

from grepogram import extract
from grepogram.extract import EXTRACT_MAX_CHARS, ExtractError
from tests.fakes import FakeClient, make_message

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_PDF = FIXTURES / "sample.pdf"
SAMPLE_DOCX = FIXTURES / "sample.docx"


def _write_pdf(path: Path, lines: list[str]) -> Path:
    """A one-page PDF drawing ``lines``, written by hand so no generator library is needed."""
    drawn = b"".join(b"(" + line.encode("ascii") + b") Tj 0 -20 Td " for line in lines)
    stream = b"BT /F1 14 Tf 72 720 Td " + drawn + b"ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    path.write_bytes(bytes(out))
    return path


def _write_docx(path: Path, paragraphs: list[str], table: list[list[str]] | None = None) -> Path:
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        built = document.add_table(rows=len(table), cols=len(table[0]))
        for row, cells in zip(built.rows, table, strict=True):
            for cell, text in zip(row.cells, cells, strict=True):
                cell.text = text
    document.save(str(path))
    return path


# --- the committed fixtures ------------------------------------------------------------------


def test_pdf_fixture_round_trips() -> None:
    text = extract.extract_document(SAMPLE_PDF)
    assert text.splitlines() == ["Grepogram sample PDF", "Embassy notice 2026"]


def test_docx_fixture_round_trips() -> None:
    text = extract.extract_document(SAMPLE_DOCX)
    assert text.splitlines() == ["Grepogram sample DOCX", "Rental contract clause"]


def test_fixtures_stay_small() -> None:
    """Committed binaries, so they are hand-built rather than downloaded — keep them tiny."""
    assert SAMPLE_PDF.stat().st_size < 4096
    assert SAMPLE_DOCX.stat().st_size < 4096


# --- dispatch --------------------------------------------------------------------------------


def test_docx_extension_over_pdf_bytes_is_rejected(tmp_path: Path) -> None:
    mislabelled = tmp_path / "notice.docx"
    mislabelled.write_bytes(SAMPLE_PDF.read_bytes())
    with pytest.raises(ExtractError, match="does not hold docx data"):
        extract.extract_document(mislabelled)


def test_pdf_extension_over_docx_bytes_is_rejected(tmp_path: Path) -> None:
    mislabelled = tmp_path / "contract.pdf"
    mislabelled.write_bytes(SAMPLE_DOCX.read_bytes())
    with pytest.raises(ExtractError, match="does not hold pdf data"):
        extract.extract_document(mislabelled)


def test_dispatch_ignores_extension_case(tmp_path: Path) -> None:
    shouted = tmp_path / "NOTICE.PDF"
    shouted.write_bytes(SAMPLE_PDF.read_bytes())
    assert "Embassy notice 2026" in extract.extract_document(shouted)


def test_unknown_extension_has_no_extractor(tmp_path: Path) -> None:
    other = tmp_path / "prices.xlsx"
    other.write_bytes(b"PK\x03\x04whatever")
    with pytest.raises(ExtractError, match="no document extractor for .xlsx"):
        extract.extract_document(other)


def test_a_name_with_no_extension_has_no_extractor(tmp_path: Path) -> None:
    nameless = tmp_path / "attachment"
    nameless.write_bytes(SAMPLE_PDF.read_bytes())
    with pytest.raises(ExtractError, match="no document extractor for a name with no extension"):
        extract.extract_document(nameless)


def test_a_missing_file_is_an_extract_error(tmp_path: Path) -> None:
    with pytest.raises(ExtractError, match="cannot read gone.pdf"):
        extract.extract_document(tmp_path / "gone.pdf")


# --- failures --------------------------------------------------------------------------------


def test_corrupt_pdf_is_an_extract_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nnot really a pdf at all\n")
    with pytest.raises(ExtractError, match="cannot read broken.pdf as a PDF"):
        extract.extract_document(broken)


def test_corrupt_docx_is_an_extract_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"PK\x03\x04not a real zip")
    with pytest.raises(ExtractError, match="cannot read broken.docx as a DOCX"):
        extract.extract_document(broken)


def test_a_zip_that_is_not_a_docx_is_an_extract_error(tmp_path: Path) -> None:
    """Magic bytes only prove it is a zip; ``python-docx`` is what proves it is a document."""
    not_a_document = tmp_path / "photos.docx"
    with zipfile.ZipFile(not_a_document, "w") as archive:
        archive.writestr("hello.txt", "no OOXML parts here")
    with pytest.raises(ExtractError, match="cannot read photos.docx as a DOCX"):
        extract.extract_document(not_a_document)


def test_pdf_without_pypdf_is_an_extract_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(ExtractError, match="pypdf is not installed"):
        extract.extract_pdf(SAMPLE_PDF)


def test_docx_without_python_docx_is_an_extract_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "docx", None)
    with pytest.raises(ExtractError, match="python-docx is not installed"):
        extract.extract_docx(SAMPLE_DOCX)


# --- what the extractors read ------------------------------------------------------------------


def test_docx_reads_tables_too(tmp_path: Path) -> None:
    path = _write_docx(
        tmp_path / "prices.docx", ["Price list"], table=[["Visa", "120 EUR"], ["NIE", "12 EUR"]]
    )
    text = extract.extract_document(path)
    assert text.splitlines() == ["Price list", "Visa", "120 EUR", "NIE", "12 EUR"]


def test_blank_lines_are_dropped(tmp_path: Path) -> None:
    path = _write_docx(tmp_path / "sparse.docx", ["First", "", "   ", "Second"])
    assert extract.extract_document(path).splitlines() == ["First", "Second"]


def test_an_empty_document_extracts_to_nothing(tmp_path: Path) -> None:
    path = _write_docx(tmp_path / "empty.docx", [])
    assert extract.extract_document(path) == ""


# --- the length cap ----------------------------------------------------------------------------


def test_a_long_docx_is_capped(tmp_path: Path) -> None:
    path = _write_docx(tmp_path / "long.docx", ["x" * 100 for _ in range(100)])
    text = extract.extract_document(path)
    assert len(text) == EXTRACT_MAX_CHARS


def test_a_long_pdf_is_capped(tmp_path: Path) -> None:
    path = _write_pdf(tmp_path / "long.pdf", ["y" * 80 for _ in range(100)])
    text = extract.extract_document(path)
    assert len(text) == EXTRACT_MAX_CHARS


def test_text_at_the_cap_is_kept_whole(tmp_path: Path) -> None:
    line = "z" * EXTRACT_MAX_CHARS
    path = _write_docx(tmp_path / "exact.docx", [line])
    assert extract.extract_document(path) == line


# --- the registry ------------------------------------------------------------------------------


def test_registry_maps_document() -> None:
    assert extract.registry()["document"] is extract.extract_document


def test_registry_leaves_the_kinds_with_no_extractor_out() -> None:
    """An absent kind is marked unsupported by the pass; voice and video notes wait for v0.3.0."""
    built = extract.registry()
    assert "photo" not in built
    assert "voice" not in built
    assert "video_note" not in built
    assert "video" not in built


def test_registry_drops_documents_when_the_libraries_are_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extract, "_documents_available", lambda: False)
    assert extract.registry() == {}


def test_registry_is_derived_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """An import-time constant could not answer differently after the environment changes."""
    assert "document" in extract.registry()
    monkeypatch.setattr(extract, "_documents_available", lambda: False)
    assert "document" not in extract.registry()


def test_documents_are_available_when_either_library_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert extract._documents_available() is True
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: None if name == "pypdf" else object()
    )
    assert extract._documents_available() is True
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert extract._documents_available() is False


# --- FakeClient's fetch and download -----------------------------------------------------------

CHAT_ID = -1001234567890


async def test_fake_get_messages_answers_none_for_a_missing_id() -> None:
    client = FakeClient(messages={CHAT_ID: [make_message(CHAT_ID, 1, "one")]})
    got = await client.get_messages(CHAT_ID, ids=[1, 2])
    assert [None if m is None else m.id for m in got] == [1, None]


async def test_fake_get_messages_answers_one_message_for_an_int_id() -> None:
    client = FakeClient(messages={CHAT_ID: [make_message(CHAT_ID, 7, "seven")]})
    one = await client.get_messages(CHAT_ID, ids=7)
    assert one.id == 7
    assert await client.get_messages(CHAT_ID, ids=8) is None


async def test_fake_get_messages_defaults_to_one_message() -> None:
    client = FakeClient(
        messages={CHAT_ID: [make_message(CHAT_ID, 1, "one"), make_message(CHAT_ID, 2, "two")]}
    )
    got = await client.get_messages(CHAT_ID)
    assert [m.id for m in got] == [2]
    assert ("get_messages", {"chat_id": CHAT_ID, "limit": None, "ids": None}) in client.calls


async def test_fake_download_media_writes_the_registered_bytes(tmp_path: Path) -> None:
    message = make_message(CHAT_ID, 3, "photo")
    client = FakeClient(downloads={(CHAT_ID, 3): b"%PDF-bytes"})
    target = tmp_path / "3.pdf"
    assert await client.download_media(message, file=target) == str(target)
    assert target.read_bytes() == b"%PDF-bytes"
    assert ("download_media", {"chat_id": CHAT_ID, "msg_id": 3, "file": target}) in client.calls


async def test_fake_download_media_is_none_when_nothing_is_registered(tmp_path: Path) -> None:
    client = FakeClient()
    target = tmp_path / "3.pdf"
    assert await client.download_media(make_message(CHAT_ID, 3, "photo"), file=target) is None
    assert not target.exists()


async def test_fake_download_media_raises_a_registered_error(tmp_path: Path) -> None:
    client = FakeClient(downloads={(CHAT_ID, 3): OSError("disk full")})
    with pytest.raises(OSError, match="disk full"):
        await client.download_media(make_message(CHAT_ID, 3, "photo"), file=tmp_path / "3.pdf")
