"""Deterministic random streams: one independent generator per (seed, key...)."""

import hashlib

import numpy as np


class Rng:
    """Deterministic random streams for one seed.

    Attributes:
        seed: Scenario seed.
    """

    def __init__(self, seed: int) -> None:
        """Create the stream source.

        Args:
            seed: Scenario seed.
        """
        self.seed = int(seed)

    def stream(self, *keys: object) -> np.random.Generator:
        """Return a random generator for the given keys.

        The same seed and keys always give the same generator.

        Args:
            *keys: Values that name the stream. Their string forms are hashed.

        Returns:
            Random generator.
        """
        digest = hashlib.blake2b(
            "|".join(str(k) for k in keys).encode(), digest_size=8
        ).digest()
        return np.random.default_rng(
            np.random.SeedSequence([self.seed, int.from_bytes(digest, "big")])
        )
