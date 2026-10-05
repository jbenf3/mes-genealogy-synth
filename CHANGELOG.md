# Changelog

## 0.1.0

First release.
- **Generator:** deterministic simulation of a three-stage line (mass blends, rolls, discrete units). It covers merges, splits, consumable feeders, storage moves, rework, scrap and planted failure causes, with a configurable production ramp and process churn.
- **Raw rows** include duplicates, legacy inputs, clock skew and re-minted keys. After the documented deduplication, the genealogy has no cycles and no duplicate paths.
- **Reference queries:** eight genealogy queries in recursive SQL that reads the event files directly (`queries/`).
- **Expected answers** of every query for the `smoke` and `official_1x` scenarios (`expected/`), with each query's parameters and the fingerprint of the data they hold for.
- **Tooling:** managed with uv (`uv sync`, `uv run`); every runtime dependency pinned to an exact version. ruff formats the code and checks docstrings and type hints.
