"""Process simulation, one day at a time.

Inside a day, lots are moved in waves (every lot whose next event falls in the day)
until nothing is due. Lot state lives in a LotStore, one array per field. Every event
increments the output lot's sequence, and uuid = f(name, sequence).
"""

import datetime as dt
import json
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from . import events as ev
from .identity import lot_uuids, segment_instance_id
from .rng import Rng
from .world import TERMINAL, WAIT, Segment, World

US = 1_000_000
SEC_DAY = 86400


def _hex(uuids: list[str], lo: int) -> list[int]:
    """Read eight hex digits of each id as an integer.

    Gives a fixed random draw per segment execution.

    Args:
        uuids: 32-character hex ids.
        lo: Offset of the first digit.

    Returns:
        Integers below 2**32.
    """
    return [int(u[lo : lo + 8], 16) for u in uuids]


def _uniform(uuids: list[str], lo: int) -> list[float]:
    """Turn eight hex digits of each id into a number in [0, 1).

    Args:
        uuids: 32-character hex ids.
        lo: Offset of the first digit.

    Returns:
        Numbers in [0, 1).
    """
    return [k / 2**32 for k in _hex(uuids, lo)]


class LotStore:
    """State of every lot, one array per field.

    Lots are rows. Row indices change when the store is compacted.

    Attributes:
        n: Number of rows in use.
        name: Lot names.
        uuid: Uuid of each lot's current state.
        prev_seg: Segment execution that produced the current state.
        seq: Sequence numbers.
        mat: Material indices.
        step: Route step index, or WAIT or TERMINAL.
        ready: Time the lot is ready for its next step, in seconds since day 0.
        alive: False once the lot has left the process.
        eq: Equipment index of the lot's last event.
        qty: Quantities.
        uses: Uses left on a feeder.
        prod_ts: Time the current state was produced, in seconds since day 0.
        pending: Position of an inserted segment to run first, or -1.
        cause: Bit mask of the planted causes the lot carries.
        born: Day the lot was created.
        loops: Hold-loop cycles left.
        reminted: True if the current state is a re-mint.
        queue: Index into Simulation.queue_keys of the queue the lot waits in, or -1.
    """

    FIELDS = (
        "seq",
        "mat",
        "step",
        "ready",
        "alive",
        "eq",
        "qty",
        "uses",
        "prod_ts",
        "pending",
        "cause",
        "born",
        "loops",
        "reminted",
        "queue",
    )

    def __init__(self, capacity: int = 200_000) -> None:
        """Create an empty store.

        Args:
            capacity: Initial number of rows.
        """
        self.n = 0
        self.name = np.empty(capacity, dtype=object)
        self.uuid = np.empty(capacity, dtype=object)
        self.prev_seg = np.empty(capacity, dtype=object)
        self.seq = np.zeros(capacity, np.int32)
        self.mat = np.zeros(capacity, np.int16)
        self.step = np.zeros(capacity, np.int16)
        self.ready = np.zeros(capacity, np.float64)
        self.alive = np.zeros(capacity, bool)
        self.eq = np.zeros(capacity, np.int32)
        self.qty = np.ones(capacity, np.float64)
        self.uses = np.zeros(capacity, np.int32)
        self.prod_ts = np.zeros(capacity, np.float64)
        self.pending = np.full(capacity, -1, np.int32)
        self.cause = np.zeros(
            capacity, np.int64
        )  # bit k set: the lot carries planted cause k
        self.born = np.zeros(capacity, np.int32)
        self.loops = np.zeros(capacity, np.int32)
        self.reminted = np.zeros(capacity, bool)
        self.queue = np.full(
            capacity, -1, np.int32
        )  # index into Simulation.queue_keys while the lot waits

    def _grow(self, need: int) -> None:
        """Make room for more lots, doubling the arrays when they are full.

        Args:
            need: Number of lots about to be added.
        """
        cap = len(self.seq)
        if self.n + need <= cap:
            return
        new_cap = max(cap * 2, self.n + need)
        for f in ("name", "uuid", "prev_seg"):
            arr = np.empty(new_cap, dtype=object)
            arr[: self.n] = getattr(self, f)[: self.n]
            setattr(self, f, arr)
        for f in self.FIELDS:
            old = getattr(self, f)
            arr = (
                np.zeros(new_cap, old.dtype)
                if f not in ("pending", "queue")
                else np.full(new_cap, -1, np.int32)
            )
            arr[: self.n] = old[: self.n]
            setattr(self, f, arr)

    def add(
        self,
        names: np.ndarray,
        uuids: np.ndarray,
        seq: np.ndarray,
        mat: int,
        step: int,
        ready: np.ndarray,
        qty: np.ndarray | float = 1.0,
        uses: int = 0,
        prod_ts: np.ndarray | None = None,
        prev_seg: np.ndarray | None = None,
        cause: np.ndarray | int = 0,
        born: int = 0,
    ) -> np.ndarray:
        """Add lots.

        Args:
            names: Lot names.
            uuids: State uuids.
            seq: Sequence numbers.
            mat: Material index.
            step: Route step index.
            ready: Ready times, in seconds since day 0.
            qty: Quantities.
            uses: Feeder uses.
            prod_ts: Production times. Defaults to `ready`.
            prev_seg: Producing segment executions. Defaults to null.
            cause: Planted cause bits.
            born: Day the lots were created.

        Returns:
            Row indices of the new lots.
        """
        k = len(names)
        self._grow(k)
        s = slice(self.n, self.n + k)
        (
            self.name[s],
            self.uuid[s],
            self.seq[s],
            self.mat[s],
            self.step[s],
            self.ready[s],
        ) = names, uuids, seq, mat, step, ready
        self.alive[s] = True
        self.qty[s] = qty
        self.uses[s] = uses
        self.prod_ts[s] = ready if prod_ts is None else prod_ts
        self.prev_seg[s] = (
            prev_seg if prev_seg is not None else np.array([None] * k, dtype=object)
        )
        self.pending[s] = -1
        self.cause[s] = cause
        self.born[s] = born
        self.loops[s] = 0
        self.reminted[s] = False
        self.queue[s] = -1
        idx = np.arange(self.n, self.n + k)
        self.n += k
        return idx

    def compact(self) -> None:
        """Drop dead lots to keep the arrays small.

        Does nothing while more than 70% of the lots are alive. Row indices change.
        """
        keep = np.flatnonzero(self.alive[: self.n])
        if len(keep) > 0.7 * self.n:
            return
        for f in ("name", "uuid", "prev_seg", *self.FIELDS):
            arr = getattr(self, f)
            arr[: len(keep)] = arr[keep]
        self.n = len(keep)


class Simulation:
    """Simulate the line one day at a time.

    Attributes:
        sc: Scenario settings.
        rng: Random streams.
        world: Process template.
        lots: Lot state.
        queues: Waiting lot indices per queue key: (merge segment, material),
            ("split", segment) or ("consumable", segment).
        queue_keys: Every queue key seen, in order.
        epoch_us: Start of day 0, in microseconds since 1970.
        next_id: Id of the next row.
        day: Current day.
        counts: Event and row counts by kind.
        planted: Planted causes.
        remint_count: Number of re-minted keys.
        orphan_lots: Number of legacy lots.
    """

    def __init__(
        self, profile: dict[str, Any], scenario: dict[str, Any], rng: Rng
    ) -> None:
        """Set up the simulation.

        Args:
            profile: Profile.
            scenario: Scenario settings.
            rng: Random streams.
        """
        self.sc, self.rng = scenario, rng
        self.world = World(profile, scenario, rng)
        self.lots = LotStore()
        # queue key -> lot indices; keys: (merge_seg, material), ("split", seg), ("consumable", seg)
        self.queues: dict[tuple[str, str], list[int]] = {}
        self.queue_keys: list[tuple[str, str]] = []
        self.queue_id: dict[tuple[str, str], int] = {}
        self._seg_ids: list[str] = []
        self._seg_pos: dict[str, int] = {}
        self.epoch_us = (
            int(
                dt.datetime.fromisoformat(
                    self.world.t["epoch"].replace("Z", "+00:00")
                ).timestamp()
            )
            * US
        )
        self.next_id = 1
        self.day = 0
        self._uses: dict[tuple[object, ...], int] = {}  # stream name -> uses today
        self.counts = {
            "events": 0,
            "raw_rows": 0,
            "creation": 0,
            "transformation": 0,
            "sequence_bump": 0,
            "disposal": 0,
        }
        self.planted: list[
            dict[str, Any]
        ] = []  # planted causes: segment, equipment, day, failure rates, outcome step
        self.remint_count = 0
        self.orphan_lots = 0

    # ---- helpers -------------------------------------------------------------------------------------
    def r(self, *k: object) -> np.random.Generator:
        """Return a new random generator for the current day and a key.

        Each call with the same key on the same day gets different numbers.

        Args:
            *k: Key parts.

        Returns:
            Random generator.
        """
        n = self._uses.get(k, 0)
        self._uses[k] = n + 1
        return self.rng.stream(self.day, *k, n)

    def ramp(self, day: int) -> float:
        """Return the production ramp's volume factor for a day.

        Args:
            day: Day number.

        Returns:
            1.0 on day 0, rising in a straight line to ramp_last_over_first on the
            last day.
        """
        n = max(1, self.sc["days"] - 1)
        return 1.0 + (self.sc["volume"]["ramp_last_over_first"] - 1.0) * day / n

    def _segs(self) -> None:
        """Refresh the segment index after segments were added."""
        if len(self._seg_ids) != len(
            self.world.segments
        ):  # segments are only ever added
            self._seg_ids = list(self.world.segments)
            self._seg_pos = {sid: i for i, sid in enumerate(self._seg_ids)}

    def seg_pos(self, seg_id: str) -> int:
        """Return the position of a segment in the segment index.

        Args:
            seg_id: Segment id.

        Returns:
            Position.
        """
        self._segs()
        return self._seg_pos[seg_id]

    def seg_at(self, i: int) -> str:
        """Return the segment id at a position in the segment index.

        Args:
            i: Position.

        Returns:
            Segment id.
        """
        self._segs()
        return self._seg_ids[i]

    def join_queue(self, idx: np.ndarray | int, key: tuple[str, str]) -> None:
        """Put lots in a queue.

        The queue is also recorded on each lot, so queues can be rebuilt after
        compaction.

        Args:
            idx: Lot indices.
            key: Queue key.
        """
        if key not in self.queue_id:
            self.queue_id[key] = len(self.queue_keys)
            self.queue_keys.append(key)
        self.lots.queue[idx] = self.queue_id[key]
        self.queues.setdefault(key, []).extend(int(i) for i in np.atleast_1d(idx))

    def route(self, mat_id: str) -> list[dict[str, Any]]:
        """Return the route steps of a material.

        Args:
            mat_id: Material id.

        Returns:
            Route steps. Empty if the material has no route.
        """
        return self.world.routes.get(mat_id, [])

    def to_us(self, secs: np.ndarray) -> np.ndarray:
        """Convert times in seconds since day 0 to microseconds since 1970.

        Args:
            secs: Times in seconds.

        Returns:
            Times in microseconds.
        """
        return (np.asarray(secs) * US).astype(np.int64) + self.epoch_us

    # ---- lot creation ----------------------------------------------------------------------------------
    def create_sources(self, day: int, buf: ev.EventBuffer) -> None:
        """Create the day's new raw lots.

        Args:
            day: Day number.
            buf: Event buffer.
        """
        w = self.world
        factor = self.sc["volume"]["scale"] * self.ramp(day) * w.weekday_factor[day % 7]
        r = self.r("sources")
        lo, hi = w.shift
        for src in w.sources:
            if src.get("born_day", 0) > day:
                continue
            n = int(r.poisson(src["per_day"] * factor))
            if n == 0:
                continue
            mat = src["material"]
            names = w.pool_for(mat).take(n)
            seq = np.ones(n, np.int32)
            uuids = lot_uuids(names, seq)
            t = day * SEC_DAY + r.uniform(lo * 3600, hi * 3600, n)
            seg = w.segments[src["segment"]]
            qty = float(src["qty"]) * np.exp(0.05 * r.standard_normal(n))
            regime = w.regime_of[mat]
            qty = (
                np.round(qty, 6) if regime == "mass" else np.round(qty)
            )  # rounded so CPUs agree on every byte
            idx = self.lots.add(
                names,
                uuids,
                seq,
                w.mat_index[mat],
                0,
                t + w.gap_seconds(w.segments[self.route(mat)[0]["seg"]], r, n)
                if self.route(mat)
                else t,
                qty=qty,
                uses=0,
                prod_ts=t,
                born=day,
            )
            self.lots.eq[idx] = r.integers(0, len(seg.equipment), n)
            self.mark_cohort_cause(idx, seg, t)
            seg_uuid = [segment_instance_id("create", u) for u in uuids]
            self.lots.prev_seg[idx] = np.array(seg_uuid, dtype=object)
            self.emit(
                buf,
                seg,
                begin=t,
                end=t,
                in_idx=None,
                out_names=names,
                out_seq=seq,
                out_uuid=uuids,
                out_mat=mat,
                qty_in=None,
                qty_out=qty,
                qty_net=qty,
                unit=w.unit_mass if regime == "mass" else None,
                seg_uuid=seg_uuid,
                eq_idx=self.lots.eq[idx],
            )
            self.counts["creation"] += n

    # ---- event emission -------------------------------------------------------------------------------
    def emit(
        self,
        buf: ev.EventBuffer,
        seg: Segment,
        begin: np.ndarray,
        end: np.ndarray,
        in_idx: np.ndarray | None,
        out_names: np.ndarray | list[str] | None,
        out_seq: np.ndarray | None,
        out_uuid: np.ndarray | None,
        out_mat: str | None,
        qty_in: np.ndarray | None,
        qty_out: np.ndarray | None,
        qty_net: np.ndarray | float | None,
        unit: str | None,
        seg_uuid: list[str],
        eq_idx: np.ndarray,
        disposition: list[str | None] | None = None,
        in_override: dict[str, Any] | None = None,
        in_status: str | None = None,
        out_status: str | None = None,
    ) -> list[str | None]:
        """Add events for one segment to the buffer.

        Args:
            buf: Event buffer.
            seg: Segment.
            begin: Start times, in seconds since day 0.
            end: End times, in seconds since day 0.
            in_idx: Indices of the input lots, or None for creation events.
            out_names: Output lot names, or None for disposal events.
            out_seq: Output sequence numbers.
            out_uuid: Output state uuids.
            out_mat: Output material.
            qty_in: Input quantities.
            qty_out: Output quantities.
            qty_net: Net quantities.
            unit: Quantity unit, or None.
            seg_uuid: Segment execution ids.
            eq_idx: Equipment indices.
            disposition: Disposition of each event, or None.
            in_override: Input columns ("name", "seq", "uuid", "mat", "prev") to use
                instead of the input lots' own, for splits and dispensing.
            in_status: Input status. Defaults to normal.
            out_status: Output status. Defaults to scrapped for a scrap disposition,
                and normal otherwise.

        Returns:
            Dispositions as written, with the lower-case variant applied.
        """
        w = self.world
        n = len(begin)
        r = self.r("emit", seg.id, self.counts["events"])
        L = self.lots
        # a row is never dated before the day it is emitted in, so each day's partition is final once the day is simulated
        begin = np.maximum(np.asarray(begin, dtype=np.float64), self.day * SEC_DAY)
        end = np.maximum(np.asarray(end, dtype=np.float64), begin)
        if in_idx is not None:
            in_name, in_seq, in_uuid = L.name[in_idx], L.seq[in_idx], L.uuid[in_idx]
            in_mat = [w.mat_list[m] for m in L.mat[in_idx]]
            in_prev = L.prev_seg[in_idx].copy()
        else:
            in_name = in_seq = in_uuid = in_prev = None
            in_mat = None
        if in_override:
            in_name = in_override.get("name", in_name)
            in_seq = in_override.get("seq", in_seq)
            in_uuid = in_override.get("uuid", in_uuid)
            in_mat = in_override.get("mat", in_mat)
            in_prev = in_override.get("prev", in_prev)
        if in_prev is not None and self.sc["anomalies"]["enabled"]:
            null_mask = r.random(n) < w.anomalies["prevseg_null"]
            in_prev = np.where(null_mask, None, in_prev)
        eq_paths = [seg.equipment[i % len(seg.equipment)] for i in np.asarray(eq_idx)]
        locs = [w.location_of(e) for e in eq_paths]
        # operator and recipe belong to the segment execution: every row of one merge or dispense carries the same values
        ops = (
            [
                None if f > w.operator_fill else seg.operators[k % len(seg.operators)]
                for f, k in zip(_uniform(seg_uuid, 0), _hex(seg_uuid, 8))
            ]
            if seg.operators
            else [None] * n
        )
        op_ids = [segment_instance_id("op", u) for u in seg_uuid]
        disp, disp_codes = self.disposition_fields(n, disposition, r)
        in_props = (
            self.properties(seg.in_keys, n, r, eq_paths, "in")
            if in_idx is not None or in_override
            else [None] * n
        )
        out_props = self.properties(seg.out_keys, n, r, eq_paths, "out")
        recipe = self.recipe_json(seg, seg_uuid) if seg.recipe else [None] * n
        ext = (
            [
                w.new_ext_ref() if out_mat in w.ext_ref_materials else None
                for _ in range(n)
            ]
            if out_names is not None and out_mat in w.ext_ref_materials
            else [None] * n
        )
        begin_us, end_us = self.to_us(begin), self.to_us(end)
        buf.extend(
            event_ts=begin_us,
            begin_ts=begin_us,
            end_ts=end_us,
            segment_name=seg.id,
            segment_uuid=list(seg_uuid),
            operation_id=op_ids,
            in_lot_name=None if in_name is None else list(in_name),
            in_lot_sequence=None if in_seq is None else np.asarray(in_seq, np.int64),
            in_lot_uuid=None if in_uuid is None else list(in_uuid),
            in_material=in_mat,
            in_lot_status=None if in_name is None else (in_status or w.status_ok),
            in_prev_segment=None if in_prev is None else list(in_prev),
            out_lot_name=None if out_names is None else list(out_names),
            out_lot_sequence=None if out_seq is None else np.asarray(out_seq, np.int64),
            out_lot_uuid=None if out_uuid is None else list(out_uuid),
            out_material=out_mat if out_names is not None else None,
            out_lot_status=None
            if out_names is None
            else (
                [out_status] * n
                if out_status
                else [
                    w.status_scrap
                    if d in (w.disp_scrap, w.disp_variant)
                    else w.status_ok
                    for d in disp
                ]
            ),
            location=locs,
            location_type=seg.loc_type,
            equipment_path=eq_paths,
            operator=ops,
            work_order_id=None,
            disposition=disp,
            disposition_codes=disp_codes,
            ext_system_ref=ext,
            qty_in=qty_in,
            qty_out=qty_out,
            qty_net=qty_net,
            qty_available=qty_out,
            qty_scheduled=0.0 if qty_out is not None else None,
            unit=unit,
            in_properties=in_props,
            out_properties=out_props,
            recipe_json=recipe,
        )
        self.counts["events"] += n
        return disp

    def disposition_fields(
        self, n: int, disposition: list[str | None] | None, r: np.random.Generator
    ) -> tuple[list[str | None], list[str | None]]:
        """Build the disposition and disposition-code columns.

        From the variant day on, some scrap dispositions use the lower-case spelling.

        Args:
            n: Number of events.
            disposition: Disposition of each event, or None.
            r: Random generator.

        Returns:
            Dispositions and disposition codes (JSON strings), one of each per event.
        """
        w = self.world
        if disposition is None:
            return [None] * n, [None] * n
        disp = list(disposition)
        codes: list[str | None] = [None] * n
        variant = self.day >= w.case_variant_from_day
        for i, d in enumerate(disp):
            if d is None:
                continue
            if d == w.disp_scrap and variant and r.random() < w.case_variant_share:
                disp[i] = w.disp_variant
            code = w.codes[r.choice(len(w.codes), p=w.code_weights)]
            obj = {
                w.disp_keys[0]: code,
                w.disp_keys[1]: w.processes[int(r.integers(len(w.processes)))],
                w.disp_keys[2]: disp[i],
            }
            if r.random() < 0.08:
                obj[w.disp_keys[3]] = w.codes[r.choice(len(w.codes), p=w.code_weights)]
                if r.random() < 0.25:
                    obj[w.disp_keys[4]] = w.codes[
                        r.choice(len(w.codes), p=w.code_weights)
                    ]
            codes[i] = json.dumps(obj, separators=(", ", ": "))
        return disp, codes

    def properties(
        self,
        keys: list[str],
        n: int,
        r: np.random.Generator,
        eq_paths: list[str],
        side: str,
    ) -> list[str]:
        """Build the property JSON of each event.

        Args:
            keys: Property keys.
            n: Number of events.
            r: Random generator.
            eq_paths: Equipment path of each event.
            side: "in" for input properties, "out" for output properties.

        Returns:
            JSON object strings, one per event.
        """
        if not keys or n == 0:
            return ["{}"] * n
        w = self.world
        empty = r.random(n) < (0.06 if side == "in" else 0.08)
        values = []
        tray_base = int(r.integers(1, 10_000_000))
        for k in keys:
            kind, alt = w.prop_pool[k]
            use_alt = alt and r.random() < 0.1
            if kind == "tray":
                v = ev.enc_str([f"TR{(tray_base + i // 20):010d}" for i in range(n)])
            elif kind == "pos":
                ints = r.integers(1, 25, n)
                v = ev.enc_str(ints) if use_alt else ev.enc_int(ints)
            elif kind == "code":
                v = (
                    ev.enc_str([f"V{x}" for x in r.integers(0, 500, n)])
                    if not use_alt
                    else ev.enc_int(r.integers(0, 500, n))
                )
            elif kind == "flag":
                v = (
                    ev.enc_bool(r.random(n) < 0.5)
                    if not use_alt
                    else ev.enc_str(["Y" if x else "N" for x in r.random(n) < 0.5])
                )
            elif kind == "num":
                v = (
                    [f"{x:.3f}" for x in r.normal(50, 8, n)]
                    if not use_alt
                    else ev.enc_str([f"{x:.3f}" for x in r.normal(50, 8, n)])
                )
            else:  # group
                v = ev.enc_str(
                    [
                        f"SG_{eq_paths[i][:4]}_{int(x)}"
                        for i, x in enumerate(r.integers(1, 6, n))
                    ]
                )
            nulls = r.random(n) < w.t["properties"]["null_share"] * 0.5
            v = ["null" if z else x for x, z in zip(v, nulls)]
            values.append(v)
        out = ev.json_object(keys, values)
        return ["{}" if e else o for o, e in zip(out, empty)]

    def recipe_json(self, seg: Segment, seg_uuid: list[str]) -> list[str | None]:
        """Build the recipe JSON of each event.

        Every row of one segment execution gets the same value. About 30% of the
        executions have no recipe, and a few write the version and one flag as
        strings.

        Args:
            seg: Segment. Must run a recipe.
            seg_uuid: Segment execution ids.

        Returns:
            JSON strings or None, one per event.
        """
        w = self.world
        version = 1 + self.day // 45
        k = w.recipe_keys
        base = {
            k[0]: seg.recipe,
            k[1]: version,
            k[2]: f"RCP_{seg.recipe:03d}",
            k[3]: True,
            k[4]: f"SR_{seg.recipe:03d}_{seg.id[-3:]}",
            k[5]: f"SRN_{seg.recipe:03d}",
            k[6]: False,
        }
        s = json.dumps(base, separators=(", ", ": "))
        alt = json.dumps(
            dict(base, **{k[1]: str(version), k[6]: "false"}), separators=(", ", ": ")
        )
        return [
            None if x < 0.30 else (alt if x < 0.36 else s)
            for x in _uniform(seg_uuid, 16)
        ]

    # ---- one wave of step processing -----------------------------------------------------------------------
    def process_wave(self, day: int, buf: ev.EventBuffer) -> int:
        """Move every lot whose next event falls in the day by one step.

        Args:
            day: Day number.
            buf: Event buffer.

        Returns:
            Number of lots that were due.

        Raises:
            ValueError: If a route step has an unknown kind.
        """
        L, w = self.lots, self.world
        day_end = (day + 1) * SEC_DAY
        n = L.n
        due = np.flatnonzero(L.alive[:n] & (L.step[:n] >= 0) & (L.ready[:n] < day_end))
        if len(due) == 0:
            return 0
        # one group per (material, step, pending segment): process_pending runs a whole group on one segment
        key = (
            L.mat[due].astype(np.int64) * 10_000
            + L.step[due].astype(np.int64)
            + (L.pending[due].astype(np.int64) + 1) * 1_000_000_000
        )
        order = np.argsort(key, kind="stable")
        due, key = due[order], key[order]
        bounds = np.flatnonzero(np.diff(key)) + 1
        for grp in np.split(due, bounds):
            mat_id = w.mat_list[L.mat[grp[0]]]
            if L.pending[grp[0]] >= 0:
                self.process_pending(grp, mat_id, buf)
                continue
            steps = self.route(mat_id)
            si = int(L.step[grp[0]])
            if si >= len(steps):
                L.alive[grp] = False
                L.step[grp] = TERMINAL
                continue
            st = steps[si]
            kind = st["kind"]
            if kind in ("step", "store_in", "store_out", "rename"):
                self.process_step(grp, mat_id, si, st, buf)
            elif kind == "consume":
                self.enqueue(grp, mat_id, st)
            elif kind == "split":
                self.process_split(grp, st, buf)
            elif kind == "dispense":
                self.process_dispense(grp, mat_id, si, st, buf)
            elif kind == "consumable":
                L.step[grp] = WAIT
                L.uses[grp] = np.maximum(
                    1,
                    np.round(
                        w.consumables[st["seg"]]["uses_median"]
                        * np.exp(
                            w.consumables[st["seg"]]["uses_sigma"]
                            * self.r("uses", st["seg"]).standard_normal(len(grp))
                        )
                    ),
                ).astype(np.int32)
                self.join_queue(grp, ("consumable", st["seg"]))
            else:
                raise ValueError(kind)
        return len(due)

    def schedule_next(
        self,
        idx: np.ndarray,
        mat_id: str,
        next_step: int,
        t_end: np.ndarray,
        r: np.random.Generator,
    ) -> None:
        """Send lots on to a step of their route.

        Lots past the end of the route leave the process. Some lots first go through
        an inserted step or a hold loop.

        Args:
            idx: Lot indices.
            mat_id: Material id.
            next_step: Index of the next route step.
            t_end: End time of each lot's last event, in seconds since day 0.
            r: Random generator.
        """
        L, w = self.lots, self.world
        steps = self.route(mat_id)
        if len(idx) == 0:
            return
        L.mat[idx] = w.mat_index[mat_id]
        L.step[idx] = next_step
        if next_step >= len(steps):
            L.alive[idx] = False
            L.step[idx] = TERMINAL
            return
        seg = w.segments[steps[next_step]["seg"]]
        L.ready[idx] = self.calendar(t_end + w.gap_seconds(seg, r, len(idx)), seg, r)
        prev_seg_id = steps[next_step - 1]["seg"] if next_step > 0 else None
        for ins in w.inserted_steps:
            if (
                ins["material"] != mat_id
                or ins["after"] != prev_seg_id
                or ins["start"] > self.day
                or (ins["end"] is not None and ins["end"] <= self.day)
            ):
                continue
            pick = idx[r.random(len(idx)) < ins["share"]]
            if len(pick):
                L.pending[pick] = self.seg_pos(ins["seg"])
                L.ready[pick] = t_end[np.isin(idx, pick)] + w.gap_seconds(
                    w.segments[ins["seg"]], r, len(pick)
                )
        hold = w.hold
        if hold and mat_id in hold["materials"]:
            pick = idx[
                (r.random(len(idx)) < hold["rate_per_step"]) & (L.pending[idx] < 0)
            ]
            if len(pick):
                L.loops[pick] = np.maximum(
                    2,
                    np.round(
                        hold["cycles_median"]
                        * np.exp(hold["cycles_sigma"] * r.standard_normal(len(pick)))
                    ),
                ).astype(np.int32)
                L.pending[pick] = self.seg_pos(hold["segments"][0])
                L.ready[pick] = t_end[np.isin(idx, pick)] + self.hold_gap(r, len(pick))

    def hold_gap(self, r: np.random.Generator, n: int) -> np.ndarray:
        """Draw waits between hold-loop cycles, in whole seconds.

        Uses the first hold segment's gap when the profile sets no `gap_min`.

        Args:
            r: Random generator.
            n: Number of waits.

        Returns:
            Waits in seconds.
        """
        hold = self.world.hold
        if hold.get("gap_min") is None:
            return self.world.gap_seconds(
                self.world.segments[hold["segments"][0]], r, n
            )
        return np.rint(
            60.0 * hold["gap_min"] * np.exp(0.5 * r.standard_normal(n))
        )  # whole seconds, as World.gap_seconds

    def calendar(
        self, ready: np.ndarray, seg: Segment, r: np.random.Generator
    ) -> np.ndarray:
        """Apply the weekly calendar to ready times.

        Most weekend work waits until Monday morning, and batch-release segments only
        start at fixed hours.

        Args:
            ready: Ready times, in seconds since day 0.
            seg: Segment of the next step.
            r: Random generator.

        Returns:
            Adjusted ready times.
        """
        w = self.world
        ready = np.asarray(ready, dtype=np.float64).copy()
        if w.weekend_defer > 0:
            dow = (ready // SEC_DAY).astype(np.int64) % 7  # epoch is a Monday
            weekend = (dow >= 5) & (r.random(len(ready)) < w.weekend_defer)
            if weekend.any():
                monday = (ready[weekend] // SEC_DAY + (7 - dow[weekend])) * SEC_DAY
                ready[weekend] = (
                    monday + 6 * 3600 + r.uniform(0, 4 * 3600, int(weekend.sum()))
                )
        br = w.batch_release
        if br and seg.id in br["segments"]:
            slots = np.array(sorted(br["slot_hours"])) * 3600.0
            day_start = (ready // SEC_DAY) * SEC_DAY
            tod = ready - day_start
            nxt = np.full(len(ready), np.nan)
            for sl in slots[::-1]:
                nxt = np.where(tod <= sl, sl, nxt)
            ready = np.where(
                np.isnan(nxt), day_start + SEC_DAY + slots[0], day_start + nxt
            ) + r.uniform(0, br["jitter_min"] * 60, len(ready))
        return ready

    def process_pending(
        self, grp: np.ndarray, mat_id: str, buf: ev.EventBuffer
    ) -> None:
        """Run lots through an inserted step or hold loop before their planned step.

        Args:
            grp: Lot indices, all waiting for the same segment.
            mat_id: Material id.
            buf: Event buffer.
        """
        L, w = self.lots, self.world
        seg = w.segments[self.seg_at(int(L.pending[grp[0]]))]
        if seg.retire_day is not None:
            # the step retired before these lots reached it: they go on to their planned step
            gone = (
                np.maximum(L.ready[grp], self.day * SEC_DAY) >= seg.retire_day * SEC_DAY
            )
            L.pending[grp[gone]] = -1
            grp = grp[~gone]
            if len(grp) == 0:
                return
        r = self.r("pending", seg.id)
        t0 = L.ready[grp]
        t1 = t0 + w.duration_seconds(seg, r, len(grp))
        self.bump(grp, seg, t0, t1, mat_id, buf, r, new_mat=None)
        looping = L.loops[grp] > 0
        if looping.any() and w.hold:
            # keep cycling between the two hold segments until the loop budget is spent
            lp = grp[looping]
            L.loops[lp] -= 1
            alt = (
                w.hold["segments"][1]
                if seg.id == w.hold["segments"][0]
                else w.hold["segments"][0]
            )
            L.pending[lp] = self.seg_pos(alt)
            L.ready[lp] = t1[looping] + self.hold_gap(r, len(lp))
        done = grp[~looping]
        if len(done):
            L.pending[done] = -1
            steps = self.route(mat_id)
            si = int(L.step[done[0]])
            seg_next = w.segments[steps[si]["seg"]] if si < len(steps) else None
            L.ready[done] = t1[~looping] + (
                w.gap_seconds(seg_next, r, len(done)) if seg_next else 0
            )

    def bump(
        self,
        grp: np.ndarray,
        seg: Segment,
        t0: np.ndarray,
        t1: np.ndarray,
        mat_id: str,
        buf: ev.EventBuffer,
        r: np.random.Generator,
        new_mat: str | None = None,
        disposition: list[str | None] | None = None,
        rename_to: str | None = None,
        in_status: str | None = None,
        out_status: str | None = None,
        length_per_mass: float | None = None,
    ) -> list[str | None]:
        """Record one event per lot that moves it to its next state.

        A lot keeps its name and its sequence goes up by one, unless it is renamed.
        A few states are re-minted: same sequence, second uuid. The lots are updated
        in place.

        Args:
            grp: Lot indices.
            seg: Segment.
            t0: Start times, in seconds since day 0.
            t1: End times, in seconds since day 0.
            mat_id: Material id of the lots.
            buf: Event buffer.
            r: Random generator.
            new_mat: New material, or None.
            disposition: Disposition of each lot, or None.
            rename_to: Material whose name pool gives the lots new names, or None.
            in_status: Input status, or None for normal.
            out_status: Output status, or None to derive it from the disposition.
            length_per_mass: Roll length per mass unit, when mass becomes a roll.

        Returns:
            Dispositions as written.
        """
        L, w = self.lots, self.world
        n = len(grp)
        anomalies = self.sc["anomalies"]["enabled"]
        # a re-mint keeps the sequence and takes a variant uuid; a second re-mint would reproduce that uuid (self-loop)
        remint = anomalies and (r.random(n) < w.anomalies["remint"]) & ~L.reminted[grp]
        if rename_to:
            out_names = w.pool_for(rename_to).take(n)
            out_seq = np.ones(n, np.int32)
        else:
            out_names = L.name[grp]
            out_seq = np.where(remint, L.seq[grp], L.seq[grp] + 1).astype(np.int32)
        out_mat = rename_to or new_mat or mat_id
        out_uuid = lot_uuids(out_names, out_seq)
        if anomalies and remint.any():
            out_uuid[remint] = lot_uuids(
                out_names[remint], out_seq[remint], variant="b"
            )
            self.remint_count += int(remint.sum())
        seg_uuid = [segment_instance_id("ev", u) for u in out_uuid]
        eq_idx = r.integers(0, len(seg.equipment), n)
        if anomalies:
            # dated before its producer, but never before the day it is emitted in
            skew = r.random(n) < w.anomalies["time_order"]
            skewed = L.prod_ts[grp] - r.uniform(60, 3600, n)
            skew &= skewed >= self.day * SEC_DAY
            t0 = np.where(skew, skewed, t0)
            t1 = np.maximum(t1, t0)
        qty = L.qty[grp]
        regime = w.regime_of[out_mat]
        qty_out = qty
        if w.regime_of[mat_id] == "mass" and regime == "roll":
            qty_out = np.rint(qty * length_per_mass)
        disp = self.emit(
            buf,
            seg,
            t0,
            t1,
            grp,
            out_names,
            out_seq,
            out_uuid,
            out_mat,
            qty_in=qty,
            qty_out=qty_out
            if regime != "discrete"
            else np.where(r.random(n) < 0.5, 1.0, 0.0),
            qty_net=qty_out if regime != "discrete" else 0.0,
            unit=w.unit_mass if regime == "mass" else None,
            seg_uuid=seg_uuid,
            eq_idx=eq_idx,
            disposition=disposition,
            in_status=in_status,
            out_status=out_status,
        )
        L.name[grp], L.seq[grp], L.uuid[grp] = out_names, out_seq, out_uuid
        L.reminted[grp] = remint if anomalies else False
        L.prev_seg[grp] = np.array(seg_uuid, dtype=object)
        L.prod_ts[grp] = t0
        L.eq[grp] = eq_idx
        L.mat[grp] = w.mat_index[out_mat]
        if regime != "discrete":
            L.qty[grp] = qty_out
        self.mark_cohort_cause(grp, seg, t0)
        self.counts["transformation" if rename_to else "sequence_bump"] += n
        return disp

    def process_step(
        self,
        grp: np.ndarray,
        mat_id: str,
        si: int,
        st: dict[str, Any],
        buf: ev.EventBuffer,
    ) -> None:
        """Run lots through an ordinary route step.

        Decides which lots are scrapped (planted failures included), used as is or
        reworked, and sends each lot on.

        Args:
            grp: Lot indices.
            mat_id: Material id.
            si: Index of the step in the route.
            st: Route step.
            buf: Event buffer.
        """
        L, w = self.lots, self.world
        seg = w.segments[st["seg"]]
        r = self.r("step", seg.id, si)
        n = len(grp)
        t0 = L.ready[grp]
        t1 = t0 + w.duration_seconds(seg, r, n)
        scrap_p = st.get("scrap", seg.scrap)
        useasis_p = st.get("useasis", seg.useasis)
        u = r.random(n)
        scrapped = (u < scrap_p) | self.cohort_failures(grp, seg)
        # the step itself records only use-as-is: a scrapped lot gets its own event at the scrap step (scrap_lots)
        disposition = np.where(
            ~scrapped & (u < scrap_p + useasis_p), w.disp_useasis, None
        ).astype(object)
        rename_to = st.get("to") if st["kind"] == "rename" else None
        new_mat = st.get("to") if st["kind"] != "rename" else None
        # rework: return to an earlier material/step with the same name; the step's event already writes that material
        rework = np.zeros(n, bool)
        if st.get("rework_to") and self.sc["rework_rate"] > 0:
            rework = (~scrapped) & (r.random(n) < self.sc["rework_rate"])
        back_mat, back_step = (st.get("rework_to") or ":0").split(":")
        for part, to_mat, rename in (
            (~rework, new_mat, rename_to),
            (rework, back_mat, None),
        ):
            if part.any():
                self.bump(
                    grp[part],
                    seg,
                    t0[part],
                    t1[part],
                    mat_id,
                    buf,
                    r,
                    new_mat=to_mat,
                    disposition=list(disposition[part]),
                    rename_to=rename,
                    length_per_mass=st.get("length_per_mass"),
                )
        next_mat = rename_to or new_mat or mat_id
        cont = grp[~scrapped & ~rework]
        next_step = 0 if (rename_to or new_mat) else si + 1
        self.schedule_next(cont, next_mat, next_step, t1[~scrapped & ~rework], r)
        if rework.any():
            self.schedule_next(grp[rework], back_mat, int(back_step), t1[rework], r)
        if scrapped.any():
            self.scrap_lots(grp[scrapped], next_mat, next_step, t1[scrapped], buf, r)

    def scrap_lots(
        self,
        idx: np.ndarray,
        mat_id: str,
        resume_step: int,
        t_end: np.ndarray,
        buf: ev.EventBuffer,
        r: np.random.Generator,
    ) -> None:
        """Scrap lots.

        Each lot passes the scrap step, keeps its name and becomes scrapped. Most stop
        there. Some are stored, taken out later and disposed of. A few are recovered
        and continue their route.

        Args:
            idx: Lot indices.
            mat_id: Material id.
            resume_step: Route step a recovered lot continues from.
            t_end: End time of each lot's last event, in seconds since day 0.
            buf: Event buffer.
            r: Random generator.
        """
        L, w = self.lots, self.world
        s = w.scrap
        n = len(idx)
        seg = w.segments[s["segment"]]
        t = t_end + w.gap_seconds(seg, r, n)
        coded = r.random(n) < s["coded_share"]
        self.bump(
            idx,
            seg,
            t,
            t,
            mat_id,
            buf,
            r,
            disposition=[w.disp_scrap if c else None for c in coded],
            in_status=w.status_ok,
            out_status=w.status_scrap,
        )
        fate = r.random(n)
        store = fate < s["store_share"]
        recover = ~store & (fate < s["store_share"] + s["recover_share"])
        if store.any():
            k, tk = idx[store], t[store]
            for sid in s[
                "store_segments"
            ]:  # check-in, then check-out after the storage dwell
                sg = w.segments[sid]
                tk = tk + w.gap_seconds(sg, r, len(k))
                self.bump(
                    k,
                    sg,
                    tk,
                    tk,
                    mat_id,
                    buf,
                    r,
                    in_status=w.status_scrap,
                    out_status=w.status_scrap,
                )
            self.emit_disposal(k, w.segments[s["disposal_segment"]], tk, buf, r)
        if recover.any():
            k, sg = idx[recover], w.segments[s["recover_segment"]]
            tk = t[recover] + w.gap_seconds(sg, r, len(k))
            self.bump(
                k,
                sg,
                tk,
                tk,
                mat_id,
                buf,
                r,
                in_status=w.status_scrap,
                out_status=w.status_ok,
            )
            self.schedule_next(k, mat_id, resume_step, tk, r)
        stop = idx[~recover]
        L.alive[stop] = False
        L.step[stop] = TERMINAL

    def emit_disposal(
        self,
        idx: np.ndarray,
        seg: Segment,
        t: np.ndarray,
        buf: ev.EventBuffer,
        r: np.random.Generator,
    ) -> None:
        """Record disposal events. Each consumes a lot and has no output lot.

        Args:
            idx: Lot indices.
            seg: Disposal segment.
            t: Time each lot left storage, in seconds since day 0.
            buf: Event buffer.
            r: Random generator.
        """
        L, w = self.lots, self.world
        n = len(idx)
        seg_uuid = [segment_instance_id("disp", u) for u in L.uuid[idx]]
        t0 = t + w.gap_seconds(seg, r, n)
        self.emit(
            buf,
            seg,
            t0,
            t0,
            idx,
            None,
            None,
            None,
            None,
            qty_in=L.qty[idx],
            qty_out=None,
            qty_net=None,
            unit=None,
            seg_uuid=seg_uuid,
            eq_idx=r.integers(0, len(seg.equipment), n),
            in_status=w.status_scrap,
        )
        self.counts["disposal"] += n

    def mark_cohort_cause(
        self, grp: np.ndarray, seg: Segment, t: np.ndarray | float
    ) -> None:
        """Mark lots just processed on a planted cause's equipment, from its day on.

        Args:
            grp: Lot indices.
            seg: Segment the lots were processed on.
            t: Start time of each lot's event, in seconds since day 0.
        """
        L = self.lots
        # as in emit: an event is never dated before the day that emits it
        t = np.maximum(np.broadcast_to(t, len(grp)), self.day * SEC_DAY)
        for c in self.planted:
            if c["segment"] == seg.id:
                hit = (L.eq[grp] % len(seg.equipment) == c["eq_idx"]) & (
                    t >= c["day"] * SEC_DAY
                )
                L.cause[grp[hit]] |= 1 << c["id"]

    def cohort_failures(self, grp: np.ndarray, seg: Segment) -> np.ndarray:
        """Decide which lots fail at a planted cause's test step.

        Lots with the cause fail at rate `effect`, the others at `background`.

        Args:
            grp: Lot indices.
            seg: Segment being processed.

        Returns:
            True for each lot that fails.
        """
        L = self.lots
        fail = np.zeros(len(grp), bool)
        for c in self.planted:
            if c["outcome_segment"] == seg.id and c["day"] <= self.day:
                r = self.r("cohort", seg.id, c["id"])
                fail |= r.random(len(grp)) < np.where(
                    L.cause[grp] & (1 << c["id"]), c["effect"], c["background"]
                )
        return fail

    # ---- merges ----------------------------------------------------------------------------------------------
    def enqueue(self, grp: np.ndarray, mat_id: str, st: dict[str, Any]) -> None:
        """Queue lots for a merge or a split.

        Args:
            grp: Lot indices.
            mat_id: Material id.
            st: Route step. Its "alt" setting sends a share of the lots elsewhere.
        """
        L, w = self.lots, self.world
        r = self.r("enqueue", st["seg"], mat_id)
        targets = np.array([st["seg"]] * len(grp), dtype=object)
        for alt_seg, share in (st.get("alt") or {}).items():
            pick = r.random(len(grp)) < share
            targets[pick] = alt_seg
        L.step[grp] = WAIT
        for i, tgt in zip(grp, targets):
            self.join_queue(
                i, ("split", tgt) if tgt in w.splits else (tgt, mat_id)
            )  # a split target is a disassembly branch

    def process_merges(self, day: int, buf: ev.EventBuffer) -> int:
        """Form as many merges as the queued lots allow.

        Args:
            day: Day number.
            buf: Event buffer.

        Returns:
            Number of merged lots made.
        """
        L, w = self.lots, self.world
        day_end = (day + 1) * SEC_DAY
        formed = 0
        for seg_id, m in w.merges.items():
            seg = w.segments[seg_id]
            r = self.r("merge", seg_id)
            needs = {}
            for mat, k in m["inputs"].items():
                alts = [mat] + m.get("alternatives", {}).get(mat, [])
                needs[mat] = (k, alts)
            # available lots per input slot, sorted by ready time
            avail = {}
            for mat, (k, alts) in needs.items():
                idx = np.array(
                    sorted(
                        {
                            i
                            for a in alts
                            for i in self.queues.get((seg_id, a), [])
                            if L.alive[i] and L.ready[i] < day_end
                        },
                        key=lambda i: L.ready[i],
                    ),
                    dtype=np.int64,
                )
                avail[mat] = idx
            groups = min(len(avail[mat]) // k for mat, (k, _) in needs.items())
            if groups == 0:
                continue
            out_mat = m["output"]
            out_names = w.pool_for(out_mat).take(groups)
            out_seq = np.ones(groups, np.int32)
            out_uuid = lot_uuids(out_names, out_seq)
            seg_uuid = [segment_instance_id("merge", u) for u in out_uuid]
            eq_idx = r.integers(0, len(seg.equipment), groups)
            used = []
            in_rows, qty_sum, ready_max, cause = (
                [],
                np.zeros(groups),
                np.zeros(groups),
                np.zeros(groups, np.int64),
            )
            for mat, (k, _) in needs.items():
                take = avail[mat][: groups * k].reshape(groups, k)
                used.extend(take.ravel().tolist())
                qty_sum += L.qty[take].sum(axis=1)
                ready_max = np.maximum(ready_max, L.ready[take].max(axis=1))
                cause |= np.bitwise_or.reduce(L.cause[take], axis=1)
                for j in range(k):
                    in_rows.append(take[:, j])
            t0 = ready_max + 60 * r.uniform(1, 30, groups)
            t1 = t0 + w.duration_seconds(seg, r, groups)
            regime = w.regime_of[out_mat]
            qty_out = (
                np.ones(groups)
                if m["unit_assembly"]
                else np.round(
                    qty_sum
                    * m.get("residual_ratio", 1.0)
                    * np.exp(0.01 * r.standard_normal(groups)),
                    6 if regime == "mass" else 0,  # roll lengths are whole numbers
                )
            )
            for j, rows in enumerate(in_rows):
                self.emit(
                    buf,
                    seg,
                    t0,
                    t1,
                    rows,
                    out_names,
                    out_seq,
                    out_uuid,
                    out_mat,
                    qty_in=L.qty[rows],
                    qty_out=qty_out,
                    qty_net=qty_out if j == 0 else np.zeros(groups),
                    unit=w.unit_mass if regime == "mass" else None,
                    seg_uuid=seg_uuid,
                    eq_idx=eq_idx,
                )
            used = np.array(used, dtype=np.int64)
            assert len(np.unique(used)) == len(used), (
                f"a lot was used twice in one {seg_id} merge round"
            )
            L.alive[used] = False
            L.step[used] = TERMINAL
            for key in list(self.queues):
                if key[0] == seg_id:
                    self.queues[key] = [i for i in self.queues[key] if L.alive[i]]
            idx = L.add(
                out_names,
                out_uuid,
                out_seq,
                w.mat_index[out_mat],
                0,
                t1,
                qty=qty_out,
                prod_ts=t0,
                prev_seg=np.array(seg_uuid, dtype=object),
                cause=cause,
                born=day,
            )
            L.eq[idx] = eq_idx
            self.mark_cohort_cause(idx, seg, t0)
            self.schedule_next(idx, out_mat, 0, t1, r)
            self.counts["transformation"] += groups * sum(k for k, _ in needs.values())
            formed += groups
        return formed

    # ---- splits ------------------------------------------------------------------------------------------------
    def process_split(
        self, grp: np.ndarray, st: dict[str, Any], buf: ev.EventBuffer
    ) -> None:
        """Split lots at a route step.

        Args:
            grp: Lot indices.
            st: Route step.
            buf: Event buffer.
        """
        self._split(grp, st["seg"], buf)

    def process_split_queue(self, day: int, buf: ev.EventBuffer) -> int:
        """Split the queued lots that are ready.

        Args:
            day: Day number.
            buf: Event buffer.

        Returns:
            Number of lots split.
        """
        L = self.lots
        day_end = (day + 1) * SEC_DAY
        n = 0
        for key in [k for k in self.queues if k[0] == "split"]:
            idx = np.array(
                [i for i in self.queues[key] if L.alive[i] and L.ready[i] < day_end],
                dtype=np.int64,
            )
            if len(idx):
                self._split(idx, key[1], buf)
                self.queues[key] = [i for i in self.queues[key] if L.alive[i]]
                n += len(idx)
        return n

    def _split(self, grp: np.ndarray, seg_id: str, buf: ev.EventBuffer) -> None:
        """Split each lot into new child lots.

        Args:
            grp: Lot indices.
            seg_id: Split segment id.
            buf: Event buffer.
        """
        L, w = self.lots, self.world
        sp = w.splits[seg_id]
        seg = w.segments[seg_id]
        r = self.r("split", seg_id)
        out_mat = sp["output"]
        for i in grp:
            n_out = max(
                1,
                int(
                    round(
                        sp["n_out_median"]
                        * np.exp(sp["n_out_sigma"] * r.standard_normal())
                    )
                ),
            )
            names = w.pool_for(out_mat).take(n_out)
            seq = np.ones(n_out, np.int32)
            uuids = lot_uuids(names, seq)
            seg_uuid = [segment_instance_id("split", u) for u in uuids]
            t0 = L.ready[i] + np.sort(r.uniform(0, sp["spread_min"] * 60, n_out))
            t1 = t0 + w.duration_seconds(seg, r, n_out)
            eq_idx = np.full(n_out, int(r.integers(0, len(seg.equipment))))
            # a new input material takes a share of the children (World._apply_structural_event)
            mats = np.full(n_out, out_mat, dtype=object)
            for new, share in sp.get("variants", []):
                mats[r.random(n_out) < share] = new
            for mat in dict.fromkeys(mats):
                k = mats == mat
                n_k = int(k.sum())
                in_override = {
                    "name": [L.name[i]] * n_k,
                    "seq": np.full(n_k, L.seq[i]),
                    "uuid": [L.uuid[i]] * n_k,
                    "mat": [w.mat_list[L.mat[i]]] * n_k,
                    "prev": np.array([L.prev_seg[i]] * n_k, dtype=object),
                }
                seg_uuid_k = [u for u, keep in zip(seg_uuid, k) if keep]
                self.emit(
                    buf,
                    seg,
                    t0[k],
                    t1[k],
                    np.full(n_k, i),
                    names[k],
                    seq[k],
                    uuids[k],
                    mat,
                    qty_in=np.full(n_k, L.qty[i]),
                    qty_out=np.ones(n_k),
                    qty_net=np.ones(n_k),
                    unit=None,
                    seg_uuid=seg_uuid_k,
                    eq_idx=eq_idx[k],
                    in_override=in_override,
                )
                idx = L.add(
                    names[k],
                    uuids[k],
                    seq[k],
                    w.mat_index[mat],
                    0,
                    t1[k],
                    qty=1.0,
                    prod_ts=t0[k],
                    prev_seg=np.array(seg_uuid_k, dtype=object),
                    cause=int(L.cause[i]),
                    born=self.day,
                )
                L.eq[idx] = eq_idx[k]
                self.mark_cohort_cause(idx, seg, t0[k])
                self.schedule_next(idx, mat, 0, t1[k], r)
            L.alive[i] = False
            L.step[i] = TERMINAL
            self.counts["transformation"] += n_out

    # ---- consumable dispense -------------------------------------------------------------------------------------
    def process_dispense(
        self,
        grp: np.ndarray,
        mat_id: str,
        si: int,
        st: dict[str, Any],
        buf: ev.EventBuffer,
    ) -> None:
        """Dispense from a feeder into each lot.

        Each dispense writes two events into the same output state: one from the lot
        and one from the feeder. The feeder's state only changes when it is refilled.
        A lot waits an hour when no feeder is ready.

        Args:
            grp: Lot indices.
            mat_id: Material id.
            si: Index of the step in the route.
            st: Route step.
            buf: Event buffer.
        """
        L, w = self.lots, self.world
        cons = w.consumables[st["seg"]]
        seg = w.segments[st["seg"]]
        bump_seg = w.segments[cons["bump_segment"]]
        r = self.r("dispense", st["seg"])
        pool = [
            i
            for i in self.queues.get(("consumable", st["seg"]), [])
            if L.alive[i] and L.uses[i] > 0
        ]
        pool.sort(key=lambda i: L.ready[i])
        order = grp[np.argsort(L.ready[grp], kind="stable")]
        to_mat = st.get("to") or mat_id
        served, starts, ends = [], [], []
        for i in order:
            active = next((c for c in pool if L.ready[c] <= L.ready[i]), None)
            if active is None:
                L.ready[i] = L.ready[i] + 3600  # wait for a consumable
                continue
            t0 = np.array([L.ready[i]])
            t1 = t0 + w.duration_seconds(seg, r, 1)
            out_name, out_seq = L.name[i], np.array([L.seq[i] + 1], np.int32)
            out_uuid = lot_uuids([out_name], out_seq)
            su = [segment_instance_id("disp", out_uuid[0])]
            eq_idx = np.array([int(r.integers(0, len(seg.equipment)))])
            # row 1: target lot -> dispensed target (same name, new material); row 2: consumable -> dispensed target
            self.emit(
                buf,
                seg,
                t0,
                t1,
                np.array([i]),
                [out_name],
                out_seq,
                out_uuid,
                to_mat,
                qty_in=np.ones(1),
                qty_out=np.ones(1),
                qty_net=np.ones(1),
                unit=None,
                seg_uuid=su,
                eq_idx=eq_idx,
            )
            self.emit(
                buf,
                seg,
                t0,
                t1,
                np.array([active]),
                [out_name],
                out_seq,
                out_uuid,
                to_mat,
                qty_in=L.qty[[active]],
                qty_out=np.ones(1),
                qty_net=np.zeros(1),
                unit=None,
                seg_uuid=su,
                eq_idx=eq_idx,
            )
            L.name[i], L.seq[i], L.uuid[i], L.prev_seg[i], L.prod_ts[i], L.eq[i] = (
                out_name,
                out_seq[0],
                out_uuid[0],
                su[0],
                t0[0],
                eq_idx[0],
            )
            L.cause[i] |= L.cause[active]
            # the feeder keeps its uuid across uses (one uuid feeds many units); its sequence advances only on refill
            L.qty[active] -= 1
            L.uses[active] -= 1
            L.ready[active] = t1[0] + 30
            if (
                int(L.uses[active]) > 0
                and int(L.qty[active]) % int(cons.get("refill_every", 10**9)) == 0
            ):
                c_seq = np.array([L.seq[active] + 1], np.int32)
                c_uuid = lot_uuids([L.name[active]], c_seq)
                csu = [segment_instance_id("bump", c_uuid[0])]
                self.emit(
                    buf,
                    bump_seg,
                    t1,
                    t1,
                    np.array([active]),
                    [L.name[active]],
                    c_seq,
                    c_uuid,
                    w.mat_list[L.mat[active]],
                    qty_in=L.qty[[active]] + 1,
                    qty_out=L.qty[[active]],
                    qty_net=np.array([-1.0]),
                    unit=None,
                    seg_uuid=csu,
                    eq_idx=eq_idx,
                )
                L.seq[active], L.uuid[active], L.prev_seg[active], L.prod_ts[active] = (
                    c_seq[0],
                    c_uuid[0],
                    csu[0],
                    t1[0],
                )
                L.eq[active] = eq_idx[0]
                self.mark_cohort_cause(np.array([active]), bump_seg, t1)
                self.counts["sequence_bump"] += 1
            if L.uses[active] <= 0:
                L.alive[active] = False
                L.step[active] = TERMINAL
                pool.remove(active)
            served.append(i)
            starts.append(t0[0])
            ends.append(t1[0])
            self.counts["transformation"] += 1
            self.counts["sequence_bump"] += 1
        if served:
            idx = np.array(served, dtype=np.int64)
            self.mark_cohort_cause(idx, seg, np.array(starts))
            self.schedule_next(
                idx,
                to_mat,
                si + 1 if to_mat == mat_id else 0,
                np.array(ends) + 1,
                r,
            )
        self.queues[("consumable", st["seg"])] = [
            i for i in self.queues.get(("consumable", st["seg"]), []) if L.alive[i]
        ]

    # ---- legacy population (orphan inputs) -------------------------------------------------------------------------
    def seed_legacy(self) -> None:
        """Add lots that are already in progress on day 0.

        Their earlier events are not in the data, so they show up as legacy lots.
        """
        if not self.sc["anomalies"]["enabled"]:
            return
        w = self.world
        r = self.r("legacy")
        scale = self.sc["volume"]["scale"]
        for mat, n in w.anomalies["legacy_lots"].items():
            if mat not in w.routes:
                continue
            n = max(1, int(n * scale))
            names = w.pool_for(mat).take(n)
            seq = r.integers(2, 9, n).astype(np.int32)
            uuids = lot_uuids(names, seq)
            steps = self.route(mat)
            step = r.integers(0, len(steps), n).astype(np.int16)
            ready = r.uniform(0, 3 * SEC_DAY, n)
            idx = self.lots.add(
                names,
                uuids,
                seq,
                w.mat_index[mat],
                0,
                ready,
                prod_ts=ready - 3600,
                born=0,
            )
            self.lots.step[idx] = step
            self.orphan_lots += n

    # ---- duplicates ---------------------------------------------------------------------------------------------------
    def inject_duplicates(self, table: pa.Table) -> pa.Table:
        """Add duplicate rows: null twins, exact copies and conflicting copies.

        Copies follow their original row, and ids are renumbered.

        Args:
            table: Rows of one day.

        Returns:
            Rows with the duplicates added.
        """
        if not self.sc["duplicates"]["enabled"] or table.num_rows == 0:
            return table
        w = self.world
        r = self.r("dups")
        n = table.num_rows
        has_prev = pc.is_valid(table["in_prev_segment"]).to_numpy(zero_copy_only=False)
        twin_p = (
            min(1.0, w.duplicates["twin"] / max(1e-9, has_prev.mean()))
            if has_prev.any()
            else 0.0
        )
        u = r.random(n)
        twin = has_prev & (u < twin_p)
        u2 = r.random(n)
        exact = ~twin & (u2 < w.duplicates["exact"])
        conflict = (
            ~twin & ~exact & (u2 < w.duplicates["exact"] + w.duplicates["conflict"])
        )
        # interleave: original row then its copies, ids reassigned afterwards
        rep = 1 + twin.astype(int) + exact.astype(int) + conflict.astype(int)
        src_idx = np.repeat(np.arange(n), rep)
        starts = np.cumsum(rep) - rep
        copy_rank = np.arange(len(src_idx)) - np.repeat(
            starts, rep
        )  # 0 for the original row, 1.. for its copies
        out = table.take(pa.array(src_idx))
        is_copy = copy_rank > 0
        twin_rows = is_copy & twin[src_idx]
        prev = out["in_prev_segment"].to_numpy(zero_copy_only=False).astype(object)
        prev[twin_rows] = None
        cols = {c: out[c] for c in out.column_names}
        cols["in_prev_segment"] = pa.array(prev.tolist(), type=pa.string())
        conf_rows = is_copy & conflict[src_idx]
        if conf_rows.any():
            ops = out["operator"].to_numpy(zero_copy_only=False).astype(object)
            ops[conf_rows] = [
                f"OP_{int(x):03d}" for x in r.integers(1, 999, int(conf_rows.sum()))
            ]
            cols["operator"] = pa.array(ops.tolist(), type=pa.string())
        first_id = int(out["id"][0].as_py())
        cols["id"] = pa.array(
            np.arange(first_id, first_id + len(src_idx), dtype=np.int64)
        )
        return pa.table(cols, schema=table.schema)

    # ---- day loop -------------------------------------------------------------------------------------------------------
    def run_day(self, day: int) -> pa.Table:
        """Simulate one day.

        Args:
            day: Day number.

        Returns:
            Rows emitted during the day, duplicates included.
        """
        self.day = day
        self._uses = {}
        w = self.world
        if day % 7 == 0:
            w.weekly_churn(day, self.sc["churn_multiplier"])
        w.apply_structural(day)
        buf = ev.EventBuffer()
        if day == 0:
            self.seed_legacy()
            self.plant_cohorts()
        self.create_sources(day, buf)
        for _ in range(400):
            moved = self.process_wave(day, buf)
            moved += self.process_merges(day, buf)
            moved += self.process_split_queue(day, buf)
            if moved == 0:
                break
        table = buf.to_table(self.next_id)
        self.next_id += buf.n
        raw = self.inject_duplicates(table)
        self.next_id = int(raw["id"][-1].as_py()) + 1 if raw.num_rows else self.next_id
        self.counts["raw_rows"] += raw.num_rows
        self.lots.compact()
        self._reindex_queues()
        return raw

    def _reindex_queues(self) -> None:
        """Rebuild every queue from the queue recorded on each lot, after compaction."""
        L = self.lots
        self.queues = {}
        for i in np.flatnonzero(L.alive[: L.n] & (L.queue[: L.n] >= 0)).tolist():
            self.queues.setdefault(self.queue_keys[L.queue[i]], []).append(i)

    def plant_cohorts(self) -> None:
        """Pick the equipment of each planted cause in the scenario."""
        w = self.world
        for k, c in enumerate(self.sc["cohorts"]):
            seg = w.segments[c["segment"]]
            eq_idx = int(self.r("plant", k).integers(0, len(seg.equipment)))
            self.planted.append(
                {
                    "id": k,
                    "segment": c["segment"],
                    "eq_idx": eq_idx,
                    "equipment": seg.equipment[eq_idx],
                    "day": c["day"],
                    "effect": c["effect"],
                    "background": c["background"],
                    "outcome_segment": c.get(
                        "outcome_segment", w.t["cohort_outcome_segment"]
                    ),
                }
            )
