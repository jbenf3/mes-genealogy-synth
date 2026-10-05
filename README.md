# mesgen

mesgen generates a synthetic event log for a manufacturing line. There is one row per processing event, in
the shape of a manufacturing execution system (MES) edge table with generic names for equipment, segments,
materials, operators, property keys, etc. It also ships:
- eight reference genealogy queries, in SQL that reads the event files directly;
- their expected answers for two scenarios, `smoke` and `official_1x`.

Use them to verify a closure table or a graph database built from the data. The same scenario, seed and mesgen version
give byte-identical data on any machine.

## 1. Install

Install [uv](https://docs.astral.sh/uv/), then run these in the repository folder:

```bash
uv sync
uv run mesgen --version
```

`uv sync` creates `.venv` with the Python version in `.python-version` and the exact dependency versions pinned in
`pyproject.toml`. Run every command through `uv run`, as below.

## 2. Generate the data

```bash
uv run mesgen generate --scenario smoke --out out/smoke
uv run mesgen generate --scenario official_1x --out out/official_1x
```

`generate` simulates the scenario one day at a time and writes the event files. `--out` must be a new or empty folder.

| Scenario | Days | Base load | What it is |
|---|---|---|---|
| `smoke` | 12 | days 0–7 | Seconds to generate; try everything on it first |
| `official_1x` | 273 | days 0–181 | Nine months at the base volume, ramping to 1.9× |

`official_1x` takes about 13 minutes and 0.7 GB of memory to generate. It writes 2.1 GB of files: 9.1 million events in
14.4 million raw rows.

What `--out` holds:

| Path | Content |
|---|---|
| `base/day=0000.parquet` … | Base load: one file per day up to the base-load end. |
| `stream/day=0182/hour=06.parquet` … | Update stream: one file per simulated hour. Every row sits in the file of its own day and hour. |
| `inputs/` | The exact scenario and profile this run used. |
| `churn_log.parquet` | Every process change. |
| `manifest.json` | Seed, counts, the planted failure causes, and `content_sha256`: a fingerprint of the data files. |

## 3. Load the data into your implementation

1. Read every file under `base/`, then `stream/`, in path order (day, then hour).
2. Deduplicate. The raw rows contain duplicates on purpose; keep one row per (`in_lot_uuid`, `out_lot_uuid`, `segment_uuid`, `begin_ts`), preferring a non-null `in_prev_segment`, then the lowest `id`. The rule as SQL is in `docs/SCHEMA.md`.
3. Make each `lot_uuid` a node and each distinct `in_lot_uuid → out_lot_uuid` an edge. Key nodes on the uuid, never on lot name and sequence: a few re-minted keys give one name and sequence two uuids.

`docs/SCHEMA.md` describes every column.

## 4. Answer the reference queries

`docs/QUERIES.md` defines the eight queries. For each scenario, `expected/<scenario>/parameters.sql` gives the input of
every query, and `expected/<scenario>/q1_ancestry.csv` … its expected answer.

1. Check that `content_sha256` in your `manifest.json` equals `expected/<scenario>/content_sha256.txt`. If it does not, your data differs from the data the answers were computed on; see section 6.
2. Answer each query with your implementation, using the values in `parameters.sql`.
3. Compare your rows with the CSV file as a set: the order of rows does not matter.

The reference SQL in `queries/` reads the event files directly. To run it:

```bash
uv run python queries/run.py official_1x out/official_1x out/results/official_1x
```

It writes one CSV file per query, and `content_sha256.txt`, in the same form as `expected/official_1x/`. It takes a few
seconds on `smoke`, and 1 to 3 minutes and 5.3 GB of memory on `official_1x`.
`docs/QUERIES.md` shows how to run one query by hand in the DuckDB command line.

## 5. Configure a scenario

Copy a scenario, edit it, and pass the file. Expected answers exist only for `smoke` and `official_1x`.

```bash
cp scenarios/official_1x.yaml my_ramp.yaml
uv run mesgen generate --scenario my_ramp.yaml --out out/my_ramp
```

| Key | Meaning |
|---|---|
| `seed` | Seed of every random draw. |
| `days`, `base_days` | Simulated days in total; days written to `base/` (the rest go to `stream/`). |
| `volume.scale` | Daily volume relative to the base volume (1.0). |
| `volume.ramp_last_over_first` | Production ramp: daily volume grows in a straight line from 1× on day 0 to this multiple on the last day. |
| `churn_multiplier` | Multiplies the weekly rates of new equipment, operators, segments, materials and property keys. |
| `rework_rate` | Share of lots sent back to an earlier step at the rework point. |
| `cohorts` | Planted failure causes: `{segment, day, effect, background}`. From `day` on, units processed on one equipment of that segment fail the test step (`SEG_041`) at rate `effect`, and the other units at `background`. Failed units are scrapped. Query Q3 finds the cause. |
| `duplicates.enabled`, `anomalies.enabled` | Turn the deliberate duplicates and anomalies off (`false`) for a clean stream. |

For example, a year that starts at half the base volume and ends at three times it:

```yaml
name: my_ramp
seed: 7
days: 365
base_days: 240
volume: {scale: 0.5, ramp_last_over_first: 6.0}
cohorts:
  - {segment: SEG_032, day: 120, effect: 0.35, background: 0.05}
```

The weekly pattern, the process routes and every rate per step are in `profiles/default.json`. Pass `--profile` to use a
copy of it with your changes.

## 6. Keep the answers the same

The data depends only on the scenario, the seed, the profile and the mesgen version. Follow these rules, so your data
matches the data the expected answers were computed on:
- Generate `smoke` or `official_1x` by name, without `--seed` or `--profile`.
- Do not edit the files in `scenarios/` or `profiles/`.
- Install with `uv sync` and keep the versions pinned in `pyproject.toml`: do not run `uv add`, `uv lock --upgrade` or `uv pip install` for numpy, pyarrow, pyyaml or duckdb.
- Generate into a new folder every time; `generate` refuses a folder that is not empty.
- Do not edit, delete or rename anything inside an output folder. Copy or move only the whole folder.
- Compare `content_sha256` in `manifest.json` with `expected/<scenario>/content_sha256.txt`.

## Repository

```
src/mesgen/   the generator: sim.py, world.py (the simulation), generate.py (writes the files), cli.py
queries/      the reference SQL (DuckDB) and run.py, which runs it
expected/     the expected answers for smoke and official_1x
profiles/     default.json: the simulated process (materials, segments, routes, rates)
scenarios/    smoke and official_1x
docs/         SCHEMA.md (the data), QUERIES.md (the queries)
tests/        run them with `uv run pytest`
```

`uv run ruff format` formats the code, and `uv run ruff check` checks that every function has a
Google-style docstring and type hints.

License: MIT. See `LICENSE`.
