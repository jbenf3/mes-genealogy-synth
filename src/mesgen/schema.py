"""Generalized event-row schema (see docs/SCHEMA.md)."""

import pyarrow as pa

EVENT_SCHEMA = pa.schema(
    [
        ("id", pa.int64()),
        ("event_ts", pa.timestamp("us")),
        ("begin_ts", pa.timestamp("us")),
        ("end_ts", pa.timestamp("us")),
        ("segment_name", pa.string()),
        ("segment_uuid", pa.string()),
        ("operation_id", pa.string()),
        ("in_lot_name", pa.string()),
        ("in_lot_sequence", pa.int32()),
        ("in_lot_uuid", pa.string()),
        ("in_material", pa.string()),
        ("in_lot_status", pa.string()),
        ("in_prev_segment", pa.string()),
        ("out_lot_name", pa.string()),
        ("out_lot_sequence", pa.int32()),
        ("out_lot_uuid", pa.string()),
        ("out_material", pa.string()),
        ("out_lot_status", pa.string()),
        ("location", pa.string()),
        ("location_type", pa.string()),
        ("equipment_path", pa.string()),
        ("operator", pa.string()),
        ("work_order_id", pa.string()),
        ("disposition", pa.string()),
        ("disposition_codes", pa.string()),
        ("ext_system_ref", pa.string()),
        ("qty_in", pa.float64()),
        ("qty_out", pa.float64()),
        ("qty_net", pa.float64()),
        ("qty_available", pa.float64()),
        ("qty_scheduled", pa.float64()),
        ("unit", pa.string()),
        ("in_properties", pa.string()),
        ("out_properties", pa.string()),
        ("recipe_json", pa.string()),
    ]
)

COLUMNS = [f.name for f in EVENT_SCHEMA]

CHURN_SCHEMA = pa.schema(
    [
        ("sim_ts", pa.timestamp("us")),
        ("sim_day", pa.int32()),
        ("churn_class", pa.string()),  # C1 | C2 | C3
        ("kind", pa.string()),
        ("entity", pa.string()),
        ("detail", pa.string()),
    ]
)
