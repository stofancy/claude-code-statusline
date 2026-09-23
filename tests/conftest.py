"""Shared fixtures: keep every test hermetic from the real ~/.claude state."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import ccs.catalog as catalog_mod
import ccs.cost as cost_mod


@pytest.fixture(autouse=True)
def _isolate_pricing(monkeypatch, tmp_path):
    """Point the user pricing file and remote catalog caches at an empty tmp dir,
    and never spawn a real background refresh."""
    home = tmp_path / "ccs_home"
    monkeypatch.setattr(cost_mod, "_USER_PRICING", home / "pricing.yaml")
    monkeypatch.setattr(catalog_mod, "_MODELS_PATH", home / "models_dev.json")
    monkeypatch.setattr(catalog_mod, "_FX_PATH", home / "fx_rates.json")
    monkeypatch.setattr(catalog_mod, "_LOCK_PATH", home / ".catalog_refresh.lock")
    monkeypatch.setattr(catalog_mod, "_spawn_refresh", lambda: None)
    monkeypatch.delenv("CCS_PRICING_API", raising=False)
    monkeypatch.delenv("CCS_CURRENCY", raising=False)
    cost_mod._pricing_cache = None
    cost_mod._pricing_key = None
    yield
    cost_mod._pricing_cache = None
    cost_mod._pricing_key = None
