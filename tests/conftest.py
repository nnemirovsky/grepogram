from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def fake_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test resolves models to the fakes and never opens a link — tests of the real loaders
    and of ``open`` unset these themselves — so nothing run under the test environment, a probe
    included, downloads a model or launches an application on this machine."""
    monkeypatch.setenv("GREPOGRAM_FAKE_MODELS", "1")
    monkeypatch.setenv("GREPOGRAM_NO_OPEN", "1")


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point grepogram at a throwaway home and force fake models for the test."""
    home = tmp_path / "grepogram-home"
    home.mkdir()
    monkeypatch.setenv("GREPOGRAM_HOME", str(home))
    monkeypatch.setenv("GREPOGRAM_FAKE_MODELS", "1")
    return home
