"""The shipped expected answers for smoke: regenerating smoke and running the reference SQL reproduces them exactly."""

import pathlib
import runpy

from mesgen.generate import generate

REPO = pathlib.Path(__file__).resolve().parents[1]
EXPECTED = REPO / "expected" / "smoke"


def test_reference_sql_reproduces_the_expected_answers_for_smoke(
    tmp_path: pathlib.Path,
) -> None:
    """Regenerating smoke and running the reference SQL gives the shipped answers."""
    generate("smoke", tmp_path / "data", quiet=True)
    run = runpy.run_path(str(REPO / "queries" / "run.py"))["run"]
    written = run("smoke", tmp_path / "data", tmp_path / "results")
    assert sorted(p.name for p in written) == sorted(
        p.name for p in EXPECTED.iterdir() if p.name != "parameters.sql"
    )
    for path in written:
        assert path.read_bytes() == (EXPECTED / path.name).read_bytes(), (
            f"{path.name} differs: regenerate expected/ (docs/QUERIES.md)"
        )
