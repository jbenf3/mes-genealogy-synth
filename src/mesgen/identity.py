"""Lot identity: names, sequences and uuids. uuid = f(name, sequence), so the uuid graph is a DAG by construction."""

import hashlib

import numpy as np


def lot_uuid(name: str, seq: int, variant: str = "") -> str:
    """Return the uuid of a lot state.

    Args:
        name: Lot name.
        seq: Sequence number.
        variant: Suffix for a re-minted key, which gets a second uuid. Empty
            otherwise.

    Returns:
        32-character hex string.
    """
    return hashlib.blake2b(
        f"{name}|{seq}{variant}".encode(), digest_size=16
    ).hexdigest()


def lot_uuids(
    names: np.ndarray | list[str], seqs: np.ndarray | list[int], variant: str = ""
) -> np.ndarray:
    """Return the uuids of many lot states.

    Args:
        names: Lot names.
        seqs: Sequence numbers, one per name.
        variant: Same as in `lot_uuid`.

    Returns:
        Object array of uuids.
    """
    return np.fromiter(
        (lot_uuid(n, int(s), variant) for n, s in zip(names, seqs)),
        dtype=object,
        count=len(names),
    )


def segment_instance_id(kind: str, *parts: object) -> str:
    """Return the id of one segment execution.

    Args:
        kind: Kind of execution, such as "ev" or "merge".
        *parts: Values that make the execution unique.

    Returns:
        32-character hex string.
    """
    return hashlib.blake2b(
        "|".join([kind, *map(str, parts)]).encode(), digest_size=16
    ).hexdigest()


class NamePool:
    """Hand out lot names that never repeat: a prefix plus a zero-padded counter.

    Attributes:
        prefix: Name prefix.
        width: Number of counter digits.
        counter: Next counter value.
    """

    def __init__(self, prefix: str, width: int) -> None:
        """Create a pool.

        Args:
            prefix: Name prefix.
            width: Number of counter digits.
        """
        self.prefix = prefix
        self.width = width
        self.counter = 0

    def take(self, n: int) -> np.ndarray:
        """Take the next names.

        Args:
            n: Number of names.

        Returns:
            Object array of names.
        """
        start = self.counter
        self.counter += int(n)
        return np.array(
            [f"{self.prefix}{i:0{self.width}d}" for i in range(start, self.counter)],
            dtype=object,
        )
