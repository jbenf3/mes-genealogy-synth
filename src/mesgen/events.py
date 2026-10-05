"""Per-day event buffer and JSON field builders."""

from collections.abc import Iterable
from typing import Any

import numpy as np
import pyarrow as pa

from .schema import COLUMNS, EVENT_SCHEMA


class EventBuffer:
    """Collect the events of one day, column by column.

    Attributes:
        cols: Values per column name. Every list has the same length.
        n: Number of events.
    """

    def __init__(self) -> None:
        """Create an empty buffer."""
        self.cols: dict[str, list] = {c: [] for c in COLUMNS if c != "id"}
        self.n = 0

    def extend(self, **cols: Any) -> None:
        """Append events.

        Args:
            **cols: Values per column: a list or array with one value per event, or
                one value for all of them. Missing columns are null. `begin_ts` is
                required and sets the number of events.

        Raises:
            ValueError: If a list or array has the wrong length.
        """
        n = len(cols["begin_ts"])
        for c in self.cols:
            v = cols.get(c)
            if v is None:
                self.cols[c].extend([None] * n)
            elif isinstance(v, (list, np.ndarray)):
                if len(v) != n:
                    raise ValueError(f"column {c} has {len(v)} values, expected {n}")
                self.cols[c].extend(v.tolist() if isinstance(v, np.ndarray) else v)
            else:
                self.cols[c].extend([v] * n)
        self.n += n

    def to_table(self, first_id: int) -> pa.Table:
        """Build an Arrow table of the events.

        Rows are sorted by `begin_ts` (stable sort) and get consecutive ids.

        Args:
            first_id: Id of the first row.

        Returns:
            Table with the event schema.
        """
        order = np.argsort(
            np.asarray(self.cols["begin_ts"], dtype=np.int64), kind="stable"
        )
        arrays = {
            "id": pa.array(np.arange(first_id, first_id + self.n, dtype=np.int64))
        }
        for c, values in self.cols.items():
            field = EVENT_SCHEMA.field(c)
            arr = np.asarray(values, dtype=object)[order]
            if pa.types.is_timestamp(field.type):
                arrays[c] = pa.array(
                    np.asarray([0 if v is None else v for v in arr], dtype=np.int64),
                    type=pa.int64(),
                ).cast(field.type)
            elif pa.types.is_floating(field.type):
                arrays[c] = pa.array(
                    [None if v is None else float(v) for v in arr], type=field.type
                )
            elif pa.types.is_integer(field.type):
                arrays[c] = pa.array(
                    [None if v is None else int(v) for v in arr], type=field.type
                )
            else:
                arrays[c] = pa.array(arr.tolist(), type=field.type)
        return pa.table([arrays[c] for c in COLUMNS], schema=EVENT_SCHEMA)


def json_object(keys: list[str], value_lists: list[list[str]]) -> list[str]:
    """Build one JSON object string per row.

    Args:
        keys: Object keys.
        value_lists: One list per key, holding the JSON-encoded value of each row.

    Returns:
        JSON object strings, one per row.
    """
    n = len(value_lists[0]) if value_lists else 0
    if not keys:
        return ["{}"] * n
    heads = [f'"{k}": ' for k in keys]
    return [
        "{" + ", ".join(h + v[i] for h, v in zip(heads, value_lists)) + "}"
        for i in range(n)
    ]


def drop_keys(obj: str, keys: set[str]) -> str:
    """Remove keys from a JSON object string built by `json_object`.

    Works on the text, so the other values keep their exact spelling.

    Args:
        obj: JSON object string.
        keys: Keys to remove.

    Returns:
        JSON object string without those keys.
    """
    if obj == "{}":
        return obj
    pairs = obj[1:-1].split(", ")  # no value contains ", "
    return "{" + ", ".join(p for p in pairs if p[1 : p.index('"', 1)] not in keys) + "}"


def enc_str(values: Iterable[object]) -> list[str]:
    """Encode values as JSON strings.

    Args:
        values: Values to encode.

    Returns:
        Quoted strings.
    """
    return [f'"{v}"' for v in values]


def enc_int(values: Iterable[float]) -> list[str]:
    """Encode values as JSON integers.

    Args:
        values: Numbers to encode.

    Returns:
        Integer strings.
    """
    return [str(int(v)) for v in values]


def enc_bool(values: Iterable[object]) -> list[str]:
    """Encode values as JSON booleans.

    Args:
        values: Values to encode, by truthiness.

    Returns:
        "true" or "false" strings.
    """
    return ["true" if v else "false" for v in values]
