"""Profile loading. A profile is the process template: materials, segments, routes, merges, splits, rates."""

import datetime as dt
import hashlib
import importlib.resources as res
import json
import pathlib
from typing import Any

REQUIRED_TOP = ("schema_version", "segments", "materials", "template")
REQUIRED_TEMPLATE = (
    "epoch",
    "sources",
    "routes",
    "merges",
    "splits",
    "consumables",
    "name_families",
    "scrap",
)


def bundled_profile_path(name: str) -> pathlib.Path:
    """Return the path of a profile shipped with the package.

    Args:
        name: Profile name, such as "default".

    Returns:
        Path to the JSON file. The file may not exist.
    """
    return pathlib.Path(
        str(res.files("mesgen").joinpath("profiles").joinpath(f"{name}.json"))
    )


def load_profile(ref: str | pathlib.Path) -> dict[str, Any]:
    """Load and check a profile.

    Args:
        ref: Bundled profile name, such as "default", or path to a JSON file.

    Returns:
        The profile, with its hash under "_hash" and its file path under "_path".

    Raises:
        FileNotFoundError: If `ref` has a file extension but the file doesn't exist.
        ValueError: If there is no bundled profile with that name, or the profile is
            invalid.
    """
    path = pathlib.Path(ref)
    if not path.is_file():
        if path.suffix:
            raise FileNotFoundError(f"profile file {ref} not found")
        path = bundled_profile_path(str(ref))
        if not path.exists():  # source checkout without installed resources
            path = (
                pathlib.Path(__file__).resolve().parents[2] / "profiles" / f"{ref}.json"
            )
        if not path.is_file():
            raise ValueError(f"no bundled profile named {ref}")
    text = path.read_text()
    profile = json.loads(text)
    validate_profile(profile)
    profile["_hash"] = hashlib.sha256(text.encode()).hexdigest()
    profile["_path"] = str(path)
    return profile


def validate_profile(p: dict[str, Any]) -> None:
    """Check that a profile has the required keys and that its ids match up.

    Args:
        p: Profile.

    Raises:
        ValueError: If a key is missing, an id is unknown or the epoch has no time
            zone.
    """
    missing = [k for k in REQUIRED_TOP if k not in p]
    if missing:
        raise ValueError(f"profile missing keys: {missing}")
    if p["schema_version"] != 1:
        raise ValueError(f"unsupported profile schema_version {p['schema_version']}")
    t = p["template"]
    missing = [k for k in REQUIRED_TEMPLATE if k not in t]
    if missing:
        raise ValueError(f"template missing keys: {missing}")
    epoch = dt.datetime.fromisoformat(t["epoch"].replace("Z", "+00:00"))
    if epoch.tzinfo is None:
        raise ValueError(
            f"epoch {t['epoch']} has no time zone: add one, such as Z, so every machine reads it the same"
        )
    seg_ids = {s["id"] for s in p["segments"]}
    mat_ids = {m["id"] for m in p["materials"]}
    regime = {m["id"]: m["regime"] for m in p["materials"]}
    for mat, steps in t["routes"].items():
        if mat not in mat_ids:
            raise ValueError(f"route for unknown material {mat}")
        for st in steps:
            if st["seg"] not in seg_ids:
                raise ValueError(f"route {mat} references unknown segment {st['seg']}")
            if st.get("to") and st["to"] not in mat_ids:
                raise ValueError(
                    f"route {mat} step {st['seg']} targets unknown material {st['to']}"
                )
            if (
                st.get("to")
                and regime[mat] == "mass"
                and regime[st["to"]] == "roll"
                and not st.get("length_per_mass")
            ):
                raise ValueError(
                    f"route {mat} step {st['seg']} makes a roll from mass: set length_per_mass"
                )
    for seg, m in t["merges"].items():
        if (
            seg not in seg_ids
            or m["output"] not in mat_ids
            or any(k not in mat_ids for k in m["inputs"])
        ):
            raise ValueError(f"merge {seg} references unknown ids")
    for seg, s in t["splits"].items():
        if (
            seg not in seg_ids
            or s["input"] not in mat_ids
            or s["output"] not in mat_ids
        ):
            raise ValueError(f"split {seg} references unknown ids")
    for src in t["sources"]:
        if src["material"] not in mat_ids or src["segment"] not in seg_ids:
            raise ValueError(f"source {src} references unknown ids")
    sc = t["scrap"]
    if any(
        sid not in seg_ids
        for sid in [
            sc["segment"],
            sc["disposal_segment"],
            sc["recover_segment"],
            *sc["store_segments"],
        ]
    ):
        raise ValueError("scrap block references unknown segments")
