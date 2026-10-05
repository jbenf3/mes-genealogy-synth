# Reference queries and expected answers

Eight genealogy queries, each written once in SQL directly against the event table, with their expected answers for the
`smoke` and `official_1x` scenarios. Your closure-table and graph-database implementations must give the same answers.

| Path | Content |
|---|---|
| `queries/events.sql` | The view `events`: every row of one output folder, base load and stream. Every query reads it. |
| `queries/q1_ancestry.sql` … `q8_device_history.sql` | The reference queries, in the DuckDB dialect. |
| `queries/run.py` | Runs every query for one scenario and writes the answers as CSV files. |
| `expected/<scenario>/parameters.sql` | The input of every query for that scenario: a lot, a machine, a time window. |
| `expected/<scenario>/q1_ancestry.csv` … | The expected answers. |
| `expected/<scenario>/content_sha256.txt` | The fingerprint of the data the answers hold for. |

## The queries

| | Question | Given | Answer columns |
|---|---|---|---|
| Q1 | What was this lot state made from? | a lot state | `lot_uuid` of every ancestor state |
| Q2 | What was made from this lot state? | a lot state | `lot_uuid` of every descendant state |
| Q3 | Which equipment do failing units share? | the test step, a time window | `equipment_path`, `failing`, `passing` |
| Q4 | What went into this lot? | a lot name | `lot_name` of every other name among its ancestors |
| Q5 | Where was this lot used? | a lot name | `lot_name` of every other name among its descendants |
| Q6 | What was processed on this machine in this window? | equipment, window | `lot_name` of every lot consumed or produced there |
| Q7 | What could a machine problem have affected? | equipment, window | `lot_name` of the lots produced there and of everything made from them |
| Q8 | What is this lot's history? | a lot name | per state: `lot_uuid`, `sequence`, `material`, `segment_name`, `equipment_path`, `location`, `begin_ts` |

**States and names.** A lot state is one `lot_uuid`. A lot name has many states, because every event gives the lot a new
sequence and a new uuid. The edges of the genealogy are the `in_lot_uuid → out_lot_uuid` pairs; creation rows (no input)
and disposal rows (no output) add no edge.
- Q1, Q2 and Q7 walk states.
- Q4 and Q5 start from every state of the name, and report names.

**Windows** include their start and exclude their end: `from ≤ begin_ts < to`.

**Duplicates** in the raw rows change no answer. Copies repeat edges and values that are already there, and the columns
where copies differ (`operator`, `in_prev_segment`) are in no answer. So the queries use `DISTINCT` rather than the
deduplication rule in `docs/SCHEMA.md`.

**Q3.**
- **Members:** the units that went through the test step in the window.
- **Failed:** a member whose next event scrapped the unit (the event's output status is `ST_2`).
- **History:** the events that produced the member or any of its ancestors.
- **Answer:** for each equipment, how many failing and how many passing members have it in their history.

Each planted cause (`planted_cohorts` in `manifest.json`) shows within its step. The planted equipment is in the history
of a clearly larger share of the failing members than of the passing ones; the step's other equipment is not.

**Q7** walks states, not names. A feeder keeps its name across refills, so the parts dispensed from it before the refill
are not affected.

**Q8** has one row per state:
- a re-minted sequence has two uuids, so two rows;
- for a legacy state, which no event in the data produced, the event columns are null.

## Run the reference queries

```bash
uv run python queries/run.py official_1x out/official_1x out/results/official_1x
diff -r out/results/official_1x expected/official_1x      # only parameters.sql differs: run.py does not write it
```

Or one query at a time, in the DuckDB command line:

```sql
SET VARIABLE data = 'out/official_1x';
.read queries/events.sql
.read expected/official_1x/parameters.sql
.read queries/q1_ancestry.sql
```

All eight take a few seconds on `smoke`. On `official_1x` they take 1 to 3 minutes and 5.3 GB of memory, most of it for Q3.

## Compare your answers

1. Check that `content_sha256` in your `manifest.json` equals `expected/<scenario>/content_sha256.txt`. If it does not,
   your data differs and the expected answers do not apply; see "Keep the answers the same" in the README.
2. Run your query with the values in `parameters.sql`.
3. Compare its rows with the CSV file as a set: the order of rows does not matter. Timestamps are written as
   `YYYY-MM-DD HH:MM:SS.ffffff`, with no time zone.

## Worked example

`tests/test_graph_queries.py` builds this graph row by row, runs the reference queries on it and asserts the answers
below. Rows have equipment `AR01/EQ_<segment>` unless the diagram names one.

```
A1 (created) -> A1b (re-mint: same name and sequence, variant uuid) -> A2 ─┐
B1 (legacy: consumed, never produced) ──────────────────────────────────────┴─ merge -> C1 -> C2 (storage) -> disposal
F1 (feeder) ─ dispense -> P2 (P1 -> P2);   F1 -> F2 (refill on EQ_R at t=6 s);   F2 ─ dispense -> Q2 (Q1 -> Q2)
X1 + Y1 ─ merge on EQ_W at t=10 s -> Z1 -> Z2 on EQ_W at t=11 s
Z2 -> Z3 and P2 -> P3 on the test step S9 (EQ_T, t=12 s);   Z3 -> Z4 scraps Z (t=13 s)
```

| Query | Answer |
|---|---|
| Q1 ancestry(C2) | C1, A2, B1, A1b, A1 |
| Q2 descendants(C1) | C2 |
| Q3 S9, [12 s, 13 s) | `AR01/EQ_S0` 1 failing, 1 passing; `AR01/EQ_S4` 0, 1; `EQ_T` 1, 1; `EQ_W` 1, 0 (Z3 failed, P3 passed) |
| Q4 bom(C) | A, B |
| Q5 where-used(F) | P, Q |
| Q6 EQ_W, [10 s, 11 s) | X, Y, Z |
| Q7 EQ_R, [6 s, 6 s + 1 µs) | F, Q (not P: it was dispensed from F1) |
| Q8 history(A) | three rows: A1 and A1b, both at sequence 1, then A2 |

The raw rows also carry a null-twin copy and a conflicting copy (another operator, higher id). Neither changes an answer.

## When the data changes

A change to the generator that changes the data makes `tests/test_expected.py` fail. To refresh the expected answers:
1. Generate `smoke` and `official_1x` into new folders.
2. Check that every value in each `parameters.sql` still fits its comment, because the lots differ when the data
   changes. Choose new values where needed.
3. Run `uv run python queries/run.py <scenario> <data folder> expected/<scenario>` for each scenario.
