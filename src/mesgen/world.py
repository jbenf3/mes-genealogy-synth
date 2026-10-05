"""Runtime process template: segments, materials, routes, vocabularies, and the churn mutations applied to them."""

from typing import Any

import numpy as np

from .identity import NamePool
from .rng import Rng

WAIT = -1  # lot is queued at a merge / consumable step
TERMINAL = -2  # lot left the process


class Segment:
    """A segment type: one kind of process step and its equipment.

    Attributes:
        id: Segment id, such as "SEG_017".
        cls: Segment class, such as "line_step".
        regime: Material regime: "discrete", "roll" or "mass".
        loc_type: Location type of its events.
        gap_min: Median wait before the step, in minutes.
        duration_s: Median duration, in seconds.
        equipment: Equipment paths.
        operators: Operator ids.
        in_keys: Property keys on input lots.
        out_keys: Property keys on output lots.
        recipe: Recipe number, or None if the step runs no recipe.
        scrap: Scrap rate.
        useasis: Use-as-is rate.
        born_day: Day the segment appears.
        retire_day: Day the segment is retired, or None.
    """

    __slots__ = (
        "id",
        "cls",
        "regime",
        "loc_type",
        "gap_min",
        "duration_s",
        "equipment",
        "operators",
        "in_keys",
        "out_keys",
        "recipe",
        "scrap",
        "useasis",
        "born_day",
        "retire_day",
    )

    def __init__(
        self,
        sid: str,
        cls: str,
        regime: str,
        loc_type: str,
        gap_min: float,
        duration_s: float,
        equipment: list[str],
        operators: list[str],
        in_keys: list[str],
        out_keys: list[str],
        recipe: int | None,
        scrap: float,
        useasis: float,
        born_day: int = 0,
        retire_day: int | None = None,
    ) -> None:
        """Create a segment.

        Args:
            sid: Segment id.
            cls: Segment class.
            regime: Material regime.
            loc_type: Location type of its events.
            gap_min: Median wait before the step, in minutes.
            duration_s: Median duration, in seconds.
            equipment: Equipment paths.
            operators: Operator ids.
            in_keys: Property keys on input lots.
            out_keys: Property keys on output lots.
            recipe: Recipe number, or None.
            scrap: Scrap rate.
            useasis: Use-as-is rate.
            born_day: Day the segment appears.
            retire_day: Day the segment is retired, or None.
        """
        self.id, self.cls, self.regime, self.loc_type = sid, cls, regime, loc_type
        self.gap_min, self.duration_s = float(gap_min), float(duration_s)
        self.equipment, self.operators = list(equipment), list(operators)
        self.in_keys, self.out_keys = list(in_keys), list(out_keys)
        self.recipe, self.scrap, self.useasis = recipe, float(scrap), float(useasis)
        self.born_day, self.retire_day = born_day, retire_day


class World:
    """The process template at run time, and the changes churn makes to it.

    Built from the profile. Every change is recorded in `churn_log`.

    Attributes:
        profile: Profile.
        scenario: Scenario settings.
        rng: Random streams.
        segments: Segments by id.
        materials: Materials by id.
        routes: Route steps per material.
        merges: Merge settings per segment id.
        splits: Split settings per segment id.
        consumables: Feeder settings per segment id.
        pools: Name pool per material.
        sources: Daily creation of raw lots.
        churn_log: Changes as (seconds, day, class, kind, entity, detail) tuples.
    """

    def __init__(
        self, profile: dict[str, Any], scenario: dict[str, Any], rng: Rng
    ) -> None:
        """Build the world from a profile.

        Args:
            profile: Profile.
            scenario: Scenario settings.
            rng: Random streams.
        """
        self.profile, self.scenario, self.rng = profile, scenario, rng
        t = profile["template"]
        self.t = t
        r = rng.stream("world")
        # --- vocabularies -----------------------------------------------------------------------
        self.materials = {m["id"]: m for m in profile["materials"]}
        self.mat_index = {m: i for i, m in enumerate(self.materials)}
        self.mat_list = list(self.materials)
        self.regime_of = {m["id"]: m["regime"] for m in profile["materials"]}
        fam = t["name_families"]
        self.pools = {m: NamePool(pfx, width) for m, (pfx, width) in fam.items()}
        self.operator_pool = [f"OP_{i + 1:03d}" for i in range(t["operators"]["count"])]
        self.operator_fill = t["operators"]["fill"]
        self.equipment_counter = 0
        self.areas = t["equipment"]["areas"]
        # key -> (value kind, mixed types)
        self.prop_pool: dict[str, tuple[str, bool]] = {}
        self.prop_counter = 0
        self.prop_prefix = t["properties"]["prefix"]
        for _ in range(t["properties"]["pool_size"]):
            self._new_prop_key(r)
        self.recipe_count = t["recipes"]["count"]
        self.recipe_keys = t["recipes"]["keys"]
        disp = t["disposition"]
        self.codes = [f"DC_{i + 1:03d}" for i in range(disp["n_codes"])]
        w = 1.0 / np.arange(1, disp["n_codes"] + 1) ** disp["zipf"]
        self.code_weights = w / w.sum()
        self.processes = [f"DP_{i + 1:02d}" for i in range(disp["processes"])]
        self.disp_keys = disp["keys"]
        self.case_variant_from_day = disp["case_variant_from_week"] * 7
        self.case_variant_share = disp["case_variant_share"]
        self.disp_scrap = disp["values"]["scrap"]
        self.disp_useasis = disp["values"]["useasis"]
        self.disp_variant = disp["values"]["variant"]
        self.status_ok = disp["statuses"]["complete"]
        self.status_scrap = disp["statuses"]["scrap"]
        self.scrap = dict(
            t["scrap"]
        )  # scrap step, storage and disposal of scrapped lots, recovery
        self.unit_mass = t.get("unit_mass", "MU")
        self.ext_ref_materials = set(t["external_ref"]["materials"])
        self.ext_ref_prefix = t["external_ref"]["prefix"]
        self.ext_ref_counter = 0
        # --- segments ---------------------------------------------------------------------------
        lt = t["location_types"]
        self.segments = {}
        keys_lo, keys_hi = t["properties"]["keys_per_segment"]
        pool_keys = list(self.prop_pool)
        for s in profile["segments"]:
            equipment = [
                self._new_equipment(r) for _ in range(max(1, s["n_equipment"]))
            ]
            ops = sorted(
                r.choice(
                    self.operator_pool,
                    size=min(len(self.operator_pool), max(1, s["n_operators"])),
                    replace=False,
                ).tolist()
            )
            n_keys = int(r.integers(keys_lo, keys_hi + 1))
            in_keys = sorted(
                r.choice(pool_keys, size=n_keys, replace=False).tolist(),
                key=self._key_rank,
            )
            out_keys = sorted(
                set(in_keys[: max(1, n_keys - int(r.integers(0, 4)))])
                | set(
                    r.choice(
                        pool_keys, size=int(r.integers(0, 3)), replace=False
                    ).tolist()
                ),
                key=self._key_rank,
            )
            recipe = None
            if (
                s["class"] in ("line_step", "transformation")
                and r.random() < t["recipes"]["segment_share"]
            ):
                recipe = int(r.integers(1, self.recipe_count + 1))
            loc_type = lt["overrides"].get(s["id"], lt.get(s["class"], "LINE"))
            self.segments[s["id"]] = Segment(
                s["id"],
                s["class"],
                s["regime"],
                loc_type,
                s["gap_min"],
                s["duration_s"],
                equipment,
                ops,
                in_keys,
                out_keys,
                recipe,
                s["scrap_rate"],
                s["useasis_rate"],
            )
        self.segment_counter = max(
            int(s[4:]) for s in self.segments
        )  # new ids continue after the profile's highest
        self.material_counter = max(int(m[4:]) for m in self.materials)
        # --- routes / merges / splits / consumables --------------------------------------------------
        self.routes = {
            m: [dict(st) for st in steps] for m, steps in t["routes"].items()
        }
        self.merges = {
            k: dict(v, inputs=dict(v["inputs"])) for k, v in t["merges"].items()
        }
        self.splits = {k: dict(v) for k, v in t["splits"].items()}
        self.consumables = dict(t["consumables"])
        self.hold = t.get("hold_loops")
        self.sources = [dict(s) for s in t["sources"]]
        # {material, after, seg, share, start, end}
        self.inserted_steps: list[dict[str, Any]] = []
        self.gap_sigma = t["gap_sigma"]
        self.shift = t["shift_hours"]
        self.weekday_factor = t["weekday_factor"]
        self.weekend_defer = t.get("weekend_defer", 0.0)
        self.batch_release = t.get("batch_release")
        self.anomalies = dict(t["anomalies"])
        self.duplicates = dict(t["duplicates"])
        self.churn_rates = dict(t["churn"]["per_week"])
        self.ephemeral_share = t["churn"]["ephemeral_segment_share"]
        self.structural = sorted(t["churn"]["structural"], key=lambda e: e["day"])
        # (sim_s, day, class, kind, entity, detail)
        self.churn_log: list[tuple[int, int, str, str, str, str]] = []

    # ---- vocabulary helpers ------------------------------------------------------------------------
    def _key_rank(self, k: str) -> int:
        """Return the number in a property key, for sorting.

        Args:
            k: Property key, such as "P_042".

        Returns:
            Key number.
        """
        return int(k[len(self.prop_prefix) :])

    def _new_prop_key(self, r: np.random.Generator) -> str:
        """Add a new property key to the pool.

        Args:
            r: Random generator.

        Returns:
            New key.
        """
        self.prop_counter += 1
        key = f"{self.prop_prefix}{self.prop_counter:03d}"
        kind = r.choice(
            ["tray", "pos", "code", "flag", "num", "group"],
            p=[0.15, 0.15, 0.35, 0.1, 0.15, 0.1],
        )
        alt = r.random() < 0.33  # multi-type key: sometimes encoded as another type
        self.prop_pool[key] = (str(kind), alt)
        return key

    def _new_equipment(self, r: np.random.Generator) -> str:
        """Make a new equipment path in a random area.

        Args:
            r: Random generator.

        Returns:
            Equipment path, such as "AR03/EQ_041".
        """
        self.equipment_counter += 1
        area = int(r.integers(1, self.areas + 1))
        return f"AR{area:02d}/EQ_{self.equipment_counter:03d}"

    def new_ext_ref(self) -> str:
        """Return the next external system reference.

        Returns:
            Reference string.
        """
        self.ext_ref_counter += 1
        return f"{self.ext_ref_prefix}{self.ext_ref_counter:07d}"

    def pool_for(self, material: str) -> NamePool:
        """Return the name pool of a material, creating one if needed.

        Args:
            material: Material id.

        Returns:
            Name pool.
        """
        if material not in self.pools:
            self.pools[material] = NamePool(f"M{len(self.pools):02d}", 10)
        return self.pools[material]

    def gap_seconds(self, seg: Segment, r: np.random.Generator, n: int) -> np.ndarray:
        """Draw waits before a step, in whole seconds.

        Rounding to whole seconds keeps last-bit differences in exp() between CPUs
        out of the data.

        Args:
            seg: Segment.
            r: Random generator.
            n: Number of waits.

        Returns:
            Waits in seconds.
        """
        if seg.gap_min <= 0:
            return np.zeros(n)
        return np.rint(
            60.0 * seg.gap_min * np.exp(self.gap_sigma * r.standard_normal(n))
        )

    def duration_seconds(
        self, seg: Segment, r: np.random.Generator, n: int
    ) -> np.ndarray:
        """Draw step durations, in whole seconds.

        Args:
            seg: Segment.
            r: Random generator.
            n: Number of durations.

        Returns:
            Durations in seconds.
        """
        if seg.duration_s <= 0:
            return np.zeros(n)
        return np.rint(
            seg.duration_s * np.exp(0.4 * r.standard_normal(n))
        )  # whole seconds, as gap_seconds

    def location_of(self, equipment_path: str) -> str:
        """Return the location of an equipment.

        Args:
            equipment_path: Equipment path.

        Returns:
            Location, such as "LOC_041".
        """
        return "LOC_" + equipment_path.rsplit("_", 1)[1]

    # ---- churn -------------------------------------------------------------------------------------
    def log(
        self, sim_s: float, day: int, cls: str, kind: str, entity: str, detail: str
    ) -> None:
        """Add a change to the churn log.

        Args:
            sim_s: Time in seconds since the start of day 0.
            day: Day number.
            cls: Change class: "C1", "C2" or "C3".
            kind: Kind of change.
            entity: What changed.
            detail: Description.
        """
        self.churn_log.append((int(sim_s), int(day), cls, kind, entity, detail))

    def apply_structural(self, day: int) -> None:
        """Apply the profile's scheduled structural changes that are due.

        Args:
            day: Day number.
        """
        while self.structural and self.structural[0]["day"] <= day:
            ev = self.structural.pop(0)
            self._apply_structural_event(ev, day)

    def _apply_structural_event(self, ev: dict[str, Any], day: int) -> None:
        """Apply one structural change: a new input material or an inserted step.

        Args:
            ev: Change settings.
            day: Day number.

        Raises:
            ValueError: If no source or split makes the replaced material, or no lot
                can reach the inserted step.
        """
        sim_s = day * 86400
        if ev["kind"] == "new_input_material":
            splits = [
                sp for sp in self.splits.values() if sp["output"] == ev["replace"]
            ]
            if not splits and not any(
                src["material"] == ev["replace"] for src in self.sources
            ):
                raise ValueError(f"no source or split makes {ev['replace']}")
            self.material_counter += 1
            new = f"MAT_{self.material_counter:02d}"  # always a fresh id: a reused id could feed two inputs of one merge
            old = self.materials[ev["replace"]]
            self.materials[new] = dict(old, id=new)
            self.mat_index[new] = len(self.mat_list)
            self.mat_list.append(new)
            self.regime_of[new] = old["regime"]
            self.pools[new] = (
                self.pools[ev["replace"]]
                if ev["replace"] in self.pools
                else self.pool_for(new)
            )  # shared counter: no name collisions
            self.routes[new] = [dict(st) for st in self.routes[ev["replace"]]]
            for src in list(self.sources):
                if src["material"] == ev["replace"]:
                    self.sources.append(
                        dict(
                            src,
                            material=new,
                            per_day=src["per_day"] * ev["share"],
                            born_day=day,
                        )
                    )
                    src["per_day"] *= 1 - ev["share"]
            for sp in splits:
                sp.setdefault("variants", []).append((new, ev["share"]))
            self.log(
                sim_s,
                day,
                "C2",
                "new_material",
                new,
                f"variant of {ev['replace']} at {ev['share']:.2f} share",
            )
            # the new material is taken wherever the one it replaces is, so no merge runs short
            for seg_id in dict.fromkeys(
                [ev["merge"]]
                + [k for k, m in self.merges.items() if ev["replace"] in m["inputs"]]
            ):
                m = self.merges[seg_id]
                assert all(
                    new not in alts for alts in m.get("alternatives", {}).values()
                ), f"{new} already feeds {seg_id}"
                m.setdefault("alternatives", {}).setdefault(ev["replace"], []).append(
                    new
                )
                self.log(
                    sim_s,
                    day,
                    "C3",
                    "new_transformation_shape",
                    seg_id,
                    f"{new} -> {m['output']} @ {seg_id} (replaces {ev['replace']} share {ev['share']:.2f})",
                )
        elif ev["kind"] == "insert_step":
            if ev["after"] not in self.insert_anchors(ev["material"]):
                raise ValueError(
                    f"no lot can reach a step inserted after {ev['after']} on {ev['material']}"
                )
            seg = self.new_segment(
                day,
                cls="line_step",
                regime=self.regime_of[ev["material"]],
                retire_day=ev.get("end_day"),
            )
            self.inserted_steps.append(
                {
                    "material": ev["material"],
                    "after": ev["after"],
                    "seg": seg.id,
                    "share": ev["share"],
                    "start": day,
                    "end": ev.get("end_day"),
                }
            )
            self.log(
                sim_s,
                day,
                "C3",
                "new_branch",
                seg.id,
                f"inserted after {ev['after']} on {ev['material']} for share {ev['share']:.2f}",
            )

    def insert_anchors(self, material: str) -> list[str]:
        """Return the segments a new step can be inserted after, on a material's route.

        Lots look for an inserted step only when they go on to the next step of the
        same route, so the last step and steps that change material are left out.

        Args:
            material: Material id.

        Returns:
            Segment ids, once per route step.
        """
        return [
            st["seg"]
            for st in self.routes[material][:-1]
            if st["kind"] in ("step", "store_in", "store_out") and not st.get("to")
        ]

    def new_segment(
        self,
        day: int,
        cls: str = "line_step",
        regime: str = "discrete",
        retire_day: int | None = None,
    ) -> Segment:
        """Add a new segment type with one equipment.

        Args:
            day: Day number.
            cls: Segment class.
            regime: Material regime.
            retire_day: Day the segment is retired, or None.

        Returns:
            New segment.
        """
        r = self.rng.stream("newseg", day, self.segment_counter)
        self.segment_counter += 1
        sid = f"SEG_{self.segment_counter:03d}"
        pool_keys = list(self.prop_pool)
        keys = sorted(
            r.choice(pool_keys, size=int(r.integers(6, 14)), replace=False).tolist(),
            key=self._key_rank,
        )
        seg = Segment(
            sid,
            cls,
            regime,
            self.t["location_types"].get(cls, "LINE"),
            float(r.choice([1, 5, 15, 60])),
            0.0,
            [self._new_equipment(r)],
            sorted(
                r.choice(
                    self.operator_pool, size=int(r.integers(1, 4)), replace=False
                ).tolist()
            ),
            keys,
            keys,
            None,
            0.0,
            0.0,
            born_day=day,
            retire_day=retire_day,
        )
        self.segments[sid] = seg
        self.log(
            day * 86400,
            day,
            "C2",
            "new_segment_type",
            sid,
            f"class={cls} regime={regime} retire_day={retire_day}",
        )
        return seg

    def weekly_churn(self, day: int, multiplier: float) -> None:
        """Apply one week of random churn.

        Adds equipment, operators, segments, materials and property keys, and retires
        property keys.

        Args:
            day: First day of the week.
            multiplier: Factor on the weekly churn rates.
        """
        r = self.rng.stream("churn", day)
        sim_s = day * 86400
        rates = {k: v * multiplier for k, v in self.churn_rates.items()}
        mats_with_routes = [
            m for m in self.routes if self.regime_of.get(m) == "discrete"
        ]
        for _ in range(r.poisson(rates["segment"])):
            material = str(r.choice(mats_with_routes))
            anchors = self.insert_anchors(material)
            if not anchors:
                continue
            after = str(r.choice(anchors))
            ephemeral = r.random() < self.ephemeral_share
            seg = self.new_segment(day, retire_day=day + 7 if ephemeral else None)
            share = (
                float(r.uniform(0.02, 0.3))
                if ephemeral
                else float(r.uniform(0.05, 0.6))
            )
            self.inserted_steps.append(
                {
                    "material": material,
                    "after": after,
                    "seg": seg.id,
                    "share": share,
                    "start": day,
                    "end": day + 7 if ephemeral else None,
                }
            )
            self.log(
                sim_s,
                day,
                "C3",
                "new_transition",
                seg.id,
                f"{after} -> {seg.id} on {material} share {share:.2f} ephemeral={ephemeral}",
            )
        for _ in range(r.poisson(rates["material"])):
            src = self.sources[int(r.integers(len(self.sources)))]
            merge = next(
                (k for k, m in self.merges.items() if src["material"] in m["inputs"]),
                None,
            )
            if merge:
                self._apply_structural_event(
                    {
                        "kind": "new_input_material",
                        "merge": merge,
                        "replace": src["material"],
                        "share": 0.2,
                    },
                    day,
                )
        for _ in range(r.poisson(rates["equipment"])):
            seg = self.segments[str(r.choice(list(self.segments)))]
            eq = self._new_equipment(r)
            seg.equipment.append(eq)
            self.log(sim_s, day, "C1", "new_equipment", eq, f"added to {seg.id}")
        for _ in range(r.poisson(rates["operator"])):
            op = f"OP_{len(self.operator_pool) + 1:03d}"
            self.operator_pool.append(op)
            for sid in r.choice(
                list(self.segments), size=int(r.integers(1, 4)), replace=False
            ):
                self.segments[str(sid)].operators.append(op)
            self.log(sim_s, day, "C1", "new_operator", op, "added")
        for _ in range(r.poisson(rates["json_key_add"])):
            key = self._new_prop_key(r)
            targets = r.choice(
                list(self.segments), size=int(r.integers(1, 4)), replace=False
            )
            for sid in targets:
                seg = self.segments[str(sid)]
                seg.in_keys = sorted(set(seg.in_keys) | {key}, key=self._key_rank)
                if r.random() < 0.7:
                    seg.out_keys = sorted(set(seg.out_keys) | {key}, key=self._key_rank)
            self.log(
                sim_s,
                day,
                "C2",
                "new_property_key",
                key,
                f"kind={self.prop_pool[key][0]} segments={len(targets)}",
            )
        for _ in range(r.poisson(rates["json_key_retire"])):
            candidates = [
                k
                for k in self.prop_pool
                if any(
                    k in s.in_keys or k in s.out_keys for s in self.segments.values()
                )
            ]
            if not candidates:
                break
            key = str(r.choice(candidates))
            for s in self.segments.values():
                s.in_keys = [k for k in s.in_keys if k != key]
                s.out_keys = [k for k in s.out_keys if k != key]
            del self.prop_pool[key]  # so no new segment picks it again
            self.log(
                sim_s,
                day,
                "C2",
                "retired_property_key",
                key,
                "removed from all segments",
            )
        if day == self.case_variant_from_day:
            self.log(
                sim_s,
                day,
                "C2",
                "vocabulary_variant",
                "disposition",
                f"lower-case scrap value at share {self.case_variant_share}",
            )
        for seg in list(self.segments.values()):
            if (
                seg.retire_day is not None
                and seg.retire_day <= day
                and seg.retire_day > day - 7
            ):
                self.log(
                    sim_s,
                    day,
                    "C2",
                    "retired_segment_type",
                    seg.id,
                    "ephemeral segment retired",
                )
