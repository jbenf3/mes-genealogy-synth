"""The reference SQL on a hand-built graph with one instance of every trap (the worked example in docs/QUERIES.md).

    A1 (created) -> A1b (re-mint: same name and sequence, variant uuid) -> A2 ─┐
    B1 (legacy: consumed, never produced) ──────────────────────────────────────┴─ merge M1 -> C1 -> C2 -> disposal
    F1 (feeder) ─ dispense D1 -> P2 (P1 -> P2);  F1 -> F2 (refill on EQ_R);  F2 ─ dispense D2 -> Q2 (Q1 -> Q2)
    X1 + Y1 ─ merge M2 on EQ_W at t=10 -> Z1 -> Z2 on EQ_W at t=11
    Z2 -> Z3 and P2 -> P3 on the test step S9 (EQ_T, t=12); Z3 -> Z4 scraps Z (t=13)

Raw duplicates: a null-twin of A1b -> A2 and a conflicting copy (other operator, higher id) of B1 -> C1.
"""

import pathlib
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from mesgen.identity import lot_uuid
from mesgen.schema import EVENT_SCHEMA

QUERIES = pathlib.Path(__file__).resolve().parents[1] / "queries"
S = 1_000_000  # one second in microseconds
Lot = (
    tuple[str, int] | tuple[str, int, str]
)  # (name, sequence) or (name, sequence, variant)


def _row(
    i: int,
    in_lot: Lot | None,
    out_lot: Lot | None,
    seg: str,
    t: int,
    su: str | None = None,
    eq: str | None = None,
    loc: str = "LOC_1",
    lt: str = "LINE",
    op: str = "OP_001",
    prev: str | None = None,
    in_mat: str | None = "M",
    out_mat: str | None = "M",
    **extra: Any,
) -> dict[str, Any]:
    """Build one event row.

    Args:
        i: Row id.
        in_lot: Input lot state, or None for a creation event.
        out_lot: Output lot state, or None for a disposal event.
        seg: Segment name.
        t: Time in seconds.
        su: Segment execution id. Defaults to one per row.
        eq: Equipment path. Defaults to AR01/EQ_<segment>.
        loc: Location.
        lt: Location type.
        op: Operator.
        prev: Execution that produced the input lot.
        in_mat: Input material.
        out_mat: Output material.
        **extra: Other columns.

    Returns:
        Row with every event column.
    """
    d: dict[str, Any] = {f.name: None for f in EVENT_SCHEMA}
    d.update(
        id=i,
        event_ts=t * S,
        begin_ts=t * S,
        end_ts=t * S,
        segment_name=seg,
        segment_uuid=su or f"su{i}",
        operation_id=f"op{i}",
        location=loc,
        location_type=lt,
        equipment_path=eq or f"AR01/EQ_{seg}",
        operator=op,
        **extra,
    )
    if in_lot:
        d.update(
            in_lot_name=in_lot[0],
            in_lot_sequence=in_lot[1],
            in_lot_uuid=lot_uuid(*in_lot),
            in_material=in_mat,
            in_prev_segment=prev,
        )
    if out_lot:
        d.update(
            out_lot_name=out_lot[0],
            out_lot_sequence=out_lot[1],
            out_lot_uuid=lot_uuid(*out_lot),
            out_material=out_mat,
        )
    return d


def _table(rows: list[dict[str, Any]]) -> pa.Table:
    """Build a table with the event schema.

    Args:
        rows: Event rows.

    Returns:
        Table.
    """
    cols = {}
    for f in EVENT_SCHEMA:
        vals = [r[f.name] for r in rows]
        cols[f.name] = (
            pa.array(vals, type=pa.int64()).cast(f.type)
            if pa.types.is_timestamp(f.type)
            else pa.array(vals, type=f.type)
        )
    return pa.table(cols, schema=EVENT_SCHEMA)


A1, A1b, A2, B1, C1, C2 = (
    ("A", 1),
    ("A", 1, "b"),
    ("A", 2),
    ("B", 1),
    ("C", 1),
    ("C", 2),
)
F1, F2, P1, P2, P3, Q1, Q2 = (
    ("F", 1),
    ("F", 2),
    ("P", 1),
    ("P", 2),
    ("P", 3),
    ("Q", 1),
    ("Q", 2),
)
X1, Y1, Z1, Z2, Z3, Z4 = ("X", 1), ("Y", 1), ("Z", 1), ("Z", 2), ("Z", 3), ("Z", 4)


def _rows() -> list[dict[str, Any]]:
    """Return the rows of the worked example.

    Returns:
        Event rows.
    """
    return [
        _row(1, None, A1, "S0", 1, in_mat=None, out_mat="MA"),
        _row(2, A1, A1b, "S1", 2, prev="su1", in_mat="MA", out_mat="MA"),
        _row(3, A1b, A2, "S1", 3, prev="su2", in_mat="MA", out_mat="MA"),
        _row(
            4, A1b, A2, "S1", 3, su="su3", prev=None, in_mat="MA", out_mat="MA"
        ),  # null twin of row 3
        _row(5, A2, C1, "S2", 4, su="m1", prev="su3", in_mat="MA", out_mat="MC"),
        _row(6, B1, C1, "S2", 4, su="m1", in_mat="MB", out_mat="MC"),
        _row(
            7, B1, C1, "S2", 4, su="m1", op="OP_999", in_mat="MB", out_mat="MC"
        ),  # conflicting copy of row 6
        _row(8, C1, C2, "S3", 8, loc="LOC_9", lt="STORAGE", in_mat="MC", out_mat="MC"),
        _row(
            9, C2, None, "S6", 9, loc="LOC_D", lt="DOCK", in_mat="MC", out_mat=None
        ),  # disposal
        _row(10, None, F1, "S0", 1, in_mat=None, out_mat="MF"),
        _row(11, None, P1, "S0", 1, in_mat=None, out_mat="MP"),
        _row(12, None, Q1, "S0", 1, in_mat=None, out_mat="MP"),
        _row(13, P1, P2, "S4", 5, su="d1", in_mat="MP", out_mat="MP"),
        _row(14, F1, P2, "S4", 5, su="d1", in_mat="MF", out_mat="MP"),
        _row(
            15, F1, F2, "S5", 6, eq="EQ_R", in_mat="MF", out_mat="MF"
        ),  # feeder refill
        _row(16, Q1, Q2, "S4", 7, su="d2", in_mat="MP", out_mat="MP"),
        _row(17, F2, Q2, "S4", 7, su="d2", in_mat="MF", out_mat="MP"),
        _row(18, None, X1, "S0", 1, in_mat=None, out_mat="MX"),
        _row(19, None, Y1, "S0", 1, in_mat=None, out_mat="MX"),
        _row(20, X1, Z1, "S7", 10, su="m2", eq="EQ_W", in_mat="MX", out_mat="MZ"),
        _row(21, Y1, Z1, "S7", 10, su="m2", eq="EQ_W", in_mat="MX", out_mat="MZ"),
        _row(22, Z1, Z2, "S8", 11, eq="EQ_W", in_mat="MZ", out_mat="MZ"),
        _row(23, Z2, Z3, "S9", 12, eq="EQ_T", in_mat="MZ", out_mat="MZ"),  # test step
        _row(24, P2, P3, "S9", 12, eq="EQ_T", in_mat="MP", out_mat="MP"),
        _row(
            25, Z3, Z4, "S10", 13, in_mat="MZ", out_mat="MZ", out_lot_status="ST_2"
        ),  # Z is scrapped
    ]


@pytest.fixture(scope="module")
def con(tmp_path_factory: pytest.TempPathFactory) -> duckdb.DuckDBPyConnection:
    """Write the example to Parquet and open DuckDB on it.

    Rows before t=8 go to a base file and the rest to a stream file.

    Args:
        tmp_path_factory: Pytest temporary directory factory.

    Returns:
        Connection with the events view.
    """
    out = tmp_path_factory.mktemp("example")
    t = _table(_rows())
    early = pc.less(pc.cast(t["begin_ts"], pa.int64()), 8 * S)
    for path, part in (
        (out / "base" / "day=0000.parquet", t.filter(early)),
        (out / "stream" / "day=0000" / "hour=00.parquet", t.filter(pc.invert(early))),
    ):
        path.parent.mkdir(parents=True)
        pq.write_table(part, path)
    con = duckdb.connect()
    con.execute(f"SET VARIABLE data = '{out.as_posix()}'")
    con.execute((QUERIES / "events.sql").read_text())
    return con


def ask(
    con: duckdb.DuckDBPyConnection, query: str, **params: str | int
) -> list[tuple[Any, ...]]:
    """Run a reference query.

    Args:
        con: Connection with the events view.
        query: Name of the SQL file in queries/, without ".sql".
        **params: Query parameters. Integers are times in microseconds.

    Returns:
        Result rows.
    """
    for k, v in params.items():
        con.execute(
            f"SET VARIABLE {k} = "
            + (f"make_timestamp({v})" if isinstance(v, int) else f"'{v}'")
        )
    return con.execute((QUERIES / f"{query}.sql").read_text()).fetchall()


def test_q1_ancestry_counts_the_remint_and_the_legacy_input(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Q1 reaches the re-minted state and the legacy lot."""
    assert ask(con, "q1_ancestry", q1_lot_uuid=lot_uuid(*C2)) == sorted(
        (lot_uuid(*x),) for x in (C1, A2, B1, A1b, A1)
    )
    assert ask(con, "q1_ancestry", q1_lot_uuid=lot_uuid(*A1)) == []


def test_q2_descendants_stop_at_the_disposal(con: duckdb.DuckDBPyConnection) -> None:
    """Q2 ends at the last state before the disposal."""
    assert ask(con, "q2_descendants", q2_lot_uuid=lot_uuid(*C1)) == [(lot_uuid(*C2),)]


def test_q3_counts_equipment_in_the_histories_of_failing_and_passing_units(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Q3 counts each equipment once per member, merges and dispenses included."""
    # Z3 failed (its next event scraps Z); P3 passed. Z1 has two producing rows (a merge), P2 two (a dispense).
    assert ask(
        con, "q3_commonality", q3_segment="S9", q3_from=12 * S, q3_to=13 * S
    ) == [("AR01/EQ_S0", 1, 1), ("AR01/EQ_S4", 0, 1), ("EQ_T", 1, 1), ("EQ_W", 1, 0)]


def test_q4_bom_and_q5_where_used_by_name(con: duckdb.DuckDBPyConnection) -> None:
    """Q4 and Q5 start from every state of the name and report names."""
    assert ask(con, "q4_bom", q4_lot_name="C") == [("A",), ("B",)]
    assert ask(con, "q4_bom", q4_lot_name="Q") == [("F",)]
    assert ask(con, "q5_where_used", q5_lot_name="A") == [("C",)]
    assert ask(con, "q5_where_used", q5_lot_name="F") == [
        ("P",),
        ("Q",),
    ]  # every state of F


def test_q6_window_includes_its_start_and_excludes_its_end(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Q6 windows include their start and exclude their end."""
    assert ask(
        con, "q6_processed_on", q6_equipment="EQ_W", q6_from=10 * S, q6_to=11 * S
    ) == [("X",), ("Y",), ("Z",)]
    assert (
        ask(con, "q6_processed_on", q6_equipment="EQ_T", q6_from=11 * S, q6_to=12 * S)
        == []
    )
    assert ask(
        con, "q6_processed_on", q6_equipment="EQ_T", q6_from=12 * S, q6_to=13 * S
    ) == [("P",), ("Z",)]
    assert ask(
        con, "q6_processed_on", q6_equipment="AR01/EQ_S6", q6_from=9 * S, q6_to=10 * S
    ) == [("C",)]  # a disposal


def test_q7_excursion_follows_states_not_names(con: duckdb.DuckDBPyConnection) -> None:
    """Q7 follows lot states, so parts dispensed before a refill are not affected."""
    # the refill produced F2; the part dispensed from F1 (P) is not downstream of it
    assert ask(
        con, "q7_excursion_impact", q7_equipment="EQ_R", q7_from=6 * S, q7_to=6 * S + 1
    ) == [("F",), ("Q",)]
    assert ask(
        con, "q7_excursion_impact", q7_equipment="EQ_W", q7_from=10 * S, q7_to=11 * S
    ) == [("Z",)]


def test_q8_history_lists_both_uuids_of_a_reminted_sequence_and_nulls_for_a_legacy_lot(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """Q8 lists both uuids of a re-minted sequence, and nulls for a legacy state."""
    hist = ask(con, "q8_device_history", q8_lot_name="A")
    assert sorted((h[1], h[0]) for h in hist) == sorted(
        [(1, lot_uuid(*A1)), (1, lot_uuid(*A1b)), (2, lot_uuid(*A2))]
    )
    assert {h[3] for h in hist} == {"S0", "S1"}
    assert ask(con, "q8_device_history", q8_lot_name="B") == [
        (lot_uuid(*B1), 1, None, None, None, None, None)
    ]
