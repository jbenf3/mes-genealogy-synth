"""Parquet output layout: base/ for the base load, stream/day=D/hour=H/ for the update stream."""

import pathlib
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

WRITE_OPTS: dict[str, Any] = dict(
    compression="zstd",
    compression_level=3,
    use_dictionary=True,
    write_statistics=True,
    data_page_size=1 << 20,
    version="2.6",
)


def write_day(table: pa.Table, out: pathlib.Path, day: int, base: bool) -> list[str]:
    """Write one day of events to Parquet.

    Args:
        table: Events for the day.
        out: Output directory.
        day: Day number.
        base: If True, write one file to base/. Otherwise write one file per hour
            to stream/.

    Returns:
        Paths of the written files.
    """
    written = []
    if base:
        path = out / "base" / f"day={day:04d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, path, **WRITE_OPTS)
        return [str(path)]
    hours = pc.hour(table["event_ts"]).to_numpy()
    for h in sorted(set(hours.tolist())):
        part = table.filter(pc.equal(pc.hour(table["event_ts"]), h))
        path = out / "stream" / f"day={day:04d}" / f"hour={h:02d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(part, path, **WRITE_OPTS)
        written.append(str(path))
    return written


def write_table(table: pa.Table, path: pathlib.Path) -> None:
    """Write a table to a Parquet file, creating its directory if needed.

    Args:
        table: Table to write.
        path: File path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, **WRITE_OPTS)
