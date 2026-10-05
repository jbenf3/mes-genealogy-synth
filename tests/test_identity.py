"""Tests for lot ids, name pools and random streams."""

import numpy as np

from mesgen.identity import NamePool, lot_uuid, lot_uuids, segment_instance_id
from mesgen.rng import Rng


def test_uuid_is_function_of_name_and_seq() -> None:
    """A uuid depends only on the name, the sequence and the variant."""
    assert lot_uuid("AB0001", 3) == lot_uuid("AB0001", 3)
    assert lot_uuid("AB0001", 3) != lot_uuid("AB0001", 4)
    assert lot_uuid("AB0001", 3) != lot_uuid("AB0001", 3, variant="b")
    assert len(lot_uuid("x", 1)) == 32


def test_vectorized_uuids_match_scalar() -> None:
    """lot_uuids gives the same uuids as lot_uuid."""
    names = np.array(["A1", "A2"], dtype=object)
    out = lot_uuids(names, np.array([1, 2]))
    assert list(out) == [lot_uuid("A1", 1), lot_uuid("A2", 2)]


def test_name_pool_never_repeats_and_is_fixed_width() -> None:
    """Names from a pool never repeat and all have the same length."""
    pool = NamePool("SH", 10)
    a, b = pool.take(3), pool.take(2)
    assert len(set(a) | set(b)) == 5
    assert all(len(n) == 12 for n in a)
    assert pool.counter == 5


def test_rng_streams_are_independent_and_reproducible() -> None:
    """Streams repeat for the same keys and differ for different keys."""
    r1, r2 = Rng(7), Rng(7)
    assert r1.stream("a", 1).random() == r2.stream("a", 1).random()
    assert r1.stream("a", 1).random() != r1.stream("a", 2).random()
    assert segment_instance_id("merge", 1, 2) == segment_instance_id("merge", 1, 2)
