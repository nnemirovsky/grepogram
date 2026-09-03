"""Tokenizing, stemming and FTS5 query building for the lexical index.

Telegram search is exact-word, so ``счёт`` never finds ``счета`` and ``bank`` never finds
``banks``. The FTS tables therefore carry two indexed columns: ``raw`` (the text as written) and
``stemmed`` (the output of :func:`stem_text`), and a query is stemmed the same way before it is
matched, so an inflected query hits every inflection in the index while the raw column keeps
exact tokens such as ``CUIT`` or ``bge-m3`` searchable as typed.

:func:`tokenize` is deliberately plain: NFKC-normalize, lowercase, ``\\w+``. No stop words are
dropped and short tokens and digits are kept — ``ВНЖ``, ``DNI`` or ``2024`` are exactly what a
"find that thing" query is made of. :func:`stem_token` picks the Snowball stemmer by script
(Cyrillic → russian, Latin → english, anything else unchanged); Snowball also folds ``ё`` to
``е``, which the ``unicode61`` tokenizer does not, so the query side and the ``stemmed`` column
agree on either spelling.

:func:`fts_query` quotes every stem (``"tok"``) and joins the phrases with ``AND`` or ``OR``.
Quoting is what makes user text safe for ``MATCH``: a bare ``bge-m3`` or ``12:30`` is an FTS5
syntax error (``-`` and ``:`` are operators), while the quoted phrase is tokenized by
``unicode61`` into the same pieces the index holds. ``None`` means nothing survived
tokenization (emoji or punctuation only) and the caller decides how to degrade.
"""

import re
import threading
import unicodedata
from functools import lru_cache
from typing import Literal, Protocol

import snowballstemmer

FtsOp = Literal["AND", "OR"]
Language = Literal["russian", "english"]

TOKEN_RE = re.compile(r"\w+")
CYRILLIC_RE = re.compile(r"[\u0400-\u052f\u1c80-\u1c8f\u2de0-\u2dff\ua640-\ua69f]")
LATIN_RE = re.compile(r"[a-z\u00c0-\u024f]")
STEM_CACHE_SIZE = 1 << 16


class Stemmer(Protocol):
    def stemWord(self, word: str) -> str: ...


_STEMMERS: dict[Language, Stemmer] = {
    "russian": snowballstemmer.stemmer("russian"),
    "english": snowballstemmer.stemmer("english"),
}
_STEM_LOCK = threading.Lock()  # a Snowball stemmer keeps its cursor state on the instance


def tokenize(text: str) -> list[str]:
    """NFKC-normalized, lowercased ``\\w+`` tokens; digits and ``_`` kept, nothing dropped."""
    return TOKEN_RE.findall(unicodedata.normalize("NFKC", text).lower())


def language_of(token: str) -> Language | None:
    """Which Snowball stemmer applies: any Cyrillic letter wins, then any Latin letter."""
    if CYRILLIC_RE.search(token):
        return "russian"
    if LATIN_RE.search(token):
        return "english"
    return None


@lru_cache(maxsize=STEM_CACHE_SIZE)
def stem_token(token: str) -> str:
    """Stem one lowercase token by its script; other scripts and pure digits pass through."""
    language = language_of(token)
    if language is None:
        return token
    with _STEM_LOCK:
        return _STEMMERS[language].stemWord(token)


def stem_text(text: str) -> str:
    """The ``stemmed`` column: every token of ``text`` stemmed, space-separated."""
    return " ".join(stem_token(token) for token in tokenize(text))


def fts_query(text: str, op: FtsOp = "AND") -> str | None:
    """A ``MATCH`` expression over the stems of ``text``, or ``None`` when it has no tokens.

    Every stem becomes a quoted phrase and repeated stems are kept once, in first-seen order,
    so ``счёт счета`` asks for ``"счет"`` once instead of double-weighting it in ``bm25()``.
    """
    stems = list(dict.fromkeys(stem_token(token) for token in tokenize(text)))
    if not stems:
        return None
    return f" {op} ".join(f'"{stem}"' for stem in stems)
