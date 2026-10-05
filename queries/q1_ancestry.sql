-- Q1 ancestry: every lot state that the state q1_lot_uuid was made from, directly or through other states.
WITH RECURSIVE
edges AS MATERIALIZED (       -- one row per input state -> output state, built once
    SELECT DISTINCT in_lot_uuid, out_lot_uuid FROM events WHERE in_lot_uuid IS NOT NULL AND out_lot_uuid IS NOT NULL
),
ancestors(lot_uuid) AS (
    SELECT in_lot_uuid FROM edges WHERE out_lot_uuid = getvariable('q1_lot_uuid')
    UNION
    SELECT e.in_lot_uuid FROM edges e JOIN ancestors a ON e.out_lot_uuid = a.lot_uuid
)
SELECT lot_uuid FROM ancestors ORDER BY lot_uuid;
