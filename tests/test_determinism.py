"""The same inputs give the same bytes."""

import hashlib
import math
import pathlib

import numpy as np
import pytest

from mesgen.generate import generate


def _digest(root: pathlib.Path) -> str:
    """Hash every file in a folder except manifest.json.

    Args:
        root: Folder.

    Returns:
        SHA-256 hex digest.
    """
    h = hashlib.sha256()
    for f in sorted(
        p for p in root.rglob("*") if p.is_file() and p.name != "manifest.json"
    ):
        h.update(str(f.relative_to(root)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def test_same_seed_is_byte_identical_and_other_seed_differs(
    tmp_path: pathlib.Path,
) -> None:
    """The same seed gives identical files, and another seed gives different ones."""
    a = generate("smoke", tmp_path / "a", quiet=True)
    b = generate("smoke", tmp_path / "b", quiet=True)
    c = generate("smoke", tmp_path / "c", seed=12, quiet=True)
    assert _digest(tmp_path / "a") == _digest(tmp_path / "b")
    assert a["content_sha256"] == b["content_sha256"] != c["content_sha256"]
    assert _digest(tmp_path / "a") != _digest(tmp_path / "c")


def test_last_bit_differences_in_exp_do_not_change_the_data(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The data does not depend on which exp() implementation runs."""
    # CPUs disagree on the last bit of exp() for some inputs (vectorised and scalar code paths differ); the generator
    # rounds every value derived from exp(), so the output must not depend on which exp() ran
    generate("smoke", tmp_path / "simd", quiet=True)
    scalar = np.vectorize(math.exp, otypes=[float])
    monkeypatch.setattr(
        np, "exp", lambda x: scalar(x) if np.ndim(x) else math.exp(float(x))
    )
    generate("smoke", tmp_path / "scalar", quiet=True)
    assert _digest(tmp_path / "simd") == _digest(tmp_path / "scalar")
