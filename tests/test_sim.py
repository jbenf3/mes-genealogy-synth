"""Unit checks on the simulation's random streams, planted causes and profile checks."""

import numpy as np
import pytest

from mesgen.events import EventBuffer
from mesgen.identity import lot_uuids
from mesgen.profile import load_profile, validate_profile
from mesgen.rng import Rng
from mesgen.scenario import load_scenario
from mesgen.sim import SEC_DAY, Simulation


@pytest.fixture
def sim() -> Simulation:
    """Build a smoke simulation with no lots and one planted cause on SEG_032.

    The cause starts on day 1, on the segment's first equipment, and fails every
    exposed unit at the test step half the time.

    Returns:
        Simulation.
    """
    sc = load_scenario("smoke")
    s = Simulation(load_profile("default"), sc, Rng(sc["seed"]))
    s.planted = [
        {
            "id": 0,
            "segment": "SEG_032",
            "eq_idx": 0,
            "day": 1,
            "effect": 0.5,
            "background": 0.0,
            "outcome_segment": "SEG_041",
        }
    ]
    return s


def _add_lots(
    s: Simulation, n: int, mat: str = "MAT_01", ready: float = 0.0, prefix: str = "T"
) -> np.ndarray:
    """Add lots on equipment 0.

    Args:
        s: Simulation.
        n: Number of lots.
        mat: Material id.
        ready: Ready time, in seconds since day 0.
        prefix: Start of the lot names.

    Returns:
        Lot indices.
    """
    names = np.array([f"{prefix}{i:04d}" for i in range(n)], dtype=object)
    seq = np.ones(n, np.int32)
    idx = s.lots.add(
        names, lot_uuids(names, seq), seq, s.world.mat_index[mat], 0, np.full(n, ready)
    )
    s.lots.eq[idx] = 0
    return idx


def test_same_stream_name_gives_new_numbers_each_time(sim: Simulation) -> None:
    """Two uses of one stream name on the same day draw different numbers."""
    assert not np.array_equal(sim.r("x").random(5), sim.r("x").random(5))


def test_exposed_units_checked_one_at_a_time_fail_at_the_effect_rate(
    sim: Simulation,
) -> None:
    """Units checked one by one fail about as often as the planted effect says."""
    sim.day = 1
    idx = _add_lots(sim, 200)
    sim.lots.cause[idx] = 1
    seg = sim.world.segments["SEG_041"]
    failed = [sim.cohort_failures(np.array([i]), seg)[0] for i in idx]
    assert 0.35 < np.mean(failed) < 0.65


def test_exposure_starts_at_the_cause_day_by_event_time(sim: Simulation) -> None:
    """An event just before the cause's first day isn't exposed; one at its start is."""
    idx = _add_lots(sim, 2)
    seg = sim.world.segments["SEG_032"]
    sim.mark_cohort_cause(idx[:1], seg, SEC_DAY - 1)
    sim.mark_cohort_cause(idx[1:], seg, SEC_DAY)
    assert list(sim.lots.cause[idx]) == [0, 1]


def test_a_refill_on_the_planted_machine_exposes_units_dispensed_after_it(
    sim: Simulation,
) -> None:
    """A unit dispensed before the refill isn't exposed; one dispensed after it is."""
    w = sim.world
    sim.planted[0] |= {"segment": "SEG_056", "eq_idx": 0, "day": 0}
    w.segments["SEG_056"].equipment = w.segments["SEG_056"].equipment[:1]
    w.consumables["SEG_011"] = dict(w.consumables["SEG_011"], refill_every=1)
    feeder = _add_lots(sim, 1, mat="MAT_15", prefix="F")
    sim.lots.qty[feeder] = sim.lots.uses[feeder] = 10
    sim.join_queue(feeder, ("consumable", "SEG_011"))
    first = _add_lots(sim, 1, mat="MAT_11", ready=100.0, prefix="A")
    second = _add_lots(sim, 1, mat="MAT_11", ready=5000.0, prefix="B")
    step = w.routes["MAT_11"][0]
    sim.process_dispense(np.r_[first, second], "MAT_11", 0, step, EventBuffer())
    assert list(sim.lots.cause[np.r_[first, second]]) == [0, 1]


def test_epoch_without_a_time_zone_is_rejected() -> None:
    """A profile epoch must name its time zone."""
    profile = load_profile("default")
    profile["template"]["epoch"] = "2000-01-03T00:00:00"
    with pytest.raises(ValueError, match="time zone"):
        validate_profile(profile)
