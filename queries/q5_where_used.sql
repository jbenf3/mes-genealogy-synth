-- Q5 where-used: every other lot name among the descendants of any state of the lot q5_lot_name.
WITH RECURSIVE
edges AS MATERIALIZED (       -- one row per input state -> output state, built once
    SELECT DISTINCT in_lot_uuid, in_lot_name, out_lot_uuid, out_lot_name FROM events
    WHERE in_lot_uuid IS NOT NULL AND out_lot_uuid IS NOT NULL
),
descendants(lot_uuid, lot_name) AS (
    SELECT out_lot_uuid, out_lot_name FROM edges WHERE in_lot_name = getvariable('q5_lot_name')
    UNION
    SELECT e.out_lot_uuid, e.out_lot_name FROM edges e JOIN descendants d ON e.in_lot_uuid = d.lot_uuid
)
SELECT DISTINCT lot_name FROM descendants WHERE lot_name != getvariable('q5_lot_name') ORDER BY lot_name;
