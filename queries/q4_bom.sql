-- Q4 bill of materials: every other lot name among the ancestors of any state of the lot q4_lot_name.
WITH RECURSIVE
edges AS MATERIALIZED (       -- one row per input state -> output state, built once
    SELECT DISTINCT in_lot_uuid, in_lot_name, out_lot_uuid, out_lot_name FROM events
    WHERE in_lot_uuid IS NOT NULL AND out_lot_uuid IS NOT NULL
),
ancestors(lot_uuid, lot_name) AS (
    SELECT in_lot_uuid, in_lot_name FROM edges WHERE out_lot_name = getvariable('q4_lot_name')
    UNION
    SELECT e.in_lot_uuid, e.in_lot_name FROM edges e JOIN ancestors a ON e.out_lot_uuid = a.lot_uuid
)
SELECT DISTINCT lot_name FROM ancestors WHERE lot_name != getvariable('q4_lot_name') ORDER BY lot_name;
