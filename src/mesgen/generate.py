"""Orchestrate a run: simulate days, write base/stream Parquet, the inputs used, churn log and manifest."""

import datetime as dt
import hashlib
import json
import pathlib
import time
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from . import __version__
from .events import drop_keys
from .profile import load_profile
from .rng import Rng
from .scenario import load_scenario
from .schema import CHURN_SCHEMA
from .sim import Simulation
from .writer import write_day, write_table


def generate(
    scenario_ref: str | pathlib.Path,
    out_dir: str | pathlib.Path,
    seed: int | None = None,
    profile_ref: str | pathlib.Path | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Simulate a scenario and write its output folder.

    Writes the event files, the inputs used, the churn log and the manifest.

    Args:
        scenario_ref: Bundled scenario name or path to a YAML file.
        out_dir: Output directory. Must be new or empty.
        seed: Seed to use instead of the scenario's.
        profile_ref: Profile to use instead of the scenario's, by name or path.
        quiet: If True, don't print progress.

    Returns:
        The manifest, as written to manifest.json.

    Raises:
        FileExistsError: If the output directory is not empty.
        AssertionError: If a row is dated before the day that emitted it (a bug).
    """
    sc = load_scenario(scenario_ref, seed=seed)
    profile = load_profile(profile_ref or sc["profile"])
    out = pathlib.Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(
            f"{out} is not empty: generate into a new folder, so no file from another run is mixed in"
        )
    (out / "inputs").mkdir(parents=True)
    # the exact inputs travel with the data, so every output folder shows what produced it
    scenario_text = json.dumps(
        {k: v for k, v in sc.items() if not k.startswith("_")}, indent=1, sort_keys=True
    )
    (out / "inputs" / "scenario.json").write_text(scenario_text)
    (out / "inputs" / "profile.json").write_text(
        pathlib.Path(profile["_path"]).read_text()
    )
    rng = Rng(sc["seed"])
    sim = Simulation(profile, sc, rng)
    t_start = time.time()
    files, per_day = [], []
    # event day -> (emission day, rows); some rows are dated after the day that emitted them (split children past
    # midnight, a scrapped lot's storage stay)
    pending: dict[int, list[tuple[int, pa.Table]]] = {}
    retired: dict[int, set[str]] = {}  # day -> property keys retired that day
    for day in range(sc["days"]):
        n_log = len(sim.world.churn_log)
        table = sim.run_day(day)
        retired[day] = {
            entity
            for _, _, _, kind, entity, _ in sim.world.churn_log[n_log:]
            if kind == "retired_property_key"
        }
        for ev_day, part in _by_event_day(table, sim.epoch_us):
            if ev_day < day:
                raise AssertionError(f"row dated day {ev_day} emitted on day {day}")
            pending.setdefault(ev_day, []).append((day, part))
        # a day's partition is final once that day is simulated: no later day emits a row dated earlier
        parts = [
            _drop_retired_keys(
                part, set().union(*(retired[d] for d in range(e + 1, day + 1)))
            )
            for e, part in pending.pop(day, [])
        ]
        today = pa.concat_tables(parts) if parts else table.slice(0, 0)
        files += write_day(today, out, day, base=day < sc["base_days"])
        per_day.append(
            {
                "day": day,
                "rows": today.num_rows,
                "live_lots": int(sim.lots.alive[: sim.lots.n].sum()),
            }
        )
        if not quiet and (day % 10 == 0 or day == sc["days"] - 1):
            rate = sim.counts["events"] / max(1e-9, time.time() - t_start) * 60
            print(
                f"day {day:4d}: {table.num_rows:8d} raw rows, {per_day[-1]['live_lots']:8d} live lots, {rate:,.0f} events/min",
                flush=True,
            )
    churn = pa.table(
        {
            "sim_ts": pa.array(
                [sim.epoch_us + s * 1_000_000 for s, *_ in sim.world.churn_log],
                type=pa.int64(),
            ).cast(pa.timestamp("us")),
            "sim_day": pa.array(
                [d for _, d, *_ in sim.world.churn_log], type=pa.int32()
            ),
            "churn_class": [c for _, _, c, *_ in sim.world.churn_log],
            "kind": [k for _, _, _, k, *_ in sim.world.churn_log],
            "entity": [e for *_, e, _ in sim.world.churn_log],
            "detail": [d for *_, d in sim.world.churn_log],
        },
        schema=CHURN_SCHEMA,
    )
    write_table(churn, out / "churn_log.parquet")
    beyond = sum(
        t.num_rows for parts in pending.values() for _, t in parts
    )  # dated after the horizon: not yet happened
    manifest = {
        "mesgen_version": __version__,
        "scenario": {
            "name": sc["name"],
            "sha256": hashlib.sha256(scenario_text.encode()).hexdigest(),
            "seed": sc["seed"],
            "days": sc["days"],
            "base_days": sc["base_days"],
        },
        "profile": {"sha256": profile["_hash"]},
        "epoch": profile["template"]["epoch"],
        "counts": sim.counts | {"raw_rows": sum(d["rows"] for d in per_day)},
        "rows_beyond_horizon": beyond,
        "per_day": per_day,
        "churn_events": len(sim.world.churn_log),
        "planted_cohorts": sim.planted,
        "orphan_legacy_lots": sim.orphan_lots,
        "reminted_keys": sim.remint_count,
        "files": len(files),
        "content_sha256": _sha256_files(out, files + [str(out / "churn_log.parquet")]),
        "generated_at_wallclock": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "runtime_seconds": round(time.time() - t_start, 1),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def _sha256_files(out: pathlib.Path, paths: list[str]) -> str:
    """Hash the paths and contents of the data files, in path order.

    Args:
        out: Output directory. Paths are hashed relative to it.
        paths: Files to hash.

    Returns:
        SHA-256 hex digest.
    """
    h = hashlib.sha256()
    for p in sorted(pathlib.Path(p) for p in paths):
        h.update(p.relative_to(out).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def _drop_retired_keys(table: pa.Table, keys: set[str]) -> pa.Table:
    """Remove retired property keys from rows that were emitted before the key retired.

    Args:
        table: Rows.
        keys: Property keys retired after the rows were emitted, on or before their date.

    Returns:
        Rows without those keys.
    """
    if not keys:
        return table
    for col in ("in_properties", "out_properties"):
        i = table.schema.get_field_index(col)
        values = [
            None if v is None else drop_keys(v, keys) for v in table[col].to_pylist()
        ]
        table = table.set_column(
            i, table.schema.field(i), pa.array(values, table.schema.field(i).type)
        )
    return table


def _by_event_day(table: pa.Table, epoch_us: int) -> list[tuple[int, pa.Table]]:
    """Split one day's output by the day of each row's `event_ts`.

    Rows keep their order.

    Args:
        table: Rows emitted on one day.
        epoch_us: Start of day 0 in microseconds.

    Returns:
        (day, rows) pairs.
    """
    if table.num_rows == 0:
        return []
    ev_day = (pc.cast(table["event_ts"], pa.int64()).to_numpy() - epoch_us) // (
        86400 * 1_000_000
    )
    days = np.unique(ev_day)
    if len(days) == 1:
        return [(int(days[0]), table)]
    return [(int(d), table.filter(pa.array(ev_day == d))) for d in days]
