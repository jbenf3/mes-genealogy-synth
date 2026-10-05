"""Run the reference queries on a generated scenario and write the answers as CSV.

    uv run python queries/run.py official_1x out/official_1x out/results/official_1x

The query parameters come from expected/<scenario>/parameters.sql. The output folder
also gets content_sha256.txt, the fingerprint of the data. Compare the written files
with the ones in expected/<scenario>/.
"""

import json
import pathlib
import sys

import duckdb

QUERIES = pathlib.Path(__file__).resolve().parent
EXPECTED = QUERIES.parent / "expected"


def _literal(value: object) -> str:
    """Quote a value as a SQL string literal.

    Args:
        value: Value to quote.

    Returns:
        Quoted string.
    """
    return "'" + str(value).replace("'", "''") + "'"


def run(
    scenario: str, data: str | pathlib.Path, results: str | pathlib.Path
) -> list[pathlib.Path]:
    """Run every reference query and write the answers.

    Args:
        scenario: Scenario name. The query parameters come from
            expected/<scenario>/parameters.sql.
        data: Output directory of `mesgen generate`.
        results: Directory for the CSV files and content_sha256.txt.

    Returns:
        Paths of the written files.
    """
    results = pathlib.Path(results)
    results.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        f"SET VARIABLE data = {_literal(pathlib.Path(data).resolve().as_posix())}"
    )
    con.execute((QUERIES / "events.sql").read_text())
    con.execute((EXPECTED / scenario / "parameters.sql").read_text())
    written = []
    for sql in sorted(QUERIES.glob("q*.sql")):
        path = results / f"{sql.stem}.csv"
        con.execute(
            f"COPY ({sql.read_text().strip().rstrip(';')}) TO {_literal(path)} (HEADER)"
        )
        written.append(path)
    path = results / "content_sha256.txt"
    path.write_text(
        json.loads((pathlib.Path(data) / "manifest.json").read_text())["content_sha256"]
        + "\n"
    )
    return written + [path]


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    for path in run(*sys.argv[1:]):
        print(path)
