"""
MODEL REGISTRY
==============

Every .py file in this folder is one detection model (files starting with "_"
are skipped). The app lists them in its "Model" dropdown and the command line
takes --model <file name>.

A model file needs three names and one function:

    NAME = "Short name shown in the app"
    AUTHOR = "Who wrote it"
    DESCRIPTION = "One or two sentences on how it works"

    def detect(vessel, track, cfg) -> dict:
        ...

detect() gets one vessel's raw positions (a DataFrame with lat, lon, time,
speed_kn, course, ...) and the settings (survey_pattern_detector.Config), and
returns the same dict as survey_pattern_detector.detect_surveys(). Copy
_template.py to start.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

DEFAULT = "baseline"

# keys every detect() result must have; the map, tables and downloads rely on them
REQUIRED_KEYS = {"vessel", "order", "has_time", "positions", "lines", "blocks",
                 "predictions", "areas", "area_hulls", "geometry", "track"}

_cache: dict[str, ModuleType] = {}


def _load(key: str) -> ModuleType:
    if key not in _cache:
        mod = importlib.import_module(f"{__name__}.{key}")
        missing = [n for n in ("NAME", "detect") if not hasattr(mod, n)]
        if missing:
            raise AttributeError(f"models/{key}.py is missing {', '.join(missing)}")
        _cache[key] = mod
    return _cache[key]


def available() -> dict[str, ModuleType]:
    """Working models in this folder: {file name: module}. The baseline comes first."""
    keys = sorted(m.name for m in pkgutil.iter_modules(__path__) if not m.name.startswith("_"))
    keys.sort(key=lambda k: k != DEFAULT)
    broken = errors()
    return {k: _load(k) for k in keys if k not in broken}


def errors() -> dict[str, str]:
    """Model files that fail to import, so one broken file doesn't take down the app."""
    out = {}
    for m in pkgutil.iter_modules(__path__):
        if not m.name.startswith("_"):
            try:
                _load(m.name)
            except Exception as exc:  # noqa: BLE001
                out[m.name] = f"{type(exc).__name__}: {exc}"
    return out


def label(key: str) -> str:
    mod = _load(key)
    author = getattr(mod, "AUTHOR", "")
    return f"{mod.NAME} ({author})" if author else mod.NAME


def detect(key: str, vessel, track, cfg) -> dict:
    """Run one model on one vessel and check its result has everything the app needs."""
    result = _load(key or DEFAULT).detect(vessel, track, cfg)
    missing = REQUIRED_KEYS - set(result)
    if missing:
        raise ValueError(f"models/{key}.py: detect() result is missing {sorted(missing)}")
    return result
