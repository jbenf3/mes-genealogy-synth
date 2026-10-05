-- Q2 descendants: every lot state made from the state q2_lot_uuid, directly or through other states.
WITH RECURSIVE
edges AS MATERIALIZED (       -- one row per input state -> output state, built once
    SELECT DISTINCT in_lot_uuid, out_lot_uuid FROM events WHERE in_lot_uuid IS NOT NULL AND out_lot_uuid IS NOT NULL
),
descendants(lot_uuid) AS (
    SELECT out_lot_uuid FROM edges WHERE in_lot_uuid = getvariable('q2_lot_uuid')
    UNION
    SELECT e.out_lot_uuid FROM edges e JOIN descendants d ON e.in_lot_uuid = d.lot_uuid
)
SELECT lot_uuid FROM descendants ORDER BY lot_uuid;
