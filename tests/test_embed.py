import logging
import math
import sys
import threading
import types
from collections.abc import Callable
from typing import Any

import pytest

from grepogram import embed
from grepogram.embed import (
    BATCH_SIZE,
    FAKE_DIM,
    MAX_SEQ_LENGTH,
    BgeM3Embedder,
    Embedder,
    FakeEmbedder,
    ModelUnavailable,
    as_rows,
    fake_models_enabled,
    load_embedder,
    normalize,
    resolve_device,
)
from grepogram.models import Config, ModelsCfg

RU_BANK = "Открыл счёт в банке Galicia без DNI, только с паспортом"
RU_BANK_INFLECTED = "открываю счета в банках без dni по паспорту"
EN_BANK = "Opening bank accounts without a DNI, passport only"
WEATHER = "Завтра обещают дождь, возьми зонт"


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def length(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


# --- FakeEmbedder ----------------------------------------------------------------------------


def test_fake_satisfies_protocol() -> None:
    fake = FakeEmbedder()
    assert isinstance(fake, Embedder)
    assert fake.name == "fake"
    assert fake.dim == FAKE_DIM == 256


@pytest.mark.parametrize("dim", [0, -1])
def test_fake_rejects_non_positive_dim(dim: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        FakeEmbedder(dim=dim)


def test_fake_is_deterministic_across_instances() -> None:
    texts = [RU_BANK, EN_BANK, WEATHER, ""]
    first = FakeEmbedder().embed(texts)
    second = FakeEmbedder().embed(texts)
    assert first == second
    assert FakeEmbedder().embed(texts) == first


def test_fake_query_matches_batch_embedding() -> None:
    fake = FakeEmbedder()
    assert fake.embed_query(RU_BANK) == fake.embed([RU_BANK])[0]


def test_fake_vectors_have_the_declared_shape() -> None:
    fake = FakeEmbedder(dim=16)
    vectors = fake.embed([RU_BANK, EN_BANK])
    assert len(vectors) == 2
    assert all(len(vector) == 16 for vector in vectors)
    assert all(isinstance(value, float) for vector in vectors for value in vector)
    assert fake.embed([]) == []


def test_fake_vectors_are_unit_length() -> None:
    for vector in FakeEmbedder().embed([RU_BANK, EN_BANK, WEATHER, "DNI"]):
        assert length(vector) == pytest.approx(1.0)


@pytest.mark.parametrize("text", ["", "   ", "🙂🙂 !!!", "2024 03 15 10 30", "a в i"])
def test_fake_texts_without_features_are_the_zero_vector(text: str) -> None:
    fake = FakeEmbedder(dim=4)
    assert fake.features(text) == []
    assert fake.embed_query(text) == [0.0, 0.0, 0.0, 0.0]


def test_fake_features_are_stems_through_the_lexicon_without_stamps() -> None:
    line = "[2024-03-15 10:30] Alice: Открыл счёт в банке Galicia без DNI"
    assert FakeEmbedder().features(line) == [
        "alic",
        "откр",
        "account",
        "bank",
        "galicia",
        "без",
        "dni",
    ]


def test_fake_inflections_land_close_and_unrelated_text_does_not() -> None:
    fake = FakeEmbedder()
    ru, inflected, weather = fake.embed([RU_BANK, RU_BANK_INFLECTED, WEATHER])
    assert cosine(ru, inflected) > 0.6
    assert cosine(ru, inflected) > cosine(ru, weather) + 0.4
    assert abs(cosine(ru, weather)) < 0.3


def test_fake_shared_terms_across_languages_score_higher_than_unrelated() -> None:
    fake = FakeEmbedder()
    ru, en, weather = fake.embed([RU_BANK, EN_BANK, WEATHER])
    assert cosine(ru, en) > cosine(ru, weather)
    assert cosine(en, ru) > cosine(en, weather)


def test_fake_lexicon_bridges_the_paraphrase_pair() -> None:
    fake = FakeEmbedder()
    assert cosine(fake.embed_query("ВНЖ"), fake.embed_query("residence permit")) == pytest.approx(
        1.0
    )
    assert cosine(fake.embed_query("нужен ВНЖ"), fake.embed_query("a residence permit")) > 0.4
    assert cosine(fake.embed_query("ВНЖ"), fake.embed_query(WEATHER)) < 0.3


def test_fake_custom_lexicon_replaces_the_default() -> None:
    fake = FakeEmbedder(lexicon={"Кот": "cat"})
    assert cosine(fake.embed_query("кот"), fake.embed_query("cats")) == pytest.approx(1.0)
    assert cosine(fake.embed_query("ВНЖ"), fake.embed_query("residence permit")) == pytest.approx(
        0.0
    )


def test_fake_small_dim_still_normalizes() -> None:
    fake = FakeEmbedder(dim=1)
    assert fake.embed_query("DNI") in ([1.0], [-1.0])
    assert fake.embed_query("DNI DNI") == fake.embed_query("DNI")


def test_fake_is_usable_from_a_worker_thread() -> None:
    fake = FakeEmbedder()
    results: list[list[float]] = []
    worker = threading.Thread(target=lambda: results.append(fake.embed_query(RU_BANK)))
    worker.start()
    worker.join()
    assert results == [fake.embed_query(RU_BANK)]


# --- helpers ---------------------------------------------------------------------------------


def test_normalize() -> None:
    assert normalize([3.0, 4.0]) == [0.6, 0.8]
    assert normalize([0.0, 0.0]) == [0.0, 0.0]
    assert normalize([]) == []


class _ArrayLike:
    def __init__(self, rows: list[list[Any]]) -> None:
        self.rows = rows

    def tolist(self) -> list[list[Any]]:
        return self.rows


def test_as_rows_accepts_arrays_and_lists() -> None:
    assert as_rows(_ArrayLike([[1, 2], [3, 4]])) == [[1.0, 2.0], [3.0, 4.0]]
    assert as_rows([[0.5, 1]]) == [[0.5, 1.0]]
    assert as_rows([]) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        (" on ", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("no", False),
    ],
)
def test_fake_models_enabled(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("GREPOGRAM_FAKE_MODELS", value)
    assert fake_models_enabled() is expected


def test_fake_models_disabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GREPOGRAM_FAKE_MODELS", raising=False)
    assert fake_models_enabled() is False


# --- BgeM3Embedder through stub modules --------------------------------------------------------


class StubModel:
    """Stands in for ``sentence_transformers.SentenceTransformer``."""

    instances: list["StubModel"] = []
    dim: int | None = 4

    def __init__(self, model_id: str, device: str | None = None, **kwargs: Any) -> None:
        self.model_id = model_id
        self.device = device
        self.kwargs = kwargs
        self.max_seq_length = 8192
        self.halved = False
        self.calls: list[dict[str, Any]] = []
        StubModel.instances.append(self)

    def half(self) -> "StubModel":
        self.halved = True
        return self

    def get_embedding_dimension(self) -> int | None:
        return self.dim

    def get_sentence_embedding_dimension(self) -> int | None:
        return self.dim

    def encode(self, texts: list[str], **kwargs: Any) -> _ArrayLike:
        self.calls.append(kwargs)
        return _ArrayLike([[len(text), 1, 0, 0] for text in texts])


class OfflineModel(StubModel):
    def __init__(self, model_id: str, device: str | None = None, **kwargs: Any) -> None:
        raise OSError("offline: cannot reach huggingface.co")


class DimensionlessModel(StubModel):
    dim = None


class LegacyModel(StubModel):
    """A sentence-transformers release before ``get_embedding_dimension`` existed."""

    get_embedding_dimension = None  # type: ignore[assignment]


Install = Callable[..., None]


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> Install:
    """Inject stub ``torch`` / ``sentence_transformers`` modules; the real ones are never used."""

    def install(
        *,
        mps: bool = True,
        model: type[StubModel] | None = StubModel,
        torch_missing: bool = False,
    ) -> None:
        torch_mod: Any = types.ModuleType("torch")
        torch_mod.backends = types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: mps)
        )
        monkeypatch.setitem(sys.modules, "torch", None if torch_missing else torch_mod)
        st_mod: Any = types.ModuleType("sentence_transformers")
        st_mod.SentenceTransformer = model
        monkeypatch.setitem(sys.modules, "sentence_transformers", None if model is None else st_mod)
        monkeypatch.setattr(embed, "_cpu_warned", False)
        monkeypatch.delenv("GREPOGRAM_FAKE_MODELS", raising=False)
        StubModel.instances.clear()

    return install


@pytest.mark.parametrize("device", ["cpu", "mps", "cuda:1"])
def test_resolve_device_passes_explicit_devices_through(stubs: Install, device: str) -> None:
    stubs(torch_missing=True)
    assert resolve_device(device) == device


def test_resolve_device_auto_prefers_mps(stubs: Install, caplog: pytest.LogCaptureFixture) -> None:
    stubs(mps=True)
    with caplog.at_level(logging.WARNING, logger="grepogram.embed"):
        assert resolve_device("auto") == "mps"
    assert caplog.records == []


def test_resolve_device_auto_falls_back_to_cpu_with_one_warning(
    stubs: Install, caplog: pytest.LogCaptureFixture
) -> None:
    stubs(mps=False)
    with caplog.at_level(logging.WARNING, logger="grepogram.embed"):
        assert resolve_device("auto") == "cpu"
        assert resolve_device("auto") == "cpu"
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "MPS" in warnings[0].getMessage()


def test_resolve_device_auto_without_torch(stubs: Install) -> None:
    stubs(torch_missing=True)
    with pytest.raises(ModelUnavailable, match="torch"):
        resolve_device("auto")


def test_bge_loads_on_mps_in_fp16(stubs: Install) -> None:
    stubs(mps=True)
    embedder = BgeM3Embedder("BAAI/bge-m3", "auto")
    (model,) = StubModel.instances
    assert isinstance(embedder, Embedder)
    assert embedder.name == "BAAI/bge-m3"
    assert embedder.dim == 4
    assert embedder.device == "mps"
    assert model.model_id == "BAAI/bge-m3"
    assert model.device == "mps"
    assert model.halved is True
    assert model.max_seq_length == MAX_SEQ_LENGTH == 512


def test_bge_on_cpu_keeps_fp32(stubs: Install) -> None:
    stubs(mps=False)
    embedder = BgeM3Embedder("BAAI/bge-m3", "cpu")
    (model,) = StubModel.instances
    assert embedder.device == "cpu"
    assert model.device == "cpu"
    assert model.halved is False


def test_bge_embed_normalizes_in_batches_without_progress_bar(stubs: Install) -> None:
    stubs()
    embedder = BgeM3Embedder("BAAI/bge-m3")
    vectors = embedder.embed(["a", "bbb"])
    assert vectors == [[1.0, 1.0, 0.0, 0.0], [3.0, 1.0, 0.0, 0.0]]
    assert all(isinstance(value, float) for vector in vectors for value in vector)
    (model,) = StubModel.instances
    assert model.calls == [
        {
            "batch_size": BATCH_SIZE,
            "normalize_embeddings": True,
            "convert_to_numpy": True,
            "show_progress_bar": False,
        }
    ]
    assert BATCH_SIZE == 32


def test_bge_embed_empty_list_skips_the_model(stubs: Install) -> None:
    stubs()
    embedder = BgeM3Embedder("BAAI/bge-m3")
    assert embedder.embed([]) == []
    assert StubModel.instances[0].calls == []


def test_bge_embed_query_is_the_single_row(stubs: Install) -> None:
    stubs()
    embedder = BgeM3Embedder("BAAI/bge-m3")
    assert embedder.embed_query("hello") == [5.0, 1.0, 0.0, 0.0]


def test_bge_embed_from_a_worker_thread(stubs: Install) -> None:
    stubs()
    embedder = BgeM3Embedder("BAAI/bge-m3")
    results: list[list[float]] = []
    worker = threading.Thread(target=lambda: results.append(embedder.embed_query("xy")))
    worker.start()
    worker.join()
    assert results == [[2.0, 1.0, 0.0, 0.0]]


def test_bge_without_sentence_transformers(stubs: Install) -> None:
    stubs(model=None)
    with pytest.raises(ModelUnavailable, match="sentence-transformers is not installed"):
        BgeM3Embedder("BAAI/bge-m3")


def test_bge_download_failure_becomes_model_unavailable(stubs: Install) -> None:
    stubs(model=OfflineModel)
    with pytest.raises(ModelUnavailable, match="'BAAI/bge-m3'.*offline") as info:
        BgeM3Embedder("BAAI/bge-m3")
    assert isinstance(info.value.__cause__, OSError)


def test_bge_reads_the_dimension_from_older_sentence_transformers(stubs: Install) -> None:
    stubs(model=LegacyModel)
    assert BgeM3Embedder("BAAI/bge-m3").dim == 4


def test_bge_without_a_dimension_is_unavailable(stubs: Install) -> None:
    stubs(model=DimensionlessModel)
    with pytest.raises(ModelUnavailable, match="dimension"):
        BgeM3Embedder("BAAI/bge-m3")


# --- load_embedder ---------------------------------------------------------------------------


def test_load_embedder_returns_the_fake_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GREPOGRAM_FAKE_MODELS", "1")
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    embedder = load_embedder(Config())
    assert isinstance(embedder, FakeEmbedder)
    assert embedder.dim == FAKE_DIM


def test_load_embedder_uses_the_configured_model_and_device(stubs: Install) -> None:
    stubs(mps=True)
    cfg = Config(models=ModelsCfg(embed="BAAI/bge-m3-small", device="cpu"))
    embedder = load_embedder(cfg)
    assert isinstance(embedder, BgeM3Embedder)
    assert embedder.name == "BAAI/bge-m3-small"
    assert embedder.device == "cpu"
    assert StubModel.instances[0].model_id == "BAAI/bge-m3-small"


def test_load_embedder_resolves_auto_device(stubs: Install) -> None:
    stubs(mps=True)
    embedder = load_embedder(Config())
    assert isinstance(embedder, BgeM3Embedder)
    assert embedder.device == "mps"


def test_load_embedder_raises_when_the_import_fails(stubs: Install) -> None:
    stubs(model=None)
    with pytest.raises(ModelUnavailable):
        load_embedder(Config())


def test_tmp_home_selects_the_fake(tmp_home: object) -> None:
    assert isinstance(load_embedder(Config()), FakeEmbedder)
