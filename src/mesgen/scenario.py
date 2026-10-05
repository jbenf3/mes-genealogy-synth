"""Scenario loading with defaults. A scenario is a small YAML file; (profile, scenario, seed) determine every output byte."""

import hashlib
import importlib.resources as res
import pathlib
from typing import Any

import yaml

DEFAULTS = {
    "name": "custom",
    "seed": 1,
    "profile": "default",
    "days": 30,
    "base_days": 20,
    "volume": {"scale": 1.0, "ramp_last_over_first": 1.9},
    "churn_multiplier": 1.0,
    "rework_rate": 0.01,
    "duplicates": {"enabled": True},
    "anomalies": {"enabled": True},
    "cohorts": [],  # planted causes: {segment, day, effect, background}; the cause is one equipment of that segment
}


def _merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """Merge settings into defaults, recursing into nested dicts.

    Args:
        base: Default settings.
        override: Settings that replace the defaults.

    Returns:
        Merged settings. Neither input is changed.
    """
    out = dict(base)
    for k, v in (override or {}).items():
        out[k] = (
            _merge(base[k], v)
            if isinstance(v, dict) and isinstance(base.get(k), dict)
            else v
        )
    return out


def bundled_scenario_path(name: str) -> pathlib.Path:
    """Return the path of a scenario shipped with the package.

    Falls back to the scenarios/ folder of a source checkout.

    Args:
        name: Scenario name, such as "smoke".

    Returns:
        Path to the YAML file. The file may not exist.
    """
    p = pathlib.Path(
        str(res.files("mesgen").joinpath("scenarios").joinpath(f"{name}.yaml"))
    )
    if not p.exists():
        p = pathlib.Path(__file__).resolve().parents[2] / "scenarios" / f"{name}.yaml"
    return p


def load_scenario(ref: str | pathlib.Path, seed: int | None = None) -> dict[str, Any]:
    """Load a scenario and fill in the defaults.

    Args:
        ref: Bundled scenario name, such as "smoke", or path to a YAML file.
        seed: Seed to use instead of the scenario's.

    Returns:
        The scenario, with its hash under "_hash" and its file path under "_path".

    Raises:
        FileNotFoundError: If `ref` has a file extension but the file doesn't exist.
        ValueError: If there is no bundled scenario with that name, or base_days is
            larger than days.
    """
    path = pathlib.Path(ref)
    if not path.is_file():
        if path.suffix:
            raise FileNotFoundError(f"scenario file {ref} not found")
        path = bundled_scenario_path(str(ref))
        if not path.is_file():
            raise ValueError(f"no bundled scenario named {ref}")
    text = path.read_text()
    sc = _merge(DEFAULTS, yaml.safe_load(text) or {})
    if seed is not None:
        sc["seed"] = int(seed)
    if sc["base_days"] > sc["days"]:
        raise ValueError("base_days must not exceed days")
    sc["_hash"] = hashlib.sha256(text.encode()).hexdigest()
    sc["_path"] = str(path)
    return sc
