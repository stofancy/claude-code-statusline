"""Minimal localisation layer for renderer labels.

Resolution order for the active language:
    1. ``CCS_LANG`` env var (e.g. ``zh``, ``en``)
    2. ``LC_ALL`` / ``LC_MESSAGES`` / ``LANG`` (POSIX locale → first segment)
    3. fallback ``en``

Locale files live in ``src/ccs/locales/<lang>.yaml`` and contain a flat
key→string map. Missing keys fall back to the key itself, so the bar still
renders intelligibly even if a translation is incomplete.
"""

import os
from pathlib import Path

import yaml

_LOCALES_DIR = Path(__file__).parent / "locales"
_DEFAULT_LANG = "en"

_cache: dict | None = None
_cache_lang: str | None = None


def _detect_lang() -> str:
    explicit = os.getenv("CCS_LANG", "").strip().lower()
    if explicit:
        return explicit.split("_")[0].split(".")[0]
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(key, "")
        if val:
            return val.split(".")[0].split("_")[0].lower()
    return _DEFAULT_LANG


def _load(lang: str) -> dict:
    candidates = [_LOCALES_DIR / f"{lang}.yaml", _LOCALES_DIR / f"{_DEFAULT_LANG}.yaml"]
    for p in candidates:
        if not p.exists():
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except OSError:
            continue
    return {}


def t(key: str) -> str:
    """Translate *key* using the active locale; falls back to the key itself."""
    global _cache, _cache_lang
    lang = _detect_lang()
    if _cache is None or _cache_lang != lang:
        _cache = _load(lang)
        _cache_lang = lang
    return _cache.get(key, key)
