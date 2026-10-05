"""Checks on two years of process churn, without simulating any lots."""

from typing import Any

import pytest

from mesgen.profile import load_profile
from mesgen.rng import Rng
from mesgen.scenario import load_scenario
from mesgen.world import World


@pytest.fixture(scope="module")
def churned() -> tuple[World, set[str], list[tuple[str, str]]]:
    """Run two years of weekly churn and scheduled changes on the default profile.

    Returns:
        World, the retired property keys, and (segment, key) for each retired key
        that a later new segment uses.
    """
    sc = load_scenario("official_1x")
    w = World(load_profile("default"), sc, Rng(sc["seed"]))
    retired: set[str] = set()
    reused: list[tuple[str, str]] = []
    for day in range(730):
        if day % 7 == 0:
            w.weekly_churn(day, 2.0)
        w.apply_structural(day)
        reused += [
            (s.id, k)
            for s in w.segments.values()
            if s.born_day == day
            for k in retired & set(s.in_keys + s.out_keys)
        ]
        retired |= {
            entity
            for _, d, _, kind, entity, _ in w.churn_log
            if kind == "retired_property_key" and d == day
        }
    return w, retired, reused


def test_new_segments_never_use_a_retired_key(churned: tuple[Any, ...]) -> None:
    """A retired property key never comes back on a new segment."""
    _, retired, reused = churned
    assert retired and reused == []


def test_lots_can_reach_every_inserted_step(churned: tuple[Any, ...]) -> None:
    """Every inserted step follows a step after which lots stay on the same route."""
    w = churned[0]
    assert w.inserted_steps
    for ins in w.inserted_steps:
        steps = w.routes[ins["material"]]
        # lots leave the route after its last step, and switch routes after a step with "to"
        assert any(
            st["seg"] == ins["after"] and i < len(steps) - 1 and not st.get("to")
            for i, st in enumerate(steps)
        ), ins
