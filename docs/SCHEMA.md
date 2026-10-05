# Event schema

Every row is one **event**: a process segment consumed an input lot and produced an output lot. The genealogy is
the graph whose edges are `in_lot_uuid → out_lot_uuid`. Files are Parquet; the Arrow schema is
`mesgen.schema.EVENT_SCHEMA`.

## Lot identity

- **Names and states.** A lot keeps its **name** for life. Every event that touches it increments its **sequence**, and `lot_uuid` identifies that state of the lot.
- **No cycles.** The sequence only increases, so the uuid graph has no cycles. A lot that is reworked keeps its name and returns to an earlier step.
- **Key nodes on `lot_uuid`, never on (name, sequence).** A re-mint gives one (name, sequence) a second uuid; see the anomalies below.

## Columns

| Column | Type | Meaning |
|---|---|---|
| `id` | int64 | Row id: unique, increasing in the order rows were emitted. Duplicate rows have their own id. |
| `event_ts` | timestamp[us] | Equals `begin_ts`. Every row is written to the partition of its own `event_ts` day and hour. |
| `begin_ts`, `end_ts` | timestamp[us] | Event start and end, on a simulated clock that starts at 2000-01-03. Most events have zero duration because the event corresponds to an MES transaction. |
| `segment_name` | string | Process segment type (e.g., `SEG_017`). |
| `segment_uuid` | string | Id of one **execution** of a segment. All inputs to a blending step share the same `segment_uuid`; each child of a one-to-many step has a unique `segment_uuid`. |
| `operation_id` | string | A second per-execution id, always paired with `segment_uuid`. |
| `in_lot_name`, `in_lot_sequence`, `in_lot_uuid` | string, int32, string | Input lot state. Null for creation events. |
| `in_material` | string | Material of the input lot (e.g., `MAT_07`). |
| `in_lot_status` | string | `ST_1` (normal) or `ST_2` (scrapped). See "Scrap and disposal" below. |
| `in_prev_segment` | string | `segment_uuid` of the event that produced the input lot. |
| `out_lot_name`, `out_lot_sequence`, `out_lot_uuid` | string, int32, string | Output lot state. Null only for disposal events. |
| `out_material` | string | Material of the output lot. |
| `out_lot_status` | string | `ST_1` (normal) or `ST_2` (scrapped). The scrap event sets `ST_2`; a recovery sets `ST_1` again. |
| `location`, `location_type` | string | Where the event happened. `location` is one-to-one with `equipment_path`. Types: `LINE`, `STORAGE`, `STATION`, `STATION_GROUP`, `DOCK`. Storage moves are ordinary events: they bump the sequence. |
| `equipment_path` | string | Equipment id (e.g., `AR03/EQ_041`): area followed by equipment. |
| `operator` | string | Operator id (e.g., `OP_017`). |
| `work_order_id` | string | Always null. |
| `disposition` | string | `DISP_1` (scrap), only on scrap events; `DISP_2` (use as is: the lot continues), on process steps; `disp_1`, a lower-case spelling of `DISP_1` that appears from week 4; or null. |
| `disposition_codes` | string (JSON) | Codes for the disposition: `{"D_1": code, "D_2": process, "D_3": action, "D_4": code2, "D_5": code3}`. |
| `ext_system_ref` | string | A reference into another system. |
| `qty_in`, `qty_out`, `qty_net`, `qty_available`, `qty_scheduled` | float64 | Quantities. Discrete lots: `qty_in = 1`, `qty_out` 0 or 1. Rolls: integer lengths. Mass lots: mass units. |
| `unit` | string | `MU` (mass unit) when the output lot is a mass lot, otherwise null. |
| `in_properties`, `out_properties` | string (JSON) | Property bags for the input and output lot. Keys (`P_042`) appear and retire over time, and value types drift. |
| `recipe_json` | string (JSON) | Recipe parameters (`R_01` to `R_07`) for segments that run a recipe, otherwise null. `R_03` is the recipe name. The same for every row of one segment execution. |

## Event classes

| Class | Definition |
|---|---|
| creation | `in_lot_uuid` null, `out_lot_uuid` set: a raw material, roll or consumable enters |
| sequence bump | `in_lot_name = out_lot_name`, sequence + 1: a line step, a storage move or a material change |
| transformation | different lot names: a merge input, a split child, a dispense |
| disposal | `out_lot_uuid` null: a scrapped lot is disposed of after a stay in storage (rare) |

Three kinds of material coexist:
- **Mass:** non-integer quantities, blended about five at a time.
- **Rolls:** integer lengths in the hundreds to thousands, each cut into hundreds of discrete lots. A roll made from a
  blend gets a length in proportion to the blend's mass.
- **Discrete units:** `qty_in = 1`, one sequence bump per step; more than 99% of events.

## Scrap and disposal

Scrapping, disposal and recovery each have their own step, used for nothing else: `SEG_057`, `SEG_058` and `SEG_059`
in the default profile. A scrapped lot goes through these events:

1. **The process step that finds a problem says nothing about scrap.** It may carry `DISP_2` (use as is), but never `DISP_1`.
2. **The scrap event.** The lot passes the scrap step (`SEG_057`). It keeps its name, gets the next sequence, and its status changes from `ST_1` to `ST_2`. Most scrap events carry `DISP_1` and its codes; the rest record only the status change.
3. **Most scrapped lots stop there**, and never appear again.
4. **Some are stored, then disposed of.** A storage check-in and a check-out, both with status `ST_2`, are followed later by a disposal at the disposal step (`SEG_058`). That is the only event with no output lot.
5. **A few are recovered.** The recovery step (`SEG_059`) sets the status back to `ST_1`, and the lot continues its route from the step after the one that found the problem.

Units that fail the test step (`SEG_041`) because of a scenario's planted causes are scrapped the same way, right after it.

## Duplicates and anomalies

The raw rows contain duplicates on purpose. Deduplicate before loading: keep one row per
`(in_lot_uuid, out_lot_uuid, segment_uuid, begin_ts)`, preferring the row with a non-null `in_prev_segment`, then the
lowest `id`:

```sql
qualify row_number() over (partition by in_lot_uuid, out_lot_uuid, segment_uuid, begin_ts
                           order by in_prev_segment is null, id) = 1
```

The duplicate kinds:
- **null twin:** identical except that `in_prev_segment` is null; about 56% of events have one;
- **exact copy;**
- **conflicting copy:** the operator differs, and the copy has the higher `id`.

A row and its copies share every timestamp, so they are always in the same file.

Anomalies (all but the last are rare):
- **Legacy lots:** inputs whose producing event is absent, because they were made before the scenario starts.
- **Clock skew:** events dated up to an hour before their producer's event on the same day.
- **Re-mints:** a `(name, sequence)` key issued twice, with two uuids.
- **Missing producer pointers:** `in_prev_segment` is null on about a third of rows, even when the producing event is
  in the data. With anomalies off, every input whose producer is in the data names it.

After deduplication, the uuid graph has no cycles, each state has one producing execution, and each edge comes from one row.

## Time

The simulated clock starts at 2000-01-03 (`epoch` in `manifest.json`); day `D` starts `D` days later. A profile's
`epoch` must name its time zone (for example `Z`), so every machine reads it the same. `base/` holds
the first `base_days` days, one file per day, and `stream/` the rest, one file per hour. Each row sits in the file of
its own `event_ts` day and hour.

Within a file, rows are in `id` order. That is the order rows were emitted, not necessarily `event_ts` order.

Rows dated after the scenario's last day are not written; the manifest counts them as `rows_beyond_horizon`.

## Churn

`churn_log.parquet` lists every change to the process, with its simulated time:
- **C1, new instance:** new equipment or a new operator.
- **C2, new type:** a new segment, material, property key or vocabulary variant. Some new segments retire after a week.
  A retired property key does not come back.
- **C3, structural change:** a new input material at a merge, which takes a share of the lots of the material it
  replaces, or a new step inserted in a route.

## Output folder

```
inputs/scenario.json, inputs/profile.json   the exact inputs of this run
base/day=0000.parquet ...                   base load, one file per simulated day
stream/day=0182/hour=06.parquet ...         update stream, one file per simulated hour
churn_log.parquet                           every C1/C2/C3 change
manifest.json                               scenario and profile hashes, seed, counts, planted causes, content_sha256
```
