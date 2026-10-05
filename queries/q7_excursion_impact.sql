-- Q7 excursion impact: the lots produced on equipment q7_equipment with q7_from <= begin_ts < q7_to, and every lot made
-- from them. The walk follows lot states, not names: a feeder keeps its name across refills, and the parts dispensed from
-- its earlier states are not affected.
WITH RECURSIVE
edges AS MATERIALIZED (       -- one row per input state -> output state, built once
    SELECT DISTINCT in_lot_uuid, out_lot_uuid, out_lot_name FROM events WHERE in_lot_uuid IS NOT NULL AND out_lot_uuid IS NOT NULL
),
impacted(lot_uuid, lot_name) AS (
    SELECT out_lot_uuid, out_lot_name FROM events
    WHERE equipment_path = getvariable('q7_equipment') AND begin_ts >= getvariable('q7_from') AND begin_ts < getvariable('q7_to')
      AND out_lot_uuid IS NOT NULL
    UNION
    SELECT e.out_lot_uuid, e.out_lot_name FROM edges e JOIN impacted i ON e.in_lot_uuid = i.lot_uuid
)
SELECT DISTINCT lot_name FROM impacted ORDER BY lot_name;
