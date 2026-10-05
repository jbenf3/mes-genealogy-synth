"""Checks on the generated smoke data."""

import collections
import datetime as dt
import json
import pathlib
from typing import Any

import duckdb
import pyarrow.parquet as pq
import pytest
import yaml

from mesgen.generate import generate
from mesgen.profile import load_profile

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def smoke_run(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[pathlib.Path, dict[str, Any]]:
    """Generate the smoke scenario once for this module.

    Args:
        tmp_path_factory: Pytest temporary directory factory.

    Returns:
        Output directory and manifest.
    """
    out = tmp_path_factory.mktemp("smoke")
    manifest = generate("smoke", out, quiet=True)
    return out, manifest


@pytest.fixture(scope="module")
def recovery_run(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Generate smoke with a profile that recovers about a third of scrapped lots.

    Args:
        tmp_path_factory: Pytest temporary directory factory.

    Returns:
        Output directory.
    """
    tmp = tmp_path_factory.mktemp("recovery")
    profile = load_profile("default")
    profile["template"]["scrap"]["recover_share"] = 0.3
    (tmp / "profile.json").write_text(json.dumps(profile))
    generate("smoke", tmp / "out", profile_ref=tmp / "profile.json", quiet=True)
    return tmp / "out"


@pytest.fixture(scope="module")
def changed_run(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Generate smoke with settings that the shipped scenarios don't use.

    The profile's day-42 changes happen on day 2, dispensing takes an hour and the
    next step has no wait. A third of the tested units are reworked, and a planted
    cause on merge SEG_014 fails every unit that went through it.

    Args:
        tmp_path_factory: Pytest temporary directory factory.

    Returns:
        Output directory.
    """
    tmp = tmp_path_factory.mktemp("changed")
    profile = load_profile("default")
    for ev in profile["template"]["churn"]["structural"]:
        if ev["day"] == 42:
            ev["day"] = 2
    for s in profile["segments"]:
        if s["id"] == "SEG_011":
            s["duration_s"] = 3600
        if s["id"] == "SEG_032":
            s["gap_min"] = 0
    (tmp / "profile.json").write_text(json.dumps(profile))
    scenario = yaml.safe_load((REPO / "scenarios" / "smoke.yaml").read_text())
    scenario["rework_rate"] = 0.3
    scenario["cohorts"] = [
        {"segment": "SEG_014", "day": 0, "effect": 1.0, "background": 0.0}
    ]
    (tmp / "scenario.yaml").write_text(yaml.safe_dump(scenario))
    out = tmp / "out"
    generate(tmp / "scenario.yaml", out, profile_ref=tmp / "profile.json", quiet=True)
    return out


@pytest.fixture(scope="module")
def clean_run(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Generate smoke with the duplicates and anomalies turned off.

    Args:
        tmp_path_factory: Pytest temporary directory factory.

    Returns:
        Output directory.
    """
    tmp = tmp_path_factory.mktemp("clean")
    scenario = yaml.safe_load((REPO / "scenarios" / "smoke.yaml").read_text())
    scenario["duplicates"] = {"enabled": False}
    scenario["anomalies"] = {"enabled": False}
    (tmp / "scenario.yaml").write_text(yaml.safe_dump(scenario))
    generate(tmp / "scenario.yaml", tmp / "out", quiet=True)
    return tmp / "out"


def _rows(out: pathlib.Path) -> list[dict[str, Any]]:
    """Read every event row of an output folder.

    Args:
        out: Output directory.

    Returns:
        Rows as dicts, base load first.
    """
    return (
        pq.read_table(out / "base").to_pylist()
        + pq.read_table(out / "stream", partitioning=None).to_pylist()
    )


def assert_no_cycles(rows: list[dict[str, Any]]) -> None:
    """Check that the lot graph has no cycles.

    Uses Kahn's algorithm: every lot state must come out of the topological sort.

    Args:
        rows: Event rows.
    """
    edges = {
        (r["in_lot_uuid"], r["out_lot_uuid"])
        for r in rows
        if r["in_lot_uuid"] and r["out_lot_uuid"]
    }
    children, parents = collections.defaultdict(list), collections.Counter()
    for a, b in edges:
        children[a].append(b)
        parents[b] += 1
    ready, ordered = [a for a in {a for a, _ in edges} if parents[a] == 0], 0
    while ready:
        ordered += 1
        for c in children[ready.pop()]:
            parents[c] -= 1
            if parents[c] == 0:
                ready.append(c)
    assert ordered == len({n for e in edges for n in e})


def assert_one_material_per_state(rows: list[dict[str, Any]]) -> None:
    """Check that each lot state has the same material everywhere it appears.

    Args:
        rows: Event rows.
    """
    material: dict[str, set[str]] = collections.defaultdict(set)
    for r in rows:
        if r["in_lot_uuid"]:
            material[r["in_lot_uuid"]].add(r["in_material"])
        if r["out_lot_uuid"]:
            material[r["out_lot_uuid"]].add(r["out_material"])
    assert [u for u, m in material.items() if len(m) > 1] == []


def test_smoke_produces_base_stream_and_manifest(
    smoke_run: tuple[pathlib.Path, dict[str, Any]],
) -> None:
    """Smoke writes base files, stream files and a manifest."""
    out, manifest = smoke_run
    assert (out / "manifest.json").exists() and (out / "churn_log.parquet").exists()
    assert len(list((out / "base").glob("*.parquet"))) == 8
    assert len(list((out / "stream").glob("day=*/hour=*.parquet"))) > 0
    assert manifest["counts"]["events"] > 1000
    assert manifest["counts"]["raw_rows"] > manifest["counts"]["events"]


def test_every_event_bumps_sequence_and_uuid_is_unique_per_key(
    smoke_run: tuple[pathlib.Path, dict[str, Any]],
) -> None:
    """Each event makes a new lot state, and each state has one producer."""
    out, _ = smoke_run
    t = pq.read_table(out / "base")
    rows = t.to_pylist()
    same_uuid = [
        r
        for r in rows
        if r["in_lot_uuid"]
        and r["out_lot_uuid"]
        and r["in_lot_uuid"] == r["out_lot_uuid"]
    ]
    assert not same_uuid
    same_name = [
        r
        for r in rows
        if r["in_lot_name"]
        and r["out_lot_name"]
        and r["in_lot_name"] == r["out_lot_name"]
    ]
    plus_one = sum(
        1 for r in same_name if r["out_lot_sequence"] == r["in_lot_sequence"] + 1
    )
    assert plus_one / len(same_name) > 0.99
    producers = {}
    for r in rows:
        if r["out_lot_uuid"]:
            producers.setdefault(r["out_lot_uuid"], set()).add(r["segment_uuid"])
    assert all(len(v) == 1 for v in producers.values())


def test_manifest_records_inputs(
    smoke_run: tuple[pathlib.Path, dict[str, Any]],
) -> None:
    """The manifest records the seed, the profile hash and the data hash."""
    out, manifest = smoke_run
    m = json.loads((out / "manifest.json").read_text())
    assert (
        m["scenario"]["seed"] == 11
        and m["profile"]["sha256"]
        and m["content_sha256"] == manifest["content_sha256"]
    )


def test_genealogy_has_no_cycles(
    smoke_run: tuple[pathlib.Path, dict[str, Any]],
) -> None:
    """The lot graph has no cycles."""
    assert_no_cycles(_rows(smoke_run[0]))


def test_planted_failures_are_scrapped_after_the_test_step(
    smoke_run: tuple[pathlib.Path, dict[str, Any]],
) -> None:
    """Some of the units that go through the test step are scrapped right after it."""
    out, manifest = smoke_run
    scrapped = json.loads((out / "inputs" / "profile.json").read_text())["template"][
        "disposition"
    ]["statuses"]["scrap"]
    rows = _rows(out)
    tested = {
        r["out_lot_uuid"]
        for r in rows
        if r["segment_name"] == manifest["planted_cohorts"][0]["outcome_segment"]
    }
    failed = {
        r["in_lot_uuid"]
        for r in rows
        if r["in_lot_uuid"] in tested and r["out_lot_status"] == scrapped
    }
    assert 0 < len(failed) < len(tested)


def assert_scrap_rules(
    out: pathlib.Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Check the rules in docs/SCHEMA.md, "Scrap and disposal".

    Args:
        out: Output directory.

    Returns:
        Every raw row, and the profile's scrap settings.
    """
    t = json.loads((out / "inputs" / "profile.json").read_text())["template"]
    s, values, statuses = (
        t["scrap"],
        t["disposition"]["values"],
        t["disposition"]["statuses"],
    )
    ok, scrapped = statuses["complete"], statuses["scrap"]
    rows = _rows(out)
    producer = {r["out_lot_uuid"]: r for r in rows if r["out_lot_uuid"]}
    scrap = [r for r in rows if r["segment_name"] == s["segment"]]
    assert scrap and all(
        r["out_lot_name"] == r["in_lot_name"]
        and (r["in_lot_status"], r["out_lot_status"]) == (ok, scrapped)
        for r in scrap
    )
    # only the scrap event makes a lot scrapped, and only it carries the scrap disposition
    assert all(
        r["segment_name"] == s["segment"] or r["in_lot_status"] == scrapped
        for r in rows
        if r["out_lot_status"] == scrapped
    )
    assert {
        r["segment_name"]
        for r in rows
        if r["disposition"] in (values["scrap"], values["variant"])
    } == {s["segment"]}
    # a lot keeps the status that the event producing it gave it
    assert all(
        r["in_lot_status"] == producer[r["in_lot_uuid"]]["out_lot_status"]
        for r in rows
        if r["in_lot_uuid"] in producer
    )
    # the only event with no output lot: disposal of a scrapped lot just taken out of storage
    disposals = [r for r in rows if r["out_lot_uuid"] is None]
    assert disposals and all(
        r["segment_name"] == s["disposal_segment"]
        and r["in_lot_status"] == scrapped
        and producer[r["in_lot_uuid"]]["segment_name"] == s["store_segments"][-1]
        for r in disposals
    )
    assert all(
        (r["in_lot_status"], r["out_lot_status"]) == (scrapped, ok)
        for r in rows
        if r["segment_name"] == s["recover_segment"]
    )
    return rows, s


def test_scrap_is_its_own_event(smoke_run: tuple[pathlib.Path, dict[str, Any]]) -> None:
    """Scrapping, storage, disposal and recovery follow the documented rules."""
    assert_scrap_rules(smoke_run[0])


def test_recovered_lots_continue_their_route(recovery_run: pathlib.Path) -> None:
    """Recovered lots go on to normal route steps, and the graph has no cycles."""
    rows, s = assert_scrap_rules(recovery_run)
    recovered = {
        r["out_lot_uuid"] for r in rows if r["segment_name"] == s["recover_segment"]
    }
    consumers = [r for r in rows if r["in_lot_uuid"] in recovered]
    assert (
        recovered
        and consumers
        and all(
            r["segment_name"]
            not in (s["segment"], s["disposal_segment"], s["recover_segment"])
            for r in consumers
        )
    )
    assert_no_cycles(rows)


def test_each_lot_state_has_one_material(
    smoke_run: tuple[pathlib.Path, dict[str, Any]],
) -> None:
    """A lot state has the same material as output and as input."""
    assert_one_material_per_state(_rows(smoke_run[0]))


def test_roll_lengths_are_whole_numbers_and_only_mass_has_a_unit(
    smoke_run: tuple[pathlib.Path, dict[str, Any]],
) -> None:
    """Rolls have whole-number lengths, and `unit` is set exactly for mass lots."""
    out, _ = smoke_run
    profile = json.loads((out / "inputs" / "profile.json").read_text())
    regime = {m["id"]: m["regime"] for m in profile["materials"]}
    rows = [r for r in _rows(out) if r["out_lot_uuid"]]
    rolls = [r["qty_out"] for r in rows if regime[r["out_material"]] == "roll"]
    assert rolls and all(q == round(q) for q in rolls)
    assert all(
        (r["unit"] is not None) == (regime[r["out_material"]] == "mass") for r in rows
    )


def test_reworked_units_change_material_in_the_test_step_event(
    changed_run: pathlib.Path,
) -> None:
    """A reworked unit's test-step event writes the material it goes back to."""
    rows = _rows(changed_run)
    assert {r["out_material"] for r in rows if r["segment_name"] == "SEG_041"} == {
        "MAT_02",
        "MAT_05",
    }
    assert_one_material_per_state(rows)


def test_new_input_material_is_cut_from_rolls_and_merged(
    changed_run: pathlib.Path,
) -> None:
    """The material that replaces part of MAT_07 comes out of its split and goes into both merges that take MAT_07."""
    rows = _rows(changed_run)
    cut = {r["out_material"] for r in rows if r["segment_name"] == "SEG_010"}
    new = cut - {"MAT_07"}
    assert "MAT_07" in cut and len(new) == 1
    for merge in ("SEG_013", "SEG_014"):
        assert new <= {r["in_material"] for r in rows if r["segment_name"] == merge}


def test_work_after_dispensing_starts_after_it_ends(changed_run: pathlib.Path) -> None:
    """The step after a dispense never starts before the dispense ends."""
    rows = _rows(changed_run)
    ends = {
        r["out_lot_uuid"]: r["end_ts"]
        for r in rows
        if r["segment_name"] == "SEG_011" and r["in_material"] == "MAT_11"
    }
    after = [r for r in rows if r["in_lot_uuid"] in ends]
    assert after and all(r["begin_ts"] >= ends[r["in_lot_uuid"]] for r in after)


def test_cause_on_a_merge_fails_every_unit_through_it(
    changed_run: pathlib.Path,
) -> None:
    """Q3 finds the planted merge equipment in failing units only."""
    planted = json.loads((changed_run / "manifest.json").read_text())[
        "planted_cohorts"
    ][0]["equipment"]
    con = duckdb.connect()
    con.execute(f"SET VARIABLE data = '{changed_run.as_posix()}'")
    con.execute((REPO / "queries" / "events.sql").read_text())
    con.execute("SET VARIABLE q3_segment = 'SEG_041'")
    con.execute("SET VARIABLE q3_from = TIMESTAMP '1900-01-01'")
    con.execute("SET VARIABLE q3_to = TIMESTAMP '2100-01-01'")
    rows = con.execute((REPO / "queries" / "q3_commonality.sql").read_text())
    counts = {eq: (failing, passing) for eq, failing, passing in rows.fetchall()}
    assert counts[planted][0] > 0 and counts[planted][1] == 0


def test_no_row_uses_a_property_key_after_it_retired(
    smoke_run: tuple[pathlib.Path, dict[str, Any]],
) -> None:
    """Rows dated after a key's retirement don't have the key, even rows emitted earlier."""
    out, _ = smoke_run
    retired = {
        r["entity"]: r["sim_ts"]
        for r in pq.read_table(out / "churn_log.parquet").to_pylist()
        if r["kind"] == "retired_property_key"
    }
    assert retired
    late = [
        (k, r["segment_name"])
        for r in _rows(out)
        for k, t in retired.items()
        if r["event_ts"] >= t
        and f'"{k}"' in (r["in_properties"] or "") + (r["out_properties"] or "")
    ]
    assert late == []


def test_retired_inserted_steps_have_no_later_events(
    smoke_run: tuple[pathlib.Path, dict[str, Any]],
) -> None:
    """No lot goes through an inserted step on or after the step's retirement day."""
    out, manifest = smoke_run
    epoch = dt.datetime.fromisoformat(manifest["epoch"]).replace(tzinfo=None)
    retires = {
        r["entity"]: epoch
        + dt.timedelta(days=int(r["detail"].rsplit("retire_day=", 1)[1]))
        for r in pq.read_table(out / "churn_log.parquet").to_pylist()
        if r["kind"] == "new_segment_type" and not r["detail"].endswith("=None")
    }
    assert retires
    late = [
        r["segment_name"]
        for r in _rows(out)
        if r["segment_name"] in retires and r["begin_ts"] >= retires[r["segment_name"]]
    ]
    assert late == []


def test_a_clean_stream_points_every_input_at_its_producer(
    clean_run: pathlib.Path,
) -> None:
    """With anomalies off, each input names the segment execution that produced it."""
    rows = _rows(clean_run)
    producer = {r["out_lot_uuid"]: r["segment_uuid"] for r in rows if r["out_lot_uuid"]}
    inputs = [r for r in rows if r["in_lot_uuid"] in producer]
    assert inputs and all(
        r["in_prev_segment"] == producer[r["in_lot_uuid"]] for r in inputs
    )
