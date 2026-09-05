"""Dense embeddings: the :class:`Embedder` protocol, a deterministic fake and ``bge-m3``.

The dense side of search turns every unit and every query into one vector, and everything that
consumes those vectors (the ``unit_vec`` table, KNN, the hybrid ranking) only needs the three
things the protocol names: a ``name`` that identifies the embedding space (a vector from one
model is meaningless next to a vector from another, so ``meta.embed_model`` is compared against
it before anything is embedded), a ``dim`` the vector table is created for, and the two
``embed`` calls.

:class:`BgeM3Embedder` runs ``BAAI/bge-m3`` through sentence-transformers on the Mac GPU. The
import is lazy and every failure to import or load — the ``dense`` extra not installed, no
network for the first download, a broken cache — becomes :class:`ModelUnavailable`, so callers
degrade to lexical search with a warning instead of crashing; nothing in this module touches
torch at import time, which is what keeps the MCP server and the CLI usable without the extra.
The load itself goes through :func:`load_cached_first`, which keeps a cached model off the
network entirely and downloads only when the cache has nothing — see its docstring for why.

:class:`FakeEmbedder` is what ``GREPOGRAM_FAKE_MODELS=1`` selects for tests and CI: a hashed
bag of stems with a tiny built-in Russian/English lexicon, so texts that share words (or a
lexicon pair such as ``ВНЖ`` / ``residence permit``) land close and everything else does not,
deterministically and without a model download.
"""

import hashlib
import logging
import math
import threading
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from grepogram.models import Config, ModelsCfg
from grepogram.paths import env_flag
from grepogram.stem import stem_token, tokenize

log = logging.getLogger(__name__)

FAKE_MODELS_ENV = "GREPOGRAM_FAKE_MODELS"
HF_OFFLINE_ENV = "HF_HUB_OFFLINE"
NOT_CACHED_ERRORS = frozenset({"LocalEntryNotFoundError", "OfflineModeIsEnabled"})
FAKE_NAME = "fake"
FAKE_DIM = 256
DEFAULT_MAX_SEQ_LENGTH = ModelsCfg().max_seq_length
"""The shipped ``models.max_seq_length``; the one literal lives on :class:`ModelsCfg`."""
BATCH_SIZE = 32
AUTO_DEVICE = "auto"
FAKE_LEXICON: Mapping[str, str] = {
    "внж": "residence",
    "residence": "residence",
    "permit": "residence",
    "residencia": "residence",
    "банк": "bank",
    "bank": "bank",
    "счёт": "account",
    "account": "account",
    "виза": "visa",
    "visa": "visa",
    "симка": "sim",
    "сим": "sim",
    "sim": "sim",
}

_cpu_warned = False


class ModelUnavailable(Exception):
    """A local model cannot be imported or loaded; dense search degrades to lexical."""


@runtime_checkable
class Embedder(Protocol):
    """What the dense index and hybrid search need from an embedding model."""

    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FakeEmbedder:
    """Deterministic hashed bag-of-stems vectors for tests and CI.

    Every token of a text is stemmed like the lexical index stems it, mapped through the
    lexicon (so ``ВНЖ`` and ``residence permit`` become one feature), and hashed to a bucket
    and a sign; the vector is the signed feature counts, L2-normalized. Pure digits and
    one-letter tokens are skipped because every unit line starts with a ``[YYYY-MM-DD HH:MM]``
    stamp that would otherwise dominate the vector. An empty text is the zero vector.
    """

    name = FAKE_NAME

    def __init__(self, dim: int = FAKE_DIM, lexicon: Mapping[str, str] | None = None) -> None:
        if dim < 1:
            raise ValueError(f"embedding dimension must be positive, got {dim}")
        self.dim = dim
        source = FAKE_LEXICON if lexicon is None else lexicon
        self.lexicon = {stem_token(key.lower()): value for key, value in source.items()}

    def features(self, text: str) -> list[str]:
        """The hashed features of ``text``: stems mapped through the lexicon, stamps dropped."""
        out: list[str] = []
        for token in tokenize(text):
            if token.isdigit() or len(token) < 2:
                continue
            stem = stem_token(token)
            out.append(self.lexicon.get(stem, stem))
        return out

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for feature in self.features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            vector[int.from_bytes(digest[:4], "big") % self.dim] += 1.0 if digest[4] & 1 else -1.0
        return normalize(vector)


class BgeM3Embedder:
    """``BAAI/bge-m3`` (or any sentence-transformers model) on ``mps`` or the CPU.

    The model is loaded eagerly so that a missing extra, a failed download or a broken cache
    surfaces as :class:`ModelUnavailable` right here, where the caller can degrade. On ``mps``
    the weights are halved to fp16; the text is truncated at ``max_seq_length`` tokens
    (``models.max_seq_length``, 512 by default), so a unit longer than that is embedded only up
    to it while the FTS tables still hold it whole. ``units.window_max_chars`` is a ceiling on a
    window's text, so the shipped 1500 characters fit inside the shipped 512 tokens; raise one
    and the other needs raising with it (see the README). Encoding is serialized with a lock
    because the MCP server may call it from several threads.
    """

    def __init__(
        self,
        model_id: str,
        device: str = AUTO_DEVICE,
        max_seq_length: int = DEFAULT_MAX_SEQ_LENGTH,
    ) -> None:
        if max_seq_length < 1:
            raise ValueError(f"max_seq_length must be positive, got {max_seq_length}")
        self.name = model_id
        self.max_seq_length = max_seq_length
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ModelUnavailable(
                f"sentence-transformers is not installed ({exc}); install the dense extra"
            ) from exc
        self.device = resolve_device(device)
        try:
            model = load_cached_first(
                lambda local: SentenceTransformer(
                    model_id, device=self.device, local_files_only=local
                ),
                f"embedding model {model_id!r}",
            )
            if self.device == "mps":
                model.half()
        except Exception as exc:
            raise ModelUnavailable(f"cannot load embedding model {model_id!r}: {exc}") from exc
        model.max_seq_length = max_seq_length
        dim = embedding_dimension(model)
        if not dim:
            raise ModelUnavailable(f"embedding model {model_id!r} reports no embedding dimension")
        self.dim = dim
        self._model = model
        self._lock = threading.Lock()
        log.info(
            "embedding model %s loaded on %s (dim %d, %d tokens)",
            model_id,
            self.device,
            dim,
            max_seq_length,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            raw: Any = self._model.encode(
                texts,
                batch_size=BATCH_SIZE,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        return as_rows(raw)

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


def load_embedder(cfg: Config) -> Embedder:
    """The configured embedder, or the fake when ``GREPOGRAM_FAKE_MODELS`` is set.

    Raises :class:`ModelUnavailable` when the real model cannot be imported or loaded.
    """
    if fake_models_enabled():
        return FakeEmbedder()
    return BgeM3Embedder(cfg.models.embed, cfg.models.device, cfg.models.max_seq_length)


def fake_models_enabled() -> bool:
    """True when ``GREPOGRAM_FAKE_MODELS`` is ``1``, ``true``, ``yes`` or ``on``."""
    return env_flag(FAKE_MODELS_ENV)


def resolve_device(device: str) -> str:
    """Turn the configured device into a torch device name.

    ``auto`` picks ``mps`` when Metal is available and falls back to the CPU with a warning
    logged once per process; any other value is passed through for torch to validate.
    """
    global _cpu_warned
    if device != AUTO_DEVICE:
        return device
    try:
        import torch
    except ImportError as exc:
        raise ModelUnavailable(f"torch is not installed ({exc}); install the dense extra") from exc
    if torch.backends.mps.is_available():
        return "mps"
    if not _cpu_warned:
        _cpu_warned = True
        log.warning("MPS is not available; models run on the CPU, which is much slower")
    return "cpu"


def load_cached_first[T](load: Callable[[bool], T], what: str) -> T:
    """``load(local_files_only)``, from the local cache first and from the hub only if it must.

    sentence-transformers asks huggingface.co whether each file of the model is still current
    even when the cache already holds every one of them, and that round trip is paid by every
    process that loads a model — every ``search``, every ``sync``. It costs seconds on a good
    connection and minutes where outbound connections are held open rather than refused (a
    firewall prompt nobody answers, a captive portal). So the load runs with
    ``local_files_only=True`` first, and only a failure that says the files are *not cached*
    (:func:`not_cached`) is retried with the network — the first run and nothing else. That
    retry is logged at INFO, so a first download reads as a download rather than a hang.

    ``HF_HUB_OFFLINE`` set is the user saying no network at all: the first failure is then the
    answer, and the caller turns it into :class:`ModelUnavailable` as before.
    """
    try:
        return load(True)
    except Exception as exc:
        if env_flag(HF_OFFLINE_ENV) or not not_cached(exc):
            raise
    log.info("%s is not in the Hugging Face cache yet; downloading it", what)
    return load(False)


def not_cached(exc: BaseException) -> bool:
    """Whether a ``local_files_only`` load failed only because the model is not in the cache.

    huggingface_hub raises ``LocalEntryNotFoundError`` for that, but transformers re-raises it
    as a plain ``OSError`` whose message talks about the connection, so what identifies the
    cause is the chain of ``__cause__`` / ``__context__`` below it — walked defensively, since
    a chain may loop. The match is on the class name because huggingface_hub belongs to the
    ``dense`` extra alone, and this module imports nothing that the extra brings.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in NOT_CACHED_ERRORS:
            return True
        current = current.__cause__ or current.__context__
    return False


def normalize(vector: list[float]) -> list[float]:
    """L2-normalize ``vector``; the zero vector stays zero."""
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]


def embedding_dimension(model: Any) -> int | None:
    """The model's output width; sentence-transformers 6 renamed the getter, older ones lack it."""
    getter = getattr(model, "get_embedding_dimension", None)
    if getter is None:
        getter = model.get_sentence_embedding_dimension
    dim = getter()
    return int(dim) if dim else None


def as_rows(raw: Any) -> list[list[float]]:
    """Plain Python rows from whatever ``encode`` returned (an ndarray, a tensor or lists)."""
    rows = raw.tolist() if hasattr(raw, "tolist") else raw
    return [[float(value) for value in row] for row in rows]
